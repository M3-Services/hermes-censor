"""Runtime: configuration, ACTIVE/DEGRADED/INACTIVE/ERROR states, refresh, no leak."""
import logging
import os
import threading

import pytest

from censor_core import runtime as runtime_mod
from censor_core.runtime import Runtime
from censor_core.vault import LoadReport, SecretLoad, SecretSourceError

SECRET = "Fictional-Secret-Value-1"
MSG = {"model": "m", "messages": [{"role": "user", "content": f"the fox keeps {SECRET} here"}]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSource:
    name = "fake"

    def __init__(self, values=(SECRET,), fp="1", fail=None, **report):
        self.values, self.fp, self.fail, self.report = list(values), fp, fail, report
        self.load_calls = 0
        self.interactive_seen = []

    def load(self, *, interactive):
        self.load_calls += 1
        self.interactive_seen.append(interactive)
        if self.fail:
            raise self.fail
        rep = LoadReport(source="fake", scope="exact fictional scope", entries_selected=2,
                         values_loaded=len(self.values), fingerprint=self.fp, loaded_at=0, **self.report)
        return SecretLoad(list(self.values), rep)

    def fingerprint(self):
        return self.fp

    def describe(self):
        return "exact fictional scope"


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    settings = {}
    src = FakeSource()
    rt = Runtime(settings_provider=lambda: dict(settings), home=str(tmp_path), source_factory=lambda cfg: src,
                 clock=clock)
    rules = tmp_path / "rules.txt"

    class Env:
        pass

    e = Env()
    e.clock, e.settings, e.src, e.rt, e.rules, e.tmp = clock, settings, src, rt, rules, tmp_path
    return e


def rules_config(e, text="fox:animal\n"):
    e.rules.write_text(text, encoding="utf-8")
    e.settings["rules_file"] = str(e.rules)


def vault_config(e, **extra):
    kp = {"enabled": True, "database": str(e.tmp / "x.kdbx"), "groups": ["Hermes"]}
    kp.update(extra)
    e.settings["keepassxc"] = kp


def codes(status):
    return {f.code for f in status.findings}


# ---------- basic states ----------

def test_nothing_configured_is_inactive_with_repair(env):
    st = env.rt.status()
    assert st.level == "INACTIVE" and "NOTHING_CONFIGURED" in codes(st)
    assert all(f.repair for f in st.findings if f.code == "NOTHING_CONFIGURED")
    assert env.rt.process_request(MSG) is None


def test_disabled_by_config_is_inactive_and_leaves_requests_alone(env):
    rules_config(env)
    env.settings["enabled"] = False
    assert env.rt.status().level == "INACTIVE"
    assert env.rt.process_request(MSG) is None


def test_rules_only_is_active_and_censors(env):
    rules_config(env)
    out = env.rt.process_request(MSG)
    assert out["messages"][0]["content"] == f"the animal keeps {SECRET} here"
    assert env.rt.status().level == "ACTIVE"


def test_missing_configured_rules_file_is_degraded(env):
    env.settings["rules_file"] = str(env.tmp / "absent.txt")
    st = env.rt.status()
    assert st.level == "DEGRADED" and "RULES_MISSING" in codes(st)


def test_invalid_rule_lines_degrade_and_never_echo_content(env):
    rules_config(env, "fox:animal\nsecretwordontheline\n:empty\n")
    st = env.rt.status()
    assert st.level == "DEGRADED" and "RULES_INVALID_LINES" in codes(st)
    text = env.rt.render_status()
    assert "line 2" in text and "line 3" in text and "secretwordontheline" not in text
    assert env.rt.process_request(MSG) is not None  # valid rules stay applied


def test_rules_file_changes_are_picked_up_and_last_good_kept_if_it_vanishes(env):
    rules_config(env, "fox:animal\n")
    assert env.rt.process_request(MSG) is not None
    env.rules.write_text("fox:vixen\n", encoding="utf-8")
    os.utime(env.rules, (env.rules.stat().st_atime, env.rules.stat().st_mtime + 5))
    env.clock.advance(10)
    assert "vixen" in env.rt.process_request(MSG)["messages"][0]["content"]
    env.rules.unlink()
    env.clock.advance(10)
    out = env.rt.process_request(MSG)
    assert "vixen" in out["messages"][0]["content"]  # last valid version kept
    st = env.rt.status()
    assert st.level == "DEGRADED" and "RULES_MISSING" in codes(st)


def test_rule_conflicts_are_only_informational(env):
    rules_config(env, "a1234:1\na1234:2\n")
    st = env.rt.status()
    assert st.level == "ACTIVE" and "RULE_CONFLICTS" in codes(st)


def test_ignore_case_setting(env):
    rules_config(env)
    env.settings["ignore_case"] = True
    out = env.rt.process_request({"messages": [{"role": "user", "content": "FOX"}]})
    assert out["messages"][0]["content"] == "animal"


# ---------- vault ----------

def test_vault_not_unlocked_is_degraded_but_rules_apply(env):
    rules_config(env)
    vault_config(env)
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_NOT_LOADED" in codes(st)
    assert any("/censor unlock" in f.repair for f in st.findings if f.code == "SECRETS_NOT_LOADED")
    out = env.rt.process_request(MSG)
    assert SECRET in out["messages"][0]["content"] and "animal" in out["messages"][0]["content"]
    assert "secret protection unavailable" in env.rt.render_status().lower()


def test_unlock_makes_it_active_and_secret_is_replaced(env):
    rules_config(env)
    vault_config(env)
    outcome = env.rt.unlock(interactive=True)
    assert outcome.ok and env.src.interactive_seen == [True]
    st = env.rt.status()
    assert st.level == "ACTIVE"
    out = env.rt.process_request(MSG)
    assert out["messages"][0]["content"] == "the animal keeps [SECRET] here"


def test_unlock_failure_reports_code_and_stays_degraded(env):
    vault_config(env)
    env.src.fail = SecretSourceError("unlock_failed", "unlock refused", "check the password")
    outcome = env.rt.unlock(interactive=True)
    assert not outcome.ok and "unlock refused" in outcome.message
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_LOAD_FAILED" in codes(st)


def test_failed_refresh_keeps_previous_list_but_is_flagged(env):
    vault_config(env)
    env.rt.unlock(interactive=True)
    env.src.fail = SecretSourceError("unlock_failed", "refused", "try again")
    env.rt.unlock(interactive=True)
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_LOAD_FAILED" in codes(st)
    assert env.rt.process_request(MSG)["messages"][0]["content"].count("[SECRET]") == 1


def test_short_and_not_found_values_degrade(env):
    vault_config(env)
    env.src.report = dict(skipped_short=1, short_fields=["Hermes/Short:Password"], not_found=["Ghost"])
    env.rt.unlock(interactive=True)
    st = env.rt.status()
    assert st.level == "DEGRADED" and {"SECRETS_INCOMPLETE_SHORT", "SECRETS_INCOMPLETE_NOT_FOUND"} <= codes(st)
    text = env.rt.render_status()
    assert "Hermes/Short:Password" in text and "Ghost" in text


def test_stale_when_database_changes_is_flagged_but_old_list_still_applies(env):
    vault_config(env)
    env.rt.unlock(interactive=True)
    env.src.fp = "2"
    env.clock.advance(30)
    out = env.rt.process_request(MSG)
    assert "[SECRET]" in out["messages"][0]["content"]
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_STALE_DB_CHANGED" in codes(st)
    env.rt.unlock(interactive=True)
    assert env.rt.status().level == "ACTIVE"


def test_stale_when_database_disappears(env):
    vault_config(env)
    env.rt.unlock(interactive=True)
    env.src.fp = None
    env.clock.advance(30)
    env.rt.process_request(MSG)
    assert "SECRETS_STALE_DB_MISSING" in codes(env.rt.status())


def test_stale_by_age(env):
    vault_config(env)
    env.settings["max_age_minutes"] = 10
    env.rt.unlock(interactive=True)
    env.clock.advance(11 * 60)
    env.rt.process_request(MSG)
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_STALE_AGE" in codes(st)


def test_forget_drops_secrets(env):
    rules_config(env)
    vault_config(env)
    env.rt.unlock(interactive=True)
    assert "[SECRET]" in env.rt.process_request(MSG)["messages"][0]["content"]
    env.rt.forget()
    out = env.rt.process_request(MSG)
    assert SECRET in out["messages"][0]["content"]  # no more secret protection (the rules stay)
    st = env.rt.status()
    assert st.level == "DEGRADED" and "SECRETS_NOT_LOADED" in codes(st)


def test_secret_index_not_kept_as_plain_list(env):
    vault_config(env)
    env.rt.unlock(interactive=True)
    assert not any(SECRET in repr(v) for v in vars(env.rt).values() if isinstance(v, (list, tuple, set, dict)))


# ---------- keyfile-only : chargement paresseux, non interactif ----------

def test_keyfile_only_loads_lazily_on_first_request_without_prompt(env):
    vault_config(env, unlock="keyfile-only", keyfile=str(env.tmp / "k.keyx"))
    out = env.rt.process_request(MSG)
    assert "[SECRET]" in out["messages"][0]["content"]
    assert env.src.interactive_seen == [False]
    st = env.rt.status()
    assert st.level == "ACTIVE" and "KEYFILE_ONLY_NOTICE" in codes(st)


def test_keyfile_only_auto_refreshes_when_database_changes(env):
    vault_config(env, unlock="keyfile-only", keyfile=str(env.tmp / "k.keyx"))
    env.rt.process_request(MSG)
    env.src.values = ["New-Secret-Value-2"]
    env.src.fp = "2"
    env.clock.advance(30)
    out = env.rt.process_request({"messages": [{"role": "user", "content": "x New-Secret-Value-2"}]})
    assert out["messages"][0]["content"] == "x [SECRET]"
    assert env.src.load_calls == 2


def test_keyfile_only_failure_is_not_retried_on_every_request(env):
    vault_config(env, unlock="keyfile-only", keyfile=str(env.tmp / "k.keyx"))
    env.src.fail = SecretSourceError("unlock_failed", "refused", "")
    for _ in range(5):
        env.rt.process_request(MSG)
    assert env.src.load_calls == 1
    env.clock.advance(120)
    env.rt.process_request(MSG)
    assert env.src.load_calls == 2
    assert env.rt.status().level == "DEGRADED"


def test_prompt_mode_never_prompts_from_a_request_and_refuses_non_interactive_unlock(env):
    vault_config(env)
    env.rt.process_request(MSG)
    assert env.src.load_calls == 0
    env.src.fail = SecretSourceError("no_prompt_channel", "aucun canal", "utilisez keyfile-only")
    assert not env.rt.unlock(interactive=False).ok


# ---------- detectable fail-open ----------

def test_exception_while_censoring_sets_error_state_then_clears(env, monkeypatch):
    rules_config(env)
    real = runtime_mod.censor_request

    def boom(request, censor):
        raise ValueError(f"contenu sensible {SECRET}")

    monkeypatch.setattr(runtime_mod, "censor_request", boom)
    assert env.rt.process_request(MSG) is None
    st = env.rt.status()
    assert st.level == "ERROR" and "REQUEST_FAILED_OPEN" in codes(st)
    text = env.rt.render_status()
    assert "ValueError" in text and SECRET not in text  # nom de l'exception seulement
    monkeypatch.setattr(runtime_mod, "censor_request", real)
    env.rt.process_request(MSG)
    assert env.rt.status().level == "ACTIVE"


# ---------- configuration invalide ----------

def test_invalid_config_is_reported_and_rules_keep_working(env):
    rules_config(env)
    env.settings["keepassxc"] = {"enabled": True, "database": str(env.tmp / "x.kdbx"), "groups": "not-a-list"}
    st = env.rt.status()
    assert st.level == "DEGRADED" and "CONFIG_INVALID" in codes(st)
    assert env.rt.process_request(MSG) is not None


def test_vault_without_selection_is_a_config_error(env):
    env.settings["keepassxc"] = {"enabled": True, "database": str(env.tmp / "x.kdbx")}
    assert "CONFIG_INVALID" in codes(env.rt.status())


def test_defaults_are_conservative(env):
    vault_config(env)
    cfg = env.rt.settings.keepassxc
    assert cfg.include_history is False and cfg.include_recycle_bin is False and cfg.select_all is False
    assert cfg.min_length == 6 and cfg.unlock == "prompt"


# ---------- tool results ----------

def test_transform_tool_result(env):
    rules_config(env)
    vault_config(env)
    env.rt.unlock(interactive=True)
    assert env.rt.process_tool_result(f"pass={SECRET} fox") == "pass=[SECRET] animal"
    assert env.rt.process_tool_result("rien") is None
    assert env.rt.process_tool_result(12345) is None
    env.settings["censor_tool_results"] = False
    env.clock.advance(10)
    assert env.rt.process_tool_result(f"pass={SECRET}") is None


# ---------- aucune fuite ----------

def test_no_secret_in_logs_status_or_diagnostics(env, caplog):
    caplog.set_level(logging.DEBUG)
    rules_config(env, "fox:animal\nruleword:x\n")
    vault_config(env)
    env.src.fail = SecretSourceError("unlock_failed", "refused", "try again")
    env.rt.unlock(interactive=True)
    env.src.fail = None
    env.rt.unlock(interactive=True)
    env.rt.process_request(MSG)
    env.rt.process_tool_result(SECRET)
    blob = caplog.text + env.rt.render_status() + env.rt.render_status(verbose=True)
    assert SECRET not in blob


def test_status_text_states_exact_scope_limits_and_failopen(env):
    vault_config(env)
    env.rt.unlock(interactive=True)
    text = env.rt.render_status()
    assert "exact fictional scope" in text
    assert "fail-open" in text.lower()
    assert "all the values" not in text.lower()


def test_counters_do_not_include_values(env):
    rules_config(env)
    vault_config(env)
    env.rt.unlock(interactive=True)
    env.rt.process_request(MSG)
    env.rt.process_request(MSG)
    stats = env.rt.activity()
    assert stats["requests"] == 2 and stats["last_secret_replacements"] == 1 and stats["last_rule_replacements"] == 1


def test_concurrent_requests_and_reloads_do_not_crash(env):
    rules_config(env)
    vault_config(env)
    env.rt.unlock(interactive=True)
    errors = []

    def worker():
        try:
            for i in range(50):
                env.clock.advance(3)
                env.rt.process_request(MSG)
                if i % 10 == 0:
                    env.rt.reload_rules(force=True)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


# ---------- commande slash : non bloquante ----------

def test_start_unlock_is_non_blocking_and_single_flight(env):
    vault_config(env)
    gate = threading.Event()
    real = env.src.load

    def slow(*, interactive):
        gate.wait(5)
        return real(interactive=interactive)

    env.src.load = slow
    outcome, thread = env.rt.start_unlock()
    assert outcome.ok and thread is not None and thread.is_alive()
    import time as _t
    _t.sleep(0.05)
    refused, none_thread = env.rt.start_unlock()
    assert not refused.ok and none_thread is None and "already in progress" in refused.message
    gate.set()
    thread.join(5)
    assert env.rt.status().level == "ACTIVE"


def test_command_unlock_returns_immediately_with_instructions_no_secret(env):
    vault_config(env)
    text = env.rt.command("unlock")
    assert "window" in text.lower() and "never in the chat" in text.lower()
    env.rt._unlock_thread.join(5)
    assert env.rt.status().level == "ACTIVE"


def test_command_status_forget_rules_and_usage(env):
    rules_config(env)
    assert "state: ACTIVE" in env.rt.command("")
    assert "state: ACTIVE" in env.rt.command("status")
    assert "Usage" in env.rt.command("whatever")
    assert "forgotten" in env.rt.command("forget").lower()
    assert "state" in env.rt.command("rules")


def test_command_unlock_without_vault_explains(env):
    rules_config(env)
    assert "not configured" in env.rt.command("unlock").lower()
