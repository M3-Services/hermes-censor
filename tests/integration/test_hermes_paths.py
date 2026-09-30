"""Real LLM call paths: what the fake provider RECEIVES (raw bytes), with the real Hermes.

Each test feeds the docs/CALL_PATHS.md matrix. "Covered" = the protected literal string does not appear in the
received bytes; "not covered" = it does (an experimental finding, not an assumption).
"""
import json
import os
import time

import pytest

import harness as H

pytestmark = pytest.mark.hermes

SECRET = "Fictional-Tool-Secret-XYZ-123"
RULE_WORD = "fox"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from kpx_fixtures import E, G, build_kdbx, find_cli
    cli = find_cli()
    if cli is None:
        pytest.skip("keepassxc-cli introuvable")
    root = str(tmp_path_factory.mktemp("censor_it"))
    rules = os.path.join(root, "rules.txt")
    with open(rules, "w", encoding="utf-8") as fh:
        fh.write("fox:animal\n")
    key = os.path.join(root, "cle.keyx")
    db = build_kdbx(cli, os.path.join(root, "fictional.kdbx"),
                    G("R", groups=[G("Hermes", entries=[E("Tool", {"Password": SECRET})])]), keyfile=key)
    settings = {"rules_file": rules,
                "keepassxc": {"database": db, "keyfile": key, "unlock": "keyfile-only", "cli": [cli],
                              "groups": ["Hermes"]}}
    home = H.make_hermes_home(root, settings)
    os.environ["HERMES_HOME"] = home
    os.environ["CENSOR_TEST_TOOL_VALUE"] = SECRET
    from hermes_cli.plugins import get_plugin_manager
    get_plugin_manager().discover_and_load(force=True)  # (re)load the test profile's plugins (the plugin must be present)
    assert plugin_runtime() is not None, "the plugin was not loaded by Hermes"
    return {"root": root, "home": home, "rules": rules, "db": db, "key": key, "cli": cli}


def new_agent(base_url, **kw):
    from run_agent import AIAgent
    args = dict(base_url=base_url, api_key="fake-key", provider="custom", model="fake-model",
                enabled_toolsets=["censor_test"], quiet_mode=True, skip_memory=True, skip_context_files=True,
                platform="cli", max_iterations=6)
    args.update(kw)
    return AIAgent(**args)


def plugin_runtime():
    import sys
    for name, module in list(sys.modules.items()):
        if name.endswith("hermes_censor") and hasattr(module, "get_runtime"):
            return module.get_runtime()
    return None


def test_main_loop_tool_result_and_history_are_censored(env):
    """Main loop: the fictional value (tool result) never reaches the provider, nor the local history."""
    provider = H.FakeProvider(lambda i, path, payload:
                              H.chat_tool_call("vault_show") if i == 0 else H.chat_reply({"content": "done"}))
    try:
        agent = new_agent(provider.base_url)
        result = agent.run_conversation("the fox must call vault_show")
        assert result["final_response"] == "done"
        assert len(provider.llm_requests) == 2
        rt = plugin_runtime()
        assert rt is not None, "the plugin was not loaded by Hermes"
        assert rt.status().level == "ACTIVE", rt.render_status()
        for body in provider.bodies():
            assert SECRET not in body, "fictional value received by the provider"
        assert "animal" in provider.bodies()[0]
        tool_msgs = [m for m in provider.llm_requests[1]["payload"]["messages"] if m.get("role") == "tool"]
        assert tool_msgs and "[SECRET]" in tool_msgs[0]["content"]
        stored = [m for m in result["messages"] if m.get("role") == "tool"]
        assert stored and SECRET not in json.dumps(stored)  # transform_tool_result: history already censored
    finally:
        provider.close()


