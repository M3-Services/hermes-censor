"""Auxiliary calls: in-place edit + undo, the ``censor_auxiliary_calls`` setting, states, no leak.

Only the plugin side is tested here. That Hermes really sends the edited dictionaries is checked against the real
Hermes in ``tests/integration/test_hermes_paths.py`` (and will fail if a Hermes release changes that behaviour).
"""
import copy
import json
import threading

import pytest

from censor_core.auxiliary import MAX_PENDING, AuxiliaryEdits, edit_in_place, restore
from censor_core.config import parse_settings
from censor_core.engine import Censor
from censor_core.rules import Rule
from censor_core.runtime import Runtime

SECRET = "Fictional-Secret-Value-1"
CENSOR = Censor.build(rules=[Rule("fox", "animal", 1)], secrets=[SECRET])
IMG = "data:image/png;base64," + "A" * 400


def shapes():
    return [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": f"the fox keeps {SECRET} here"},
        {"role": "user", "content": [{"type": "text", "text": f"fox {SECRET}"},
                                     {"type": "image_url", "image_url": {"url": IMG}}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "f", "arguments": json.dumps({"p": SECRET})}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": f"out {SECRET}"},
        {"role": "user", "content": "nothing to hide"},
    ]


# ---------------------------------------------------------------------------------------------- edit + undo

def test_edit_changes_the_dicts_themselves_and_undo_puts_everything_back():
    msgs = shapes()
    same_objects = list(msgs)
    before = copy.deepcopy(msgs)
    saved, report = edit_in_place(msgs, CENSOR)
    assert report.changed and saved
    assert all(a is b for a, b in zip(msgs, same_objects))  # the same dict objects, edited in place
    wire = json.dumps(msgs)
    assert SECRET not in wire and "fox" not in wire and "[SECRET]" in wire and "animal" in wire
    assert IMG in wire and '"call_1"' in wire  # binary data and identifiers untouched
    restore(saved)
    assert msgs == before


def test_nothing_to_change_touches_nothing():
    msgs = [{"role": "user", "content": "nothing to hide"}]
    before = copy.deepcopy(msgs)
    saved, report = edit_in_place(msgs, CENSOR)
    assert saved == [] and not report.changed and msgs == before


@pytest.mark.parametrize("not_a_list", [None, "text", {"role": "user"}, 3])
def test_not_a_message_list_is_ignored(not_a_list):
    saved, report = edit_in_place(not_a_list, CENSOR)
    assert saved == [] and not report.changed


def test_non_dict_items_are_skipped_without_error():
    class Opaque:
        content = SECRET

    msgs = [Opaque(), {"role": "user", "content": f"x {SECRET}"}]
    saved, _ = edit_in_place(msgs, CENSOR)
    assert SECRET not in msgs[1]["content"] and msgs[0].content == SECRET
    restore(saved)
    assert msgs[1]["content"] == f"x {SECRET}"


def test_edit_failing_midway_undoes_what_was_done_and_raises():
    class Locked(dict):
        def __setitem__(self, key, value):
            raise TypeError("read-only")

    first = {"role": "user", "content": f"a {SECRET}"}
    msgs = [first, Locked(role="user", content=f"b {SECRET}")]
    with pytest.raises(TypeError):
        edit_in_place(msgs, CENSOR)
    assert first["content"] == f"a {SECRET}"  # the first one was put back


def test_restore_is_best_effort_and_never_raises():
    class Locked(dict):
        def __setitem__(self, key, value):
            raise TypeError("read-only")

    restore([(Locked(a=1), "a", 0), ({}, "k", "v")])


# ---------------------------------------------------------------------------------------------- pending edits

def test_undo_pairs_by_key_and_is_idempotent():
    edits = AuxiliaryEdits()
    a, b = {"c": "changed-a"}, {"c": "changed-b"}
    edits.keep("ka", [(a, "c", "orig-a")])
    edits.keep("kb", [(b, "c", "orig-b")])
    assert edits.undo("ka") == 1 and a["c"] == "orig-a" and b["c"] == "changed-b"
    assert edits.undo("ka") == 0  # already done: nothing happens
    assert edits.undo("unknown") == 0
    assert edits.undo("kb") == 1 and b["c"] == "orig-b" and edits.pending() == 0


def test_pending_is_bounded_and_the_oldest_are_restored():
    edits = AuxiliaryEdits()
    dicts = [{"c": "changed"} for _ in range(MAX_PENDING + 5)]
    for i, d in enumerate(dicts):
        edits.keep(i, [(d, "c", "orig")])
    assert edits.pending() == MAX_PENDING
    assert all(d["c"] == "orig" for d in dicts[:5]) and dicts[-1]["c"] == "changed"


def test_concurrent_attempts_do_not_mix_up():
    edits = AuxiliaryEdits()
    errors = []

    def worker(i):
        for j in range(200):
            msgs = [{"role": "user", "content": f"w{i}-{j} {SECRET}"}]
            original = copy.deepcopy(msgs)
            saved, _ = edit_in_place(msgs, CENSOR)
            edits.keep((i, j), saved)
            if SECRET in msgs[0]["content"]:
                errors.append("not edited")
            edits.undo((i, j))
            if msgs != original:
                errors.append("not restored")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and edits.pending() == 0


# ---------------------------------------------------------------------------------------------- setting + runtime

def test_setting_defaults_to_off_and_validates():
    s, problems = parse_settings({}, "/h")
    assert s.censor_auxiliary_calls is False and not problems
    s, problems = parse_settings({"censor_auxiliary_calls": True}, "/h")
    assert s.censor_auxiliary_calls is True and not problems
    s, problems = parse_settings({"censor_auxiliary_calls": "yes"}, "/h")
    assert s.censor_auxiliary_calls is False and problems
    assert parse_settings({"censor_auxiliary_call": True}, "/h")[1]  # a typo is reported, not silently ignored


@pytest.fixture
def make_runtime(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("fox:animal\n", encoding="utf-8")

    def make(**settings):
        cfg = {"rules_file": str(rules), **settings}
        return Runtime(settings_provider=lambda: dict(cfg), home=str(tmp_path))
    return make


KEY = ("aux-1", 0, 1.0)


def test_runtime_off_by_default_leaves_the_messages_alone(make_runtime):
    rt = make_runtime()
    msgs = [{"role": "user", "content": "the fox"}]
    rt.process_auxiliary_before(msgs, KEY)
    assert msgs[0]["content"] == "the fox" and rt.activity()["aux_requests"] == 0
    assert "NOT filtered" in rt.render_status()


def test_runtime_on_edits_then_gives_the_objects_back(make_runtime):
    rt = make_runtime(censor_auxiliary_calls=True)
    rt.aux_hooks_registered = True
    msgs = [{"role": "user", "content": "the fox"}]
    rt.process_auxiliary_before(msgs, KEY)
    assert msgs[0]["content"] == "the animal"
    a = rt.activity()
    assert a["aux_requests"] == 1 and a["aux_last_rule_replacements"] == 1 and a["aux_total_rule_replacements"] == 1
    rt.process_auxiliary_after(KEY)
    assert msgs[0]["content"] == "the fox"
    assert rt.status().level == "ACTIVE"
    text = rt.render_status()
    assert "Auxiliary calls: filtered" in text and "AUXILIARY_NOTICE" in text


def test_runtime_undo_works_even_if_the_setting_was_turned_off_meanwhile(make_runtime, tmp_path):
    cfg = {"rules_file": str(tmp_path / "rules.txt"), "censor_auxiliary_calls": True}
    (tmp_path / "rules.txt").write_text("fox:animal\n", encoding="utf-8")
    rt = Runtime(settings_provider=lambda: dict(cfg), home=str(tmp_path))
    msgs = [{"role": "user", "content": "the fox"}]
    rt.process_auxiliary_before(msgs, KEY)
    cfg["censor_auxiliary_calls"] = False
    rt._t_settings = None
    rt.process_auxiliary_after(KEY)
    assert msgs[0]["content"] == "the fox"


def test_runtime_disabled_plugin_does_nothing(make_runtime):
    rt = make_runtime(censor_auxiliary_calls=True, enabled=False)
    msgs = [{"role": "user", "content": "the fox"}]
    rt.process_auxiliary_before(msgs, KEY)
    assert msgs[0]["content"] == "the fox"


def test_setting_on_but_hooks_not_registered_is_degraded_with_a_repair(make_runtime):
    rt = make_runtime(censor_auxiliary_calls=True)  # aux_hooks_registered stays False: loaded with the setting off
    st = rt.status()
    assert st.level == "DEGRADED"
    finding = next(f for f in st.findings if f.code == "AUX_RESTART_REQUIRED")
    assert "Restart Hermes" in finding.repair


def test_failure_is_fail_open_reported_as_error_and_never_raises(make_runtime, monkeypatch, caplog):
    rt = make_runtime(censor_auxiliary_calls=True)
    rt.aux_hooks_registered = True
    from censor_core import runtime as runtime_mod

    def boom(messages, censor):
        raise RuntimeError(f"cannot edit {SECRET}")  # the message must never reach the logs or the status

    monkeypatch.setattr(runtime_mod, "edit_in_place", boom)
    msgs = [{"role": "user", "content": "the fox"}]
    with caplog.at_level("WARNING", logger="hermes-censor"):
        rt.process_auxiliary_before(msgs, KEY)  # no exception
    st = rt.status()
    assert st.level == "ERROR" and "AUX_REQUEST_FAILED_OPEN" in {f.code for f in st.findings}
    assert rt.activity()["aux_failopen_count"] == 1
    assert SECRET not in caplog.text and SECRET not in rt.render_status(verbose=True)
    # the next auxiliary call that goes through cleanly brings the state back
    monkeypatch.undo()
    rt.process_auxiliary_before([{"role": "user", "content": "the fox"}], ("aux-2", 0, 2.0))
    assert rt.status().level == "ACTIVE"


def test_no_value_in_status_or_logs(make_runtime, caplog):
    rt = make_runtime(censor_auxiliary_calls=True)
    rt.aux_hooks_registered = True
    rt._censor = rt._censor.with_secrets([SECRET])
    msgs = [{"role": "user", "content": f"fox {SECRET}"}]
    with caplog.at_level("DEBUG", logger="hermes-censor"):
        rt.process_auxiliary_before(msgs, KEY)
        text = rt.render_status(verbose=True)
    assert SECRET not in text and SECRET not in caplog.text
    assert SECRET not in msgs[0]["content"]
    rt.process_auxiliary_after(KEY)
    assert msgs[0]["content"] == f"fox {SECRET}"
