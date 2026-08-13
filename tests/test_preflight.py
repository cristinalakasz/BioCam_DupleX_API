import pytest
import sys
from pathlib import Path

from biocam.preflight import CheckResult, REQUIRED_DLLS, check_environment, format_report


def test_reports_a_result_for_every_required_dll(tmp_path):
    results = check_environment(tmp_path)
    dll_checks = [r for r in results if r.name.endswith(".dll")]
    assert len(dll_checks) == len(REQUIRED_DLLS)


def test_missing_dlls_are_reported_as_failures(tmp_path):
    results = check_environment(tmp_path)
    assert all(not r.ok for r in results if r.name.endswith(".dll"))


def test_present_dlls_are_reported_as_passing(tmp_path):
    for name in REQUIRED_DLLS:
        (tmp_path / name).write_bytes(b"x")
    results = check_environment(tmp_path)
    assert all(r.ok for r in results if r.name.endswith(".dll"))


def test_zero_length_dlls_are_reported_as_failures(tmp_path):
    for name in REQUIRED_DLLS:
        (tmp_path / name).write_bytes(b"")
    results = check_environment(tmp_path)
    assert all(not r.ok for r in results if r.name.endswith(".dll"))


def test_python_version_check_passes_on_the_running_interpreter():
    results = check_environment(Path("."))
    version_check = next(r for r in results if r.name == "Python version")
    assert version_check.ok is (sys.version_info[:2] >= (3, 12))


def test_report_marks_failures_visibly():
    report = format_report([
        CheckResult("thing that worked", True, "fine"),
        CheckResult("thing that failed", False, "missing"),
    ])
    assert "PASS" in report
    assert "FAIL" in report
    assert "thing that failed" in report


def test_report_ends_with_an_overall_verdict():
    passing = format_report([CheckResult("a", True, "")])
    failing = format_report([CheckResult("a", False, "")])
    assert passing.strip().endswith("ALL CHECKS PASSED")
    assert "1 CHECK FAILED" in failing


def test_bytes_per_second_matches_the_reference_recording():
    from biocam.preflight import bytes_per_second
    rate = bytes_per_second(total_channels=4096, ch_sample_byte_size=2,
                            frame_rate_hz=18557.720703125)
    assert rate == pytest.approx(152_024_848, rel=1e-6)


def test_disk_check_passes_when_there_is_room(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=1, bytes_per_sec=1000)
    assert result.ok is True
    assert "1,000" in result.detail or "1000" in result.detail


def test_disk_check_fails_when_the_requirement_exceeds_free_space(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=10**9, bytes_per_sec=10**9)
    assert result.ok is False


def test_disk_check_names_the_directory_it_examined(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=1, bytes_per_sec=1)
    assert str(tmp_path) in result.detail