def _set_plugin_setting(env, **changes):
    """Change the plugin settings in the TEST profile's config.yaml and force them to be re-read."""
    import yaml
    path = os.path.join(env["home"], "config.yaml")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["plugins"]["entries"]["hermes-censor"]["settings"].update(changes)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True)
    rt = plugin_runtime()
    rt._t_settings = None  # immediate re-read (otherwise: within 2 s)
    rt._tick(force=True)
    return rt


def test_control_with_plugin_disabled_the_secret_DOES_reach_the_provider(env):
    """Control: proves the main test discriminates (without censoring, the fictional value does leak)."""
    rt = _set_plugin_setting(env, enabled=False)
    provider = H.FakeProvider(lambda i, path, payload:
                              H.chat_tool_call("vault_show") if i == 0 else H.chat_reply({"content": "done"}))
    try:
        agent = new_agent(provider.base_url)
        agent.run_conversation("the fox must call vault_show")
        assert any(SECRET in body for body in provider.bodies()), "control: the value should have leaked without the plugin"
        assert rt.status().level == "INACTIVE"
    finally:
        provider.close()
        _set_plugin_setting(env, enabled=True)


@pytest.fixture(params=[True, False], ids=["plugin-active", "control-without-plugin"])
def protected(request, env):
    """Each path runs twice: with the plugin (no leak) and without (the leak MUST appear)."""
    _set_plugin_setting(env, enabled=request.param)
    yield request.param
    _set_plugin_setting(env, enabled=True)


def _check(provider, protected):
    """Protected: the fictional value appears in no received body. Control: it appears (the test discriminates)."""
    if protected:
        for body in provider.bodies():
            assert SECRET not in body, "fictional value received by the provider"
        assert any("[SECRET]" in body for body in provider.bodies())
    else:
        assert any(SECRET in body for body in provider.bodies()), "control: the value should have leaked without the plugin"


# ---------- retries ----------

def test_retry_after_provider_error_is_censored_on_every_attempt(env, protected):
    def script(i, path, payload):
        if i == 0:
            return H.chat_tool_call("vault_show")
        if i == 1:
            return {"status": 500}          # first attempt carrying the tool result: server error
        return H.chat_reply({"content": "done"})

    provider = H.FakeProvider(script)
    try:
        agent = new_agent(provider.base_url)
        result = agent.run_conversation("call vault_show")
        assert result["final_response"] == "done"
        assert len(provider.llm_requests) >= 3, "no retry observed"
        _check(provider, protected)
        # both attempts carrying the tool result are censored
        with_tool = [b for b in provider.bodies() if '"role":"tool"' in b or '"role": "tool"' in b]
        assert len(with_tool) >= 2
        assert all(("[SECRET]" in b) == protected for b in with_tool)
    finally:
        provider.close()


# ---------- fallback provider ----------

