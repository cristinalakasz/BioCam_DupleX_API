"""Layer 3 - streaming filters for extracellular signal.

Extracellular recordings carry two things at once: slow local field potential
and drift below a few hundred hertz, and the fast, roughly millisecond-wide
deflections that are spikes. Detecting the second means removing the first,
because a threshold on the raw signal tracks the baseline wander instead of
the spikes.

Everything here is **causal and stateful**, because it has to run on a
recording as it arrives. A filter that needs the whole recording is fine for
analysis afterwards and useless for a closed loop. The consequence is that
these produce exactly the same output whether a recording is fed in one block
or in a hundred - which is a property worth testing rather than assuming,
since a filter that quietly resets its state at every packet boundary
produces a plausible-looking transient 9000 times a second.

No scipy: this project depends on numpy alone, and a second-order section is
forty lines. The coefficient design is the standard bilinear transform with
frequency pre-warping, and the tests check the response rather than the
arithmetic.
"""

import numpy as np

# Below this many channels, the recursion is run in plain Python floats
# instead of numpy arrays.
#
# The recursion is sequential in time, so it cannot be vectorised over frames
# - which means one numpy call per coefficient per frame, and on a handful of
# channels those calls are almost entirely interpreter overhead. Measured on
# a 37-frame packet (a 2 ms acquisition period at 18.5 kHz):
#
#     channels    scalar      numpy
#            1      12 us     421 us
#            4      40 us     437 us
#           16     117 us     481 us
#           64     460 us     473 us
#
# Thirty-five times faster at one channel. That is the difference between
# closed-loop detection being affordable on the acquisition thread and not,
# so it is worth the second code path - which is required to agree with the
# first exactly, and is tested to.
#
# A later pass removed this path's per-packet containers: the state no longer
# round-trips through `tolist()`/`asarray()`, and the output goes through a
# reused scratch list instead of a list per row. Measured through `process()`
# on a 19-frame packet (1 ms at 18.5 kHz), median of five alternating A/B runs
# because a single run on this machine varies by 50%:
#
#     channels    before     after
#            1      50 us     39 us    -22%
#            4      88 us     71 us    -19%
#           32     497 us    468 us     -6%
#
# Modest, and worth stating as such - an earlier benchmark of the bare loop,
# without `process`'s own validation, suggested 2x and was measuring something
# nobody runs.
#
# It was NOT done for garbage-collection reasons, and the reasoning that
# suggested it should be is worth writing down because it is a trap. Counting
# "objects allocated per packet" says ~200, which sounds like it should drive
# gen-0 collections constantly. It does not: CPython's gen-0 counter is
# allocations *minus* deallocations, and floats are not GC-tracked at all, so
# containers created and freed inside one call cancel out completely. Measured
# directly, 50,000 packets through this path cause **zero** collections at any
# width. Only objects that SURVIVE drive collections - which is why the cap on
# retained waveforms mattered and this did not.
#
# The 32-channel case got slower under the same change (448 -> 628 us) when the
# inner loop was flattened, so the row-wise `enumerate` was kept. Wide
# detection remains expensive; see the closed-loop budget document.
SCALAR_CHANNEL_LIMIT = 48


def highpass_coefficients(cutoff_hz: float, frame_rate_hz: float):
    """Second-order Butterworth high-pass, as (b, a) with a[0] == 1.

    Bilinear transform with pre-warping, which is what makes the -3 dB point
    land at `cutoff_hz` rather than somewhere near it: the bilinear transform
    compresses the frequency axis, and `tan(w0/2)` undoes that at the one
    frequency we care about.
    """
    if frame_rate_hz <= 0:
        raise ValueError(f"frame_rate_hz must be positive, got {frame_rate_hz}")
    nyquist = frame_rate_hz / 2.0
    if not 0 < cutoff_hz < nyquist:
        raise ValueError(
            f"cutoff_hz must be between 0 and the Nyquist frequency "
            f"({nyquist:g} Hz), got {cutoff_hz}"
        )

    k = np.tan(np.pi * cutoff_hz / frame_rate_hz)
    root2 = np.sqrt(2.0)
    norm = 1.0 / (1.0 + root2 * k + k * k)

    b = np.array([norm, -2.0 * norm, norm], dtype=np.float64)
    a = np.array([
        1.0,
        2.0 * (k * k - 1.0) * norm,
        (1.0 - root2 * k + k * k) * norm,
    ], dtype=np.float64)
    return b, a


