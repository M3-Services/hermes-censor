"""Tk window prompt channel: real Tk, a fresh process per scenario, driven by an internal Python hook
(no keystroke is sent to the user's desktop; the window appears briefly)."""
import json
import os
import subprocess
import sys

import pytest

from censor_core.kpx_helper import HelperError, _ChannelUnavailable, ask_password

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_driver.py")


def run(scenario):
    proc = subprocess.run([sys.executable, DRIVER, ROOT, scenario], capture_output=True, timeout=60)
    return json.loads(proc.stdout.decode("ascii")) if proc.stdout else {"error": "no_output", "stderr": proc.stderr[-300:]}


def _tk_usable():
    proc = subprocess.run([sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"], capture_output=True)
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(not _tk_usable(), reason="Tk unavailable (no display)")


def test_gui_returns_typed_password_and_masks_it():
    out = run("typed")
    assert out["result"] == "Fictional-password é€" and out["show"] == "*"


def test_gui_cancel_is_reported_as_cancelled():
    assert run("cancel")["error"] == "unlock_cancelled"


def test_gui_empty_password_is_cancelled():
    assert run("empty")["error"] == "unlock_cancelled"


def test_gui_times_out():
    assert run("timeout")["error"] == "unlock_cancelled"


def test_channel_order_falls_through_unavailable_channels(monkeypatch):
    import censor_core.kpx_helper as h

    def unavailable(*a, **k):
        raise _ChannelUnavailable()

    monkeypatch.setattr(h, "_ask_gui", unavailable)
    monkeypatch.setattr(h, "_ask_tty", unavailable)
    with pytest.raises(HelperError) as exc:
        ask_password(["gui", "tty"], None, 1, "t")
    assert exc.value.code == "no_prompt_channel"
