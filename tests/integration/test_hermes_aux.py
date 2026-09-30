"""Auxiliary calls with ``censor_auxiliary_calls: true``, through the REAL Hermes plugin loader.

The mechanism relies on an UNDOCUMENTED Hermes behaviour (edits made in place to the message dictionaries given to
``pre_auxiliary_call`` reach the provider). These tests are the canary: if a Hermes release changes that, they fail
and docs/LIMITATIONS.md must say the option no longer protects anything.
"""
import asyncio
import os
import sys

import pytest

import harness as H

pytestmark = pytest.mark.hermes

SECRET = "Fictional-Aux-Secret-ABC-987"
RULE_WORD = "Sibelga"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("censor_aux"))
    rules = os.path.join(root, "rules.txt")
    with open(rules, "w", encoding="utf-8") as fh:
        fh.write(f"{RULE_WORD}:AcmeCompany\n")
    home = H.make_hermes_home(root, {"rules_file": rules, "censor_auxiliary_calls": True})
    os.environ["HERMES_HOME"] = home
    from hermes_cli.plugins import get_plugin_manager
    get_plugin_manager().discover_and_load(force=True)
    rt = runtime()
    assert rt is not None, "the plugin was not loaded by Hermes"
    rt._censor = rt._censor.with_secrets([SECRET])  # a secret without needing a vault (same code path as a rule)
    return {"root": root, "home": home, "rt": rt}


def runtime():
    for name, module in list(sys.modules.items()):
        if name.endswith("hermes_censor") and hasattr(module, "get_runtime"):
            return module.get_runtime()
    return None


def aux(provider, task, messages, **kw):
    from agent.auxiliary_client import call_llm
    return call_llm(task, provider="custom", base_url=provider.base_url, api_key="fake-key", model="fake-model",
                    messages=messages, **kw)


def test_the_plugin_registered_the_auxiliary_hooks_and_reports_it(env):
    from hermes_cli.plugins import has_hook
    assert has_hook("pre_auxiliary_call") and has_hook("post_auxiliary_call")
    rt = env["rt"]
    assert rt.aux_hooks_registered
    assert rt.status().level == "ACTIVE", rt.render_status()
    assert "Auxiliary calls: filtered" in rt.render_status()


@pytest.mark.parametrize("task", ["compression", "title_generation", "session_search", "approval"])
def test_auxiliary_request_is_censored_and_the_callers_objects_are_left_intact(env, task):
    provider = H.FakeProvider()
    try:
        msgs = [{"role": "system", "content": "summarise"},
                {"role": "user", "content": f"{RULE_WORD} keeps {SECRET} here"}]
        original = [dict(m) for m in msgs]
        aux(provider, task, msgs)
        body = provider.llm_requests[0]["raw"]
        assert SECRET not in body and RULE_WORD not in body, "an auxiliary call carried the protected value"
        assert "[SECRET]" in body and "AcmeCompany" in body
        assert msgs == original, "the caller's own message objects were left edited"
        assert env["rt"].activity()["aux_requests"] >= 1
    finally:
        provider.close()


def test_every_retry_attempt_is_censored(env):
    provider = H.FakeProvider(lambda i, path, payload: {"status": 500} if i == 0 else None)
    try:
        msgs = [{"role": "user", "content": f"retry {SECRET}"}]
        aux(provider, "compression", msgs)
        assert len(provider.llm_requests) >= 2, "no retry happened: the test proves nothing"
        assert not any(SECRET in b for b in provider.bodies())
        assert msgs[0]["content"] == f"retry {SECRET}"
    finally:
        provider.close()


def test_streaming_auxiliary_call(env):
    provider = H.FakeProvider()
    try:
        msgs = [{"role": "user", "content": f"stream {SECRET}"}]
        list(aux(provider, "moa_aggregator", msgs, stream=True))
        assert SECRET not in provider.llm_requests[0]["raw"] and "[SECRET]" in provider.llm_requests[0]["raw"]
        assert msgs[0]["content"] == f"stream {SECRET}"
    finally:
        provider.close()