class Biquad:
    """One second-order section, applied down the time axis of a block.

    State is per channel, so a block of shape (frames, channels) can be fed
    in and the next block continues exactly where this one stopped.

    Direct Form II transposed: it needs two state variables per channel
    rather than four, and it is the form least sensitive to coefficient
    rounding at low cutoff frequencies - which is the regime a 300 Hz
    high-pass at 18.5 kHz sits in.
    """

    def __init__(self, b, a, n_channels: int):
        b = np.asarray(b, dtype=np.float64)
        a = np.asarray(a, dtype=np.float64)
        if b.shape != (3,) or a.shape != (3,):
            raise ValueError("b and a must each have three coefficients")
        if a[0] != 1.0:
            b = b / a[0]
            a = a / a[0]
        self.b = b
        self.a = a
        self.n_channels = n_channels
        # Two state variables per channel, carried across blocks. This is the
        # whole reason the class exists rather than a function.
        self._z1 = np.zeros(n_channels, dtype=np.float64)
        self._z2 = np.zeros(n_channels, dtype=np.float64)
        # Which path this instance will take, decided once. `n_channels` never
        # changes, so a given filter always takes the same branch and the two
        # state representations below can never interleave.
        self._scalar = n_channels <= SCALAR_CHANNEL_LIMIT
        # The scalar path's state, as plain Python lists. Kept across calls so
        # the recursion neither converts the arrays to lists on the way in nor
        # rebuilds them on the way out - that round trip was four allocations
        # per packet for no arithmetic. `reset` and `warm_up` write both
        # representations, so the two never disagree.
        self._z1_list = [0.0] * n_channels
        self._z2_list = [0.0] * n_channels
        # A reused scratch list for the scalar path's output, grown when the
        # block size changes. A recording has one packet size, so after the
        # first block this is never reallocated. It is deliberately not an
        # output *array*: `process` hands its array to the caller, who may
        # still be reading it when the next block arrives.
        self._flat = []

    def reset(self):
        """Forget the past. Only correct at the start of a new recording."""
        self._z1[:] = 0.0
        self._z2[:] = 0.0
        # Both representations, always - see __init__. Cheap, and rare.
        for i in range(self.n_channels):
            self._z1_list[i] = 0.0
            self._z2_list[i] = 0.0

    def warm_up(self, sample):
        """Settle the state as though `sample` had been the input forever.

        Without this, a recording whose baseline sits at 2048 counts starts
        with a step from zero, and the filter rings for the length of its
        impulse response - tens of milliseconds of large, entirely artificial
        deflections that a spike detector would happily report as spikes.
        """
        sample = np.asarray(sample, dtype=np.float64)
        if sample.shape != (self.n_channels,):
            raise ValueError(
                f"expected one value per channel ({self.n_channels}), got "
                f"shape {sample.shape}"
            )
        # Steady state of the transposed form for a constant input: the
        # output of a high-pass at DC is zero, so z1 and z2 settle to the
        # values that hold y at zero for that input.
        self._z1 = (self.b[1] + self.b[2]) * sample
        self._z2 = self.b[2] * sample
        self._z1_list = self._z1.tolist()
        self._z2_list = self._z2.tolist()

    def process(self, block):
        """Filter a (frames, channels) block. Returns float64 of the same shape.

        The loop is over frames, not channels: at 4096 channels and a few tens
        of frames per packet that is tens of iterations of a vectorised
        expression rather than thousands of scalar ones.
        """
        block = np.asarray(block, dtype=np.float64)
        if block.ndim != 2 or block.shape[1] != self.n_channels:
            raise ValueError(
                f"expected a (frames, {self.n_channels}) block, got shape "
                f"{block.shape}"
            )
        if self._scalar:
            return self._process_scalar(block)
        return self._process_vector(block)

    def _process_vector(self, block):
        out = np.empty_like(block)
        b0, b1, b2 = self.b
        a1, a2 = self.a[1], self.a[2]
        z1, z2 = self._z1, self._z2
        for i in range(block.shape[0]):
            x = block[i]
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[i] = y
        self._z1, self._z2 = z1, z2
        return out

    def _process_scalar(self, block):
        """The same recursion in plain floats. See SCALAR_CHANNEL_LIMIT.

        Identical arithmetic in the identical order, so it produces
        bit-identical results to the vector path - IEEE-754 doubles do not
        care whether the interpreter or numpy performed the multiply. That
        equality is the point, and it is tested: two paths that were allowed
        to drift would mean a closed loop triggering on one signal while the
        recording was analysed with another.
        """
        b0, b1, b2 = float(self.b[0]), float(self.b[1]), float(self.b[2])
        a1, a2 = float(self.a[1]), float(self.a[2])
        z1, z2 = self._z1_list, self._z2_list

        n_frames = block.shape[0]
        if len(self._flat) != n_frames * self.n_channels:
            self._flat = [0.0] * (n_frames * self.n_channels)
        flat = self._flat
        # A fresh array, not a reused one. `process` returns this to the
        # caller, and the detector holds on to the previous block's output
        # across calls - handing back the same buffer each time would rewrite
        # data someone was still reading. The scratch *list* is reusable
        # precisely because it never leaves this method.
        out = np.empty((n_frames, self.n_channels), dtype=np.float64)

        # `block.tolist()` rather than indexing the array: reading `block[i, c]`
        # builds a numpy scalar per access, which is both slower and more
        # garbage than one C-level conversion of the whole block.
        i = 0
        for frame in block.tolist():
            for c, x in enumerate(frame):
                y = b0 * x + z1[c]
                z1[c] = b1 * x - a1 * y + z2[c]
                z2[c] = b2 * x - a2 * y
                flat[i] = y
                i += 1
        out.reshape(-1)[:] = flat
        # `_z1`/`_z2` are the vector path's state and are not read here, but a
        # caller that inspects them after a scalar block should not see zeros.
        self._z1[:] = z1
        self._z2[:] = z2
        return out


