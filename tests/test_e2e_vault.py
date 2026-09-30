"""Runtime + REAL keepassxc-cli + fictional databases: locked, modified, unavailable, refreshed vault (no Hermes)."""
import logging
import os
import sys
import time

import pytest

from censor_core.runtime import Runtime
from kpx_fixtures import E, G, build_kdbx, find_cli

CLI = find_cli()
pytestmark = pytest.mark.skipif(CLI is None, reason="keepassxc-cli introuvable")

MASTER = "Fictional-Master-E2E-1"
V1, V2 = "FirstValue-AAAA-1111", "SecondValue-BBBB-2222"
MSG = {"messages": [{"role": "user", "content": f"a {V1} b {V2} c"}]}


def tree(value):
    return G("R", groups=[G("Hermes", entries=[E("Server", {"Password": value})])])


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.tmp = tmp_path
    e.db = build_kdbx(CLI, str(tmp_path / "v.kdbx"), tree(V1), password=MASTER)
    e.askpass = tmp_path / "askpass.py"
    e.askpass.write_text(f"import sys; sys.stdout.buffer.write({MASTER!r}.encode('utf-8'))", encoding="utf-8")
    e.settings = {"keepassxc": {"database": e.db, "cli": [CLI], "groups": ["Hermes"],
                                "askpass": [sys.executable, str(e.askpass)]}}
    e.clock = type("C", (), {"now": 1000.0, "__call__": lambda s: s.now})()
    e.rt = Runtime(settings_provider=lambda: dict(e.settings), home=str(tmp_path), clock=e.clock)
    return e


def level(e):
    return e.rt.status().level


def content(e):
    out = e.rt.process_request(MSG)
    return (out or MSG)["messages"][0]["content"]


def test_locked_then_unlocked(env):
    # locked: nothing loaded, the values are NOT protected and the state says so
    assert level(env) == "DEGRADED" and V1 in content(env)
    assert "secret protection unavailable" in env.rt.render_status().lower()
    outcome = env.rt.unlock(interactive=True)
    assert outcome.ok, outcome.message
    assert level(env) == "ACTIVE" and V1 not in content(env) and V2 in content(env)


def test_non_interactive_session_reports_unavailable_protection(env):
    outcome = env.rt.unlock(interactive=False)
    assert not outcome.ok and "keyfile-only" in outcome.message
    assert level(env) == "DEGRADED" and V1 in content(env)


def test_modified_database_marks_stale_and_refresh_picks_up_new_secret(env):
    env.rt.unlock(interactive=True)
    assert level(env) == "ACTIVE"
    time.sleep(0.05)
    build_kdbx(CLI, env.db, tree(V2), password=MASTER)        # the database changes (new value)
    env.clock.now += 30
    text = content(env)
    assert V1 not in text and V2 in text                       # the old list still protects V1, not V2
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_STALE_DB_CHANGED" in {f.code for f in st.findings}
    assert env.rt.unlock(interactive=True).ok                  # refresh
    assert level(env) == "ACTIVE" and V2 not in content(env) and V1 in content(env)


def test_database_becomes_unavailable(env):
    env.rt.unlock(interactive=True)
    os.remove(env.db)
    env.clock.now += 30
    assert V1 not in content(env)                              # list kept, never presented as up to date
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_STALE_DB_MISSING" in {f.code for f in st.findings}
    refused = env.rt.unlock(interactive=True)
    assert not refused.ok and "not found" in refused.message
    assert "SECRETS_LOAD_FAILED" in {f.code for f in env.rt.status().findings}


def test_wrong_password_on_refresh_keeps_previous_list_and_flags_it(env):
    env.rt.unlock(interactive=True)
    env.askpass.write_text("import sys; sys.stdout.buffer.write(b'wrong-password-9')", encoding="utf-8")
    refused = env.rt.unlock(interactive=True)
    assert not refused.ok and "refused" in refused.message
    assert V1 not in content(env)
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_LOAD_FAILED" in {f.code for f in st.findings}
    assert "wrong-password-9" not in env.rt.render_status() and MASTER not in env.rt.render_status()


def test_keyfile_only_full_cycle_with_auto_refresh(tmp_path):
    key = str(tmp_path / "k.keyx")
    db = build_kdbx(CLI, str(tmp_path / "k.kdbx"), tree(V1), keyfile=key)
    settings = {"keepassxc": {"database": db, "keyfile": key, "unlock": "keyfile-only", "cli": [CLI],
                              "groups": ["Hermes"]}}
    clock = type("C", (), {"now": 1000.0, "__call__": lambda s: s.now})()
    rt = Runtime(settings_provider=lambda: dict(settings), home=str(tmp_path), clock=clock)
    assert V1 not in rt.process_request(MSG)["messages"][0]["content"]       # loaded lazily, without any prompt
    assert rt.status().level == "ACTIVE"
    time.sleep(0.05)
    build_kdbx(CLI, db, tree(V2), keyfile=key)
    clock.now += 30
    out = rt.process_request(MSG)["messages"][0]["content"]                  # refreshed automatically
    assert V2 not in out and V1 in out
    assert rt.status().level == "ACTIVE"


def test_secret_priority_over_rule_with_real_values(env):
    (env.tmp / "rules.txt").write_text("FirstValue:X\nfox:animal\n", encoding="utf-8")
    env.settings["rules_file"] = str(env.tmp / "rules.txt")
    env.rt.unlock(interactive=True)
    out = env.rt.process_request({"messages": [{"role": "user", "content": f"fox {V1} FirstValue"}]})
    assert out["messages"][0]["content"] == "animal [SECRET] X"


def test_nothing_sensitive_in_logs_or_status_across_the_whole_lifecycle(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.rt.process_request(MSG)
    env.rt.unlock(interactive=False)
    env.rt.unlock(interactive=True)
    time.sleep(0.05)
    build_kdbx(CLI, env.db, tree(V2), password=MASTER)
    env.clock.now += 30
    env.rt.process_request(MSG)
    env.rt.unlock(interactive=True)
    env.rt.forget()
    blob = caplog.text + env.rt.render_status(verbose=True) + repr(env.rt.activity())
    for secret in (V1, V2, MASTER):
        assert secret not in blob