def test_fallback_provider_receives_censored_request(env, protected):
    fallback = H.FakeProvider(lambda i, path, payload: H.chat_reply({"content": "done via fallback"}))

    def script(i, path, payload):
        if i == 0:
            return H.chat_tool_call("vault_show")
        return {"status": 429, "json": {"error": {"message": "quota factice", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}}

    primary = H.FakeProvider(script)
    try:
        agent = new_agent(primary.base_url, fallback_model={
            "provider": "custom", "model": "fake-fallback", "base_url": fallback.base_url, "api_key": "fake-fb-key"})
        result = agent.run_conversation("call vault_show")
        assert result["final_response"] == "done via fallback", result["final_response"]
        assert len(fallback.llm_requests) >= 1, "the fallback was not used"
        _check(primary, protected)
        _check(fallback, protected)
    finally:
        primary.close()
        fallback.close()


# ---------- subagent ----------

def test_subagent_requests_are_censored(env, protected):
    def script(i, path, payload):
        if i == 0:
            args = json.dumps({"tasks": [{"goal": f"repeat this password: {SECRET}", "context": f"ctx {SECRET}"}]})
            return H.chat_tool_call("delegate_task", args)
        msgs = json.dumps(payload.get("messages", []))
        if "repeat this password" in msgs and '"role": "tool"' not in msgs:
            return H.chat_reply({"content": "subagent finished"})
        return H.chat_reply({"content": "parent finished"})

    provider = H.FakeProvider(script)
    try:
        agent = new_agent(provider.base_url, enabled_toolsets=["delegation", "censor_test"])
        result = agent.run_conversation("delegate a task")
        assert result["final_response"] == "parent finished", result["final_response"]
        # delegate_task is asynchronous: the subagent runs in a thread, its request may arrive after the parent's
        deadline = time.time() + 20
        child = None
        while time.time() < deadline and child is None:
            for r in list(provider.llm_requests):
                msgs = r["payload"].get("messages", [])
                if msgs and msgs[-1].get("role") == "user" and "repeat this password" in str(msgs[-1].get("content"))                         and not any(m.get("tool_calls") for m in msgs):
                    child = r
            time.sleep(0.2)
        assert child is not None, "no subagent request observed"
        assert (SECRET in child["raw"]) == (not protected), "subagent request"
        _check(provider, protected)
    finally:
        provider.close()


# ---------- auxiliary calls (compression, title, vision...): path NOT covered ----------

def _aux_call(provider, text):
    from agent.auxiliary_client import call_llm
    return call_llm("compression", provider="custom", base_url=provider.base_url, api_key="fake-key",
                    model="fake-model", messages=[{"role": "user", "content": text}])


def test_auxiliary_calls_do_NOT_traverse_llm_request(env):
    """Characterisation: an auxiliary call (here "compression") is NOT censored by llm_request.

    If this test ever fails (the value no longer reaches the provider), Hermes has extended the middleware to
    auxiliary calls: update docs/CALL_PATHS.md.
    """
    rt = _set_plugin_setting(env, enabled=True)
    assert rt.status().level == "ACTIVE", rt.render_status()
    provider = H.FakeProvider()
    try:
        _aux_call(provider, f"summarise this: the fox knows {SECRET}")
        assert provider.llm_requests, "no auxiliary call received"
        body = provider.llm_requests[0]["raw"]
        assert SECRET in body, "the auxiliary call was censored: the matrix must be updated"
        assert "fox" in body
    finally:
        provider.close()


def test_experiment_pre_auxiliary_call_hook_is_observer_only(env):
    """Experiment (not shipped): can the official pre_auxiliary_call hook serve as a complementary protection?

    Finding recorded in docs/CALL_PATHS.md: we observe the real result and rely on no undocumented side effect.
    """
    from hermes_cli.plugins import get_plugin_manager
    seen = {}

    def hook(**kwargs):
        seen["fired"] = True
        for m in kwargs.get("request_messages") or []:
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                m["content"] = m["content"].replace(SECRET, "[MUTATION-EN-PLACE]")
        return "REPLACEMENT-IGNORE"

    manager = get_plugin_manager()
    manager._hooks.setdefault("pre_auxiliary_call", []).append(hook)
    provider = H.FakeProvider()
    try:
        _aux_call(provider, f"text with {SECRET}")
        body = provider.llm_requests[0]["raw"]
        seen["mutated_in_place_reached_provider"] = "MUTATION-EN-PLACE" in body
        print("EXPERIMENT", seen)
        assert seen.get("fired"), "pre_auxiliary_call did not fire"
        # Hermes v0.21.5 finding (undocumented side effect: the list is copied, not the dictionaries)
        assert seen["mutated_in_place_reached_provider"] is True
    finally:
        manager._hooks["pre_auxiliary_call"].remove(hook)
        provider.close()


# ---------- other API formats ----------

def test_responses_api_path(env, protected):
    def script(i, path, payload):
        if i == 0:
            return H.responses_reply("", tool_call=("vault_show", "{}", "call_1"))
        return H.responses_reply("done responses")

    provider = H.FakeProvider(script)
    try:
        agent = new_agent(provider.base_url, api_mode="codex_responses")
        result = agent.run_conversation("the fox must call vault_show")
        assert result["final_response"] == "done responses", result["final_response"]
        assert all(r["path"].endswith("/responses") for r in provider.llm_requests)
        assert len(provider.llm_requests) >= 2
        _check(provider, protected)
    finally:
        provider.close()


def test_anthropic_messages_path(env, protected):
    pytest.importorskip("anthropic", reason="anthropic SDK (version pinned by Hermes: 0.87.0) not installed")
    def script(i, path, payload):
        if i == 0:
            return H.anthropic_reply("", tool_use=("vault_show", {}, "toolu_1"))
        return H.anthropic_reply("done anthropic")

    provider = H.FakeProvider(script)
    try:
        agent = new_agent(provider.anthropic_url, provider="anthropic", api_mode="anthropic_messages")
        result = agent.run_conversation("the fox must call vault_show")
        assert result["final_response"] == "done anthropic", result["final_response"]
        assert all(r["path"].endswith("/messages") for r in provider.llm_requests)
        assert len(provider.llm_requests) >= 2
        _check(provider, protected)
    finally:
        provider.close()


# ---------- real fail-open and its reporting ----------

def _recover(rt):
    provider = H.FakeProvider(lambda i, path, payload: H.chat_reply({"content": "ok"}))
    try:
        new_agent(provider.base_url).run_conversation("bonjour")
        assert rt.status().level == "ACTIVE", rt.render_status()
    finally:
        provider.close()


def test_failopen_is_real_and_is_detected(env):
    """A plugin exception lets the request GO OUT uncensored (documented fail-open), but the state becomes ERROR."""
    import sys
    # second layer switched off to isolate the middleware: otherwise transform_tool_result would already have censored the tool result
    rt = _set_plugin_setting(env, enabled=True, censor_tool_results=False)
    runtime_module = next(m for n, m in sys.modules.items() if n.endswith("censor_core.runtime"))
    real = runtime_module.censor_request

    def boom(request, censor):
        raise RuntimeError("simulated failure")

    provider = H.FakeProvider(lambda i, path, payload:
                              H.chat_tool_call("vault_show") if i == 0 else H.chat_reply({"content": "done"}))
    runtime_module.censor_request = boom
    try:
        agent = new_agent(provider.base_url)
        result = agent.run_conversation("call vault_show")
        assert result["final_response"] == "done"                      # the agent carries on normally
        assert any(SECRET in b for b in provider.bodies())             # ... but the request went out UNCENSORED
        st = rt.status()
        assert st.level == "ERROR" and "REQUEST_FAILED_OPEN" in {f.code for f in st.findings}
        assert "RuntimeError" in rt.render_status() and "simulated failure" not in rt.render_status()
    finally:
        runtime_module.censor_request = real
        provider.close()
        _set_plugin_setting(env, censor_tool_results=True)
    _recover(rt)                                                       # the state recovers as soon as a request goes through


# ---------- Hermes surfaces: slash command and CLI ----------

def test_slash_command_is_registered_and_reports_state(env):
    from hermes_cli.plugins import get_plugin_commands
    _set_plugin_setting(env, enabled=True)
    cmd = get_plugin_commands().get("censor")
    assert cmd is not None, "/censor command not registered"
    text = cmd["handler"]("status")
    assert "Hermes Censor" in text and "state: ACTIVE" in text
    assert SECRET not in text
    assert "Usage" in cmd["handler"]("help")


def test_cli_subcommand_is_registered_and_builds_its_parser(env):
    import argparse
    from hermes_cli.plugins import get_plugin_manager
    entry = get_plugin_manager()._cli_commands.get("censor")
    assert entry is not None, "\"hermes censor\" subcommand not registered"
    parser = argparse.ArgumentParser()
    entry["setup_fn"](parser)
    args = parser.parse_args(["status", "-v"])
    assert args.censor_cmd == "status" and args.verbose is True
    assert parser.parse_args(["check-rules", "x.txt"]).file == "x.txt"


def test_status_text_has_no_secret_after_real_traffic(env):
    rt = plugin_runtime()
    text = rt.render_status(verbose=True)
    assert SECRET not in text
    assert "Exact scope read" in text and "Hermes" in text


def test_defense_in_depth_tool_result_layer_survives_a_middleware_failure(env):
    """transform_tool_result censors the result BEFORE the history: a middleware failure does not leak a value that
    came from a tool (a secret typed by the user, however, would leak: see LIMITATIONS.md)."""
    import sys
    rt = _set_plugin_setting(env, enabled=True, censor_tool_results=True)
    runtime_module = next(m for n, m in sys.modules.items() if n.endswith("censor_core.runtime"))
    real = runtime_module.censor_request

    def boom(request, censor):
        raise RuntimeError("simulated failure")

    provider = H.FakeProvider(lambda i, path, payload:
                              H.chat_tool_call("vault_show") if i == 0 else H.chat_reply({"content": "done"}))
    runtime_module.censor_request = boom
    try:
        new_agent(provider.base_url).run_conversation("call vault_show")
        assert not any(SECRET in b for b in provider.bodies())
        assert rt.status().level == "ERROR"      # the middleware failure is still reported
    finally:
        runtime_module.censor_request = real
        provider.close()
    _recover(rt)


# ---------- locked then unlocked vault ("prompt" mode + askpass), in the real Hermes ----------

def test_locked_vault_leaks_but_warns_then_unlock_command_protects(env, caplog):
    import logging
    import sys
    from kpx_fixtures import E, G, build_kdbx
    master = "Fictional-Master-IT-1"
    db = build_kdbx(env["cli"], os.path.join(env["root"], "prompt.kdbx"),
                    G("R", groups=[G("Hermes", entries=[E("Tool", {"Password": SECRET})])]), password=master)
    askpass = os.path.join(env["root"], "askpass.py")
    with open(askpass, "w", encoding="utf-8") as fh:
        fh.write(f"import sys; sys.stdout.buffer.write({master!r}.encode('utf-8'))")
    original = {"database": env["db"], "keyfile": env["key"], "unlock": "keyfile-only", "cli": [env["cli"]],
                "groups": ["Hermes"]}
    rt = _set_plugin_setting(env, keepassxc={"database": db, "cli": [env["cli"]], "groups": ["Hermes"],
                                             "askpass": [sys.executable, askpass]})
    from hermes_cli.plugins import get_plugin_commands
    handler = get_plugin_commands()["censor"]["handler"]
    script = lambda i, path, payload: (H.chat_tool_call("vault_show") if i == 0 else H.chat_reply({"content": "done"}))
    try:
        # 1) locked: the tool result value is NOT protected; the state says so and a warning is logged
        provider = H.FakeProvider(script)
        with caplog.at_level(logging.WARNING, logger="hermes-censor"):
            new_agent(provider.base_url).run_conversation("call vault_show")
        assert any(SECRET in b for b in provider.bodies())
        st = rt.status()
        assert st.level == "DEGRADED" and "SECRETS_NOT_LOADED" in {f.code for f in st.findings}
        assert any("DEGRADED" in r.getMessage() for r in caplog.records), "warning not logged"
        provider.close()
        # 2) /censor unlock: returns immediately, local prompt simulated by askpass
        text = handler("unlock")
        assert "window" in text.lower() and master not in text
        rt._unlock_thread.join(30)
        assert rt.status().level == "ACTIVE", rt.render_status()
        # 3) unlocked: no more leak
        provider = H.FakeProvider(script)
        new_agent(provider.base_url).run_conversation("call vault_show")
        assert not any(SECRET in b for b in provider.bodies()) and any("[SECRET]" in b for b in provider.bodies())
        provider.close()
        assert master not in caplog.text
    finally:
        _set_plugin_setting(env, keepassxc=original)
        _recover(rt)
