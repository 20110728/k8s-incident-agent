"""Keep test outcome available to guarded per-test database finalizers."""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "test_report_" + report.when, report)
