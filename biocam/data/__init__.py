"""Layer 2 — pure data logic.

Payload bytes to frames, partial-frame carry-over across packet boundaries,
ADC counts to microvolts, metadata handling, gap detection from hardware
timestamps.

Touches no hardware. Every function here is a function from bytes and numbers to
bytes and numbers, and must be unit-tested with synthetic buffers — including the
awkward cases that are hard to produce on real hardware, such as a payload ending
mid-frame or a timestamp discontinuity indicating packet loss.

MUST NOT import `clr` or `pythonnet`, directly or transitively.

Empty in Phase 0. Populated by Phase 1.
"""