def test_async_auxiliary_call(env):
    from agent.auxiliary_client import async_call_llm
    provider = H.FakeProvider()
    try:
        msgs = [{"role": "user", "content": f"async {SECRET}"}]

        async def go():
            return await async_call_llm("compression", provider="custom", base_url=provider.base_url,
                                        api_key="fake-key", model="fake-model", messages=msgs)
        asyncio.run(go())
        assert SECRET not in provider.llm_requests[0]["raw"]
        assert msgs[0]["content"] == f"async {SECRET}"
    finally:
        provider.close()


def test_a_failing_filter_never_breaks_the_auxiliary_call_and_is_reported(env, monkeypatch):
    rt = env["rt"]
    runtime_mod = sys.modules[type(rt).__module__]  # the copy Hermes loaded, not the one pytest imports
    provider = H.FakeProvider()
    monkeypatch.setattr(runtime_mod, "edit_in_place", lambda messages, censor: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        result = aux(provider, "compression", [{"role": "user", "content": f"x {SECRET}"}])
        assert result is not None  # Hermes carried on (fail-open)
        assert rt.status().level == "ERROR"
    finally:
        monkeypatch.undo()
        aux(provider, "compression", [{"role": "user", "content": "clean call"}])
        assert rt.status().level == "ACTIVE", rt.render_status()
        provider.close()


def _selftest_ctx():
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    manager = get_plugin_manager()
    saved = {name: list(manager._hooks.get(name, [])) for name in ("pre_auxiliary_call", "post_auxiliary_call")}
    return PluginContext(PluginManifest(name="censor-selftest-probe"), manager), manager, saved


def _forget_selftest_hooks(manager, saved):
    for name, callbacks in saved.items():
        manager._hooks[name] = callbacks


def test_selftest_passes_on_this_hermes(env):
    from censor_core import selftest
    ctx, manager, saved = _selftest_ctx()
    out = []
    try:
        assert selftest.run(ctx.register_hook, out.append) == 0, "\n".join(out)
    finally:
        _forget_selftest_hooks(manager, saved)
    text = "\n".join(out)
    assert "[PASS] plain call" in text and "[PASS] streaming call" in text and "Result: OK" in text


def test_selftest_notices_when_hermes_stops_sharing_the_message_dicts(env, monkeypatch):
    """The scenario the selftest exists for: a Hermes release that copies the dictionaries handed to the hook."""
    import copy

    import agent.auxiliary_hooks as aux_hooks
    from censor_core import selftest
    real_fire = aux_hooks._fire

    def fire_with_copies(name, **payload):
        if name == "pre_auxiliary_call":
            payload["request_messages"] = copy.deepcopy(payload["request_messages"])
        return real_fire(name, **payload)

    monkeypatch.setattr(aux_hooks, "_fire", fire_with_copies)
    ctx, manager, saved = _selftest_ctx()
    out = []
    try:
        assert selftest.run(ctx.register_hook, out.append) == 1
    finally:
        _forget_selftest_hooks(manager, saved)
    text = "\n".join(out)
    assert "[FAIL] plain call" in text and "UNFILTERED" in text and "Result: FAILED" in text


def test_turning_the_setting_off_stops_the_filtering_without_a_restart(env):
    import yaml
    rt = env["rt"]
    path = os.path.join(env["home"], "config.yaml")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["plugins"]["entries"]["hermes-censor"]["settings"]["censor_auxiliary_calls"] = False
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True)
    rt._t_settings = None
    rt._tick(force=True)
    provider = H.FakeProvider()
    try:
        aux(provider, "compression", [{"role": "user", "content": f"off {SECRET}"}])
        assert SECRET in provider.llm_requests[0]["raw"]  # no longer filtered, and the status says so
        assert "NOT filtered" in rt.render_status()
    finally:
        provider.close()
