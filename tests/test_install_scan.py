"""Gate: Hermes' own install scanner must not block this plugin.

``hermes plugins install/update`` scans the repository and refuses a "dangerous" verdict (``--force`` does not
override it). A single ``SECRET = "..."`` constant outside ``tests/`` is enough for a CRITICAL finding, which is how an
update was once blocked. This test runs the same scanner on the working tree and fails on any critical/high finding,
so the problem shows up before pushing.

Needs the Hermes sources (``HERMES_SRC``, see run_tests.sh); skipped without them.
"""
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/M3-Services/hermes-censor.git/community"  # how a `hermes plugins update` sees it


def _scan():
    src = os.environ.get("HERMES_SRC")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="censor_scan_"))  # never the real profile
    try:
        from tools import plugin_guard
    except Exception as exc:
        pytest.skip(f"Hermes sources not importable ({type(exc).__name__}); set HERMES_SRC")
    if not hasattr(plugin_guard, "scan_plugin"):
        pytest.skip("this Hermes has no tools.plugin_guard.scan_plugin")
    return plugin_guard.scan_plugin(ROOT, SOURCE)


def test_the_install_scan_finds_nothing_critical_or_high():
    result = _scan()
    serious = [f"{f.severity} {f.pattern_id} {f.file}:{f.line}" for f in result.findings
               if f.severity in ("critical", "high")]
    assert not serious, "Hermes would block the install:\n  " + "\n  ".join(serious)
    assert str(result.verdict).lower() != "dangerous"
