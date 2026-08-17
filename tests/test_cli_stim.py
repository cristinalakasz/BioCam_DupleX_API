"""The `biocam stim --dry-run` path, which needs no instrument and no DLLs."""

import pytest

from biocam.cli import build_parser, main
from biocam.stim import Electrode, ElectrodeGrid

BASE = [
    "stim",
    "--amplitude", "100",
    "--phase-us", "200",
    "--positive", "10,10",
    "--negative", "20,30",
    "--dry-run",
    "--time-resolution-us", "10",
]


def run(extra=(), argv=None):
    return main(list(argv if argv is not None else BASE) + list(extra))


def with_positive(value):
    """BASE with a different --positive value, without index arithmetic."""
    argv = list(BASE)
    argv[argv.index("--positive") + 1] = value
    return argv


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def test_electrode_list_parses_one_pair():
    args = build_parser().parse_args(BASE)
    assert args.positive == (Electrode(10, 10),)


def test_electrode_list_parses_several_pairs():
    args = build_parser().parse_args(with_positive("10,10;11,12;13,14"))
    assert args.positive == (
        Electrode(10, 10), Electrode(11, 12), Electrode(13, 14),
    )


def test_electrode_list_rejects_a_malformed_pair(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(with_positive("10"))
    assert "is not a row,col pair" in capsys.readouterr().err


def test_electrode_list_rejects_a_non_integer(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(with_positive("a,b"))
    assert "non-integer coordinate" in capsys.readouterr().err


def test_grid_parses_rowsxcols():
    args = build_parser().parse_args(BASE + ["--grid", "32x16"])
    assert args.grid == ElectrodeGrid(32, 16)


def test_grid_defaults_to_the_duplex_array():
    assert build_parser().parse_args(BASE).grid == ElectrodeGrid(64, 64)


def test_grid_rejects_a_malformed_value():
    with pytest.raises(SystemExit):
        build_parser().parse_args(BASE + ["--grid", "64"])


def test_rate_and_period_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            BASE + ["--count", "5", "--rate-hz", "10", "--period-us", "1000"]
        )


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def test_a_single_balanced_pulse_plans(capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert "balanced" in out
    assert "NOT SENT" in out


def test_the_second_phase_mirrors_the_first_by_default(capsys):
    run()
    # +100 uA for 200 us then -100 uA for 200 us
    assert "+100 uA for 200 us" in capsys.readouterr().out


def test_a_train_prints_its_timestamps(capsys):
    assert run(["--count", "4", "--rate-hz", "10"]) == 0
    out = capsys.readouterr().out
    assert "0, 100000, 200000, 300000" in out
    assert "10 Hz" in out


def test_a_train_by_period_matches_the_same_train_by_rate(capsys):
    run(["--count", "3", "--rate-hz", "10"])
    by_rate = capsys.readouterr().out
    run(["--count", "3", "--period-us", "100000"])
    by_period = capsys.readouterr().out
    assert "0, 100000, 200000" in by_rate
    assert "0, 100000, 200000" in by_period


def test_a_long_train_truncates_the_printed_timestamps(capsys):
    assert run(["--count", "50", "--rate-hz", "10"]) == 0
    assert "..." in capsys.readouterr().out


def test_train_net_charge_is_reported(capsys):
    run(["--count", "10", "--rate-hz", "10"])
    assert "train net charge: +0 pC" in capsys.readouterr().out


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------

def test_an_unbalanced_pulse_is_refused(capsys):
    assert run(["--amplitude2", "-50"]) == 2
    assert "net charge" in capsys.readouterr().err


def test_an_unbalanced_pulse_can_be_allowed(capsys):
    assert run(["--amplitude2", "-50", "--allow-unbalanced"]) == 0
    assert "UNBALANCED" in capsys.readouterr().out


def test_an_over_long_pulse_is_refused(capsys):
    assert run(["--phase-us", "8000", "--max-total-ticks", "1000"]) == 2
    err = capsys.readouterr().err
    assert "exceeds the maximum" in err
    assert "shortens the later phases" in err


def test_an_out_of_range_amplitude_is_refused(capsys):
    assert run(["--amplitude", "5000"]) == 2
    assert "outside the stimulator's range" in capsys.readouterr().err


def test_a_duration_off_the_tick_grid_is_refused(capsys):
    assert run(["--phase-us", "205"]) == 2
    assert "not a whole number" in capsys.readouterr().err


def test_an_out_of_array_electrode_is_refused(capsys):
    # ChCoord(99, 99) reports IsValid == True, so the CLI is the only place
    # this is caught.
    assert run(argv=with_positive("99,99")) == 2
    assert "outside the 64x64 array" in capsys.readouterr().err


def test_shared_column_is_refused(capsys):
    argv = [a if a != "20,30" else "20,10" for a in BASE]
    assert run(argv=argv) == 2
    assert "share column" in capsys.readouterr().err


def test_shared_column_can_be_overridden(capsys):
    argv = [a if a != "20,30" else "20,10" for a in BASE]
    assert run(["--no-column-rule"], argv=argv) == 0


def test_a_train_without_a_rate_is_refused():
    with pytest.raises(SystemExit, match="needs --rate-hz or --period-us"):
        run(["--count", "5"])


def test_a_short_period_is_refused(capsys):
    assert run(["--count", "5", "--period-us", "600"]) == 2
    assert "below the driver's minimum distance" in capsys.readouterr().err


def test_a_short_period_can_be_allowed(capsys):
    assert run(["--count", "5", "--period-us", "600",
                "--allow-short-period"]) == 0


def test_overlapping_pulses_are_refused_even_when_short_periods_are_allowed(
    capsys,
):
    # The pulse is 400 us; a 300 us period would overlap. This one is never
    # waived, because it is arithmetic rather than policy.
    assert run(["--count", "5", "--period-us", "300",
                "--allow-short-period"]) == 2
    assert "would overlap" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the dry-run guard
# --------------------------------------------------------------------------

def test_dry_run_without_a_time_resolution_refuses_to_guess(capsys):
    argv = [a for a in BASE if a not in ("--time-resolution-us", "10")]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "needs --time-resolution-us" in err
    assert "Do not guess it" in err


def test_dry_run_says_the_constraints_were_not_read_from_a_device(capsys):
    run()
    out = capsys.readouterr().out
    assert "not read from an instrument" in out