class HighPass(Biquad):
    """A second-order Butterworth high-pass, ready to stream.

    300 Hz is the usual choice for extracellular spike detection: high enough
    to remove the local field potential and electrode drift, low enough to
    leave the spike waveform recognisable. It is a default, not a law - the
    right value depends on the preparation.
    """

    def __init__(self, n_channels: int, frame_rate_hz: float,
                 cutoff_hz: float = 300.0):
        b, a = highpass_coefficients(cutoff_hz, frame_rate_hz)
        super().__init__(b, a, n_channels)
        self.cutoff_hz = cutoff_hz
        self.frame_rate_hz = frame_rate_hz

    def gain_at(self, frequency_hz):
        """|H(f)|, for checking the filter is doing what it claims.

        Exposed rather than kept in the tests because it is the only honest
        way to answer "is 300 Hz actually passing?" without a spectrum
        analyser, and because a filter whose response nobody can see is a
        filter nobody can trust.
        """
        frequency = np.asarray(frequency_hz, dtype=np.float64)
        w = 2.0 * np.pi * frequency / self.frame_rate_hz
        z = np.exp(-1j * w)
        numerator = self.b[0] + self.b[1] * z + self.b[2] * z * z
        denominator = 1.0 + self.a[1] * z + self.a[2] * z * z
        return np.abs(numerator / denominator)
