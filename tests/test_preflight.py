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
