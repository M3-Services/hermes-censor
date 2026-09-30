"""Integration with the REAL Hermes. Skipped if Hermes cannot be imported or HERMES_TEST_HOME is unset.

Never touches the user's profile: see harness.make_hermes_home (it refuses the real profile).
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def pytest_collection_modifyitems(config, items):
    reason = None
    if not os.environ.get("HERMES_TEST_HOME"):
        reason = "HERMES_TEST_HOME not set (an isolated Hermes test profile is required)"
    else:
        try:
            os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
            os.environ["HERMES_HOME"] = os.environ["HERMES_TEST_HOME"]
            import importlib
            importlib.import_module("run_agent")
        except Exception as exc:  # Hermes missing or profile not provisioned
            reason = f"Hermes cannot be imported ({type(exc).__name__})"
    if reason:
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip)
