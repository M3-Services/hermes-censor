"""Selection and value policy from the KeePass XML export (pure, no keepassxc-cli)."""
import json

import pytest

from censor_core.kpx_helper import HelperError, select_secrets
from kpx_fixtures import FICTIVE_TREE, E, G, xml_for

XML = xml_for(FICTIVE_TREE)


def sel(**kw):
    return select_secrets(XML, kw)


def values(**kw):
    return set(sel(**kw)["values"])


def test_group_selection_is_recursive_by_default_and_policy_applies():
    r = sel(groups=["Hermes"])
    assert set(r["values"]) == {
        "FictionalPassword-AAA", "fictional-api-key-BBB-2222", "CurrentValue-777", "DeepSecret-999"}
    rep = r["report"]
    assert rep["skipped_empty"] == 1          # "Empty"
    assert rep["skipped_short"] == 1          # "Short" (< 6)
    assert rep["duplicates"] == 1             # "Duplicate"
    assert rep["short_fields"] == ["Hermes/Short:Password"]
    assert rep["not_found"] == []


def test_non_recursive_group_excludes_subgroups():
    assert "DeepSecret-999" not in values(groups=["Hermes"], recursive=False)


def test_subgroup_path_and_root_path():
    assert values(groups=["Hermes/Sub"]) == {"DeepSecret-999"}
    assert values(groups=["/"], recursive=False) == {"RootSecret-001"}
    assert values(groups=[""], recursive=False) == {"RootSecret-001"}


def test_paths_are_relative_to_the_root_group_name():
    r = sel(groups=["Root/Hermes"])
    assert r["values"] == []
    assert r["report"]["not_found"] == ["Root/Hermes"]


def test_individual_entries():
    assert values(entries=["Hermes/Server"]) == {"FictionalPassword-AAA", "fictional-api-key-BBB-2222"}
    assert sel(entries=["Hermes/Missing"])["report"]["not_found"] == ["Hermes/Missing"]


def test_exclude_entries_and_groups():
    v = values(groups=["Hermes"], exclude=["Hermes/Server", "Hermes/Duplicate", "Hermes/Sub"])
    assert v == {"CurrentValue-777"}


def test_select_all_skips_recycle_bin_unless_asked():
    v = values(select_all=True)
    assert "OtherSecret-555" in v and "RootSecret-001" in v and "DiscardedSecret-333" not in v
    assert "DiscardedSecret-333" in values(select_all=True, include_recycle_bin=True)


def test_history_is_off_by_default():
    assert "OldValue-666" not in values(groups=["Hermes"])
    assert "OldValue-666" in values(groups=["Hermes"], include_history=True)


def test_explicit_fields_and_min_length():
    assert values(groups=["Hermes"], fields=["UserName"], min_length=3) == {"alice"}
    assert sel(groups=["Hermes"], fields=["UserName"])["report"]["skipped_short"] >= 1  # "alice" < 6


def test_only_protected_fields():
    assert values(groups=["Hermes"], fields=["@protected"]) == {
        "FictionalPassword-AAA", "fictional-api-key-BBB-2222", "CurrentValue-777", "DeepSecret-999"}


def test_empty_selection_is_an_error():
    with pytest.raises(HelperError) as exc:
        select_secrets(XML, {})
    assert exc.value.code == "empty_selection"


def test_report_never_contains_values():
    r = sel(groups=["Hermes"], entries=["Hermes/Manquant"])
    blob = json.dumps(r["report"])
    for v in r["values"]:
        assert v not in blob


def test_xml_escapes_and_unicode_roundtrip():
    tree = G("R", entries=[E("t", {"Password": 'Clé-é&"<>\'x', "Notes": "n"})])
    r = select_secrets(xml_for(tree), {"select_all": True})
    assert r["values"] == ['Clé-é&"<>\'x']


def test_invalid_xml_is_a_clean_error():
    with pytest.raises(HelperError) as exc:
        select_secrets(b"<not xml", {"select_all": True})
    assert exc.value.code == "xml_invalid"
