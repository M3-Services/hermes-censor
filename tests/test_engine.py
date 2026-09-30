"""Engine: multi-pattern index, priorities, overlaps, encoding variants, cache."""
import json
import time
import unicodedata

from censor_core.engine import Censor
from censor_core.rules import Rule


def R(pattern, replacement="", line=1):
    return Rule(pattern, replacement, line)


def make(rules=(), secrets=(), **kw):
    return Censor.build(rules=list(rules), secrets=list(secrets), **kw)


# ---------- plain rules ----------

def test_rule_replacement_and_deletion():
    c = make([R("fox", "animal"), R("wolf", "")])
    assert c.apply("the fox and the wolf") == "the animal and the "


def test_no_match_returns_same_object():
    c = make([R("fox", "animal")])
    text = "nothing to see here" * 30
    assert c.apply(text) is text


def test_empty_censor_is_identity():
    text = "abc"
    assert make().apply(text) is text


def test_rules_leftmost_then_longest():
    c = make([R("ab", "1"), R("abc", "2"), R("bcd", "3")])
    # "abc" starts at 0 and is the longest at that position; "bcd" overlaps it: dropped
    assert c.apply("abcd") == "2d"


def test_duplicate_patterns_reaching_the_engine_first_rule_wins():
    c1 = make([R("aa", "X", 1), R("aa", "Y", 2)])
    assert c1.apply("aa") == "X"


def test_no_cascading_substitutions():
    c = make([R("a", "b"), R("b", "c")])
    assert c.apply("ab") == "bc"  # not "cc"


def test_replacement_is_not_rescanned_for_patterns():
    c = make([R("fox", "fox-animal")])
    assert c.apply("fox") == "fox-animal"


def test_ignore_case_applies_to_rules_only():
    c = make([R("fox", "animal")], ["Sekret-Value1"], ignore_case=True)
    assert c.apply("FOX Fox fox") == "animal animal animal"
    assert "[SECRET]" in c.apply("xx Sekret-Value1 xx")
    assert c.apply("xx sekret-value1 xx") == "xx sekret-value1 xx"  # secrets are case-sensitive


def test_case_sensitive_by_default():
    c = make([R("fox", "animal")])
    assert c.apply("FOX fox") == "FOX animal"


def test_ignore_case_preserves_offsets_with_length_changing_lowercase():
    # "İ" (U+0130) lowercases to 2 characters: case folding must never shift positions
    c = make([R("abc", "X")], ignore_case=True)
    assert c.apply("İ ABC İ") == "İ X İ"


def test_rule_unicode_nfc_nfd_variants_both_match():
    c = make([R("café", "coffee")])
    nfd = unicodedata.normalize("NFD", "a café")
    assert c.apply(nfd) == "a coffee"
    assert c.apply("a café") == "a coffee"


# ---------- secrets ----------

def test_secret_replaced_with_default_token():
    c = make(secrets=["Sup3r-Secret!"])
    assert c.apply("the password is Sup3r-Secret! ok") == "the password is [SECRET] ok"


def test_secret_replacement_configurable():
    c = make(secrets=["Sup3r-Secret!"], secret_replacement="<masked>")
    assert c.apply("x Sup3r-Secret! y") == "x <masked> y"


def test_secret_beats_overlapping_rule_partial_overlap():
    c = make([R("abc def", "R")], ["def ghi"])
    assert c.apply("abc def ghi") == "abc [SECRET]"


def test_secret_beats_rule_containing_it():
    c = make([R("the word Sekret-1 here", "R")], ["Sekret-1"])
    assert c.apply("the word Sekret-1 here") == "the word [SECRET] here"


def test_secret_inside_rule_span_shorter_rule_still_applies_elsewhere():
    c = make([R("abcdef", "LONG"), R("ab", "AB")], ["def"])
    # "abcdef" overlaps the secret -> dropped; "ab" does not overlap -> applied
    assert c.apply("abcdef") == "ABc[SECRET]"


def test_rule_inside_secret_is_ignored():
    c = make([R("fox", "animal")], ["fox123"])
    assert c.apply("fox123 and fox") == "[SECRET] and animal"


def test_overlapping_secrets_leftmost_longest():
    c = make(secrets=["abcdef", "cdefgh", "abc"])
    assert c.apply("abcdefgh") == "[SECRET]gh"


def test_secret_json_escaped_forms_are_matched_and_json_stays_valid():
    secret = 'Clé-"q"\\z'
    c = make(secrets=[secret])
    for ensure_ascii in (True, False):
        payload = json.dumps({"output": f"pass={secret}"}, ensure_ascii=ensure_ascii)
        out = c.apply(payload)
        assert json.loads(out) == {"output": "pass=[SECRET]"}


def test_rule_json_escaped_variant_uses_escaped_replacement():
    c = make([R('a"b', 'X"Y')])
    assert c.apply('plain a"b') == 'plain X"Y'
    out = c.apply(json.dumps({"k": 'a"b'}))
    assert json.loads(out) == {"k": 'X"Y'}


def test_secret_nfc_nfd_variants():
    secret = "pässwörd-é1"
    c = make(secrets=[secret])
    assert c.apply("x " + unicodedata.normalize("NFD", secret)) == "x [SECRET]"
    assert c.apply("x " + unicodedata.normalize("NFC", secret)) == "x [SECRET]"


def test_multiline_secret_whole_crlf_and_long_lines():
    key = "-----BEGIN KEY-----\nAAAAAAAAAAAAAAAAAAAA1\nBBBBBBBBBBBBBBBBBBBB2\n-----END KEY-----"
    c = make(secrets=[key])
    assert c.apply("k=" + key) == "k=[SECRET]"
    assert c.apply("k=" + key.replace("\n", "\r\n")) == "k=[SECRET]"
    # a single long line (>= 16) of the key leaking on its own is masked too
    assert c.apply("line AAAAAAAAAAAAAAAAAAAA1 alone") == "line [SECRET] alone"
    # short/generic lines (BEGIN/END) are not indexed on their own
    assert c.apply("-----BEGIN KEY-----") == "-----BEGIN KEY-----"


def test_secret_does_not_match_partial_words_but_is_literal_substring():
    # search is literal: a substring is masked (documented)
    c = make(secrets=["abcdef12"])
    assert c.apply("xabcdef12y") == "x[SECRET]y"


# ---------- counting, cache, performance ----------

def test_counts_are_reported():
    c = make([R("fox", "a")], ["Sekret-1"])
    out, n_secret, n_rule = c.apply_counted("fox Sekret-1 fox")
    assert (out, n_secret, n_rule) == ("a [SECRET] a", 1, 2)


def test_cache_returns_same_result_and_skips_rescan():
    c = make([R("fox", "a")], ["Sekret-1"])
    text = ("bla " * 100) + "Sekret-1 fox"
    first = c.apply_counted(text)
    scans = c.stats.scans
    assert c.apply_counted(text) == first
    assert c.stats.scans == scans
    assert c.stats.cache_hits >= 1


def test_cache_is_bounded():
    c = make(secrets=["Sekret-1"], cache_chars=2000)
    for i in range(50):
        c.apply(("x" * 500) + str(i))
    assert c.stats.cache_size_chars <= 2000 + 600


def test_apply_is_deterministic_across_builds():
    rules = [R("ab", "1"), R("bc", "2"), R("abc", "3")]
    outs = {make(rules, ["cde-secret"]).apply("abc cde-secret abc") for _ in range(5)}
    assert len(outs) == 1


def test_performance_1000_secrets_on_1mb():
    import random
    import string
    rnd = random.Random(7)
    secrets = ["".join(rnd.choices(string.ascii_letters + string.digits, k=24)) for _ in range(1000)]
    words = ["".join(rnd.choices(string.ascii_lowercase, k=6)) for _ in range(500)]
    text = " ".join(rnd.choices(words, k=150_000)) + " " + secrets[42] + " end"
    c = make(secrets=secrets)
    t0 = time.perf_counter()
    out = c.apply(text)
    dt = time.perf_counter() - t0
    assert secrets[42] not in out and out.endswith("[SECRET] end")
    assert dt < 3.0, f"too slow: {dt:.2f}s"


def test_min_pattern_length_shortcut():
    c = make(secrets=["Sekret-1"])
    assert c.apply("short") == "short"


# ---------- partial rebuild (rules reloaded without re-reading the secrets, and vice versa) ----------

def test_with_rules_keeps_the_secret_index_untouched():
    base = make([R("fox", "animal")], ["Sekret-Value1"])
    new = base.with_rules([R("wolf", "beast")])
    assert new.apply("wolf fox Sekret-Value1") == "beast fox [SECRET]"
    assert base.apply("wolf fox Sekret-Value1") == "wolf animal [SECRET]"  # the old one stays valid


def test_with_secrets_keeps_the_rule_index_untouched():
    base = make([R("fox", "animal")], ["Sekret-Value1"])
    new = base.with_secrets(["Other-Value-2"])
    assert new.apply("fox Sekret-Value1 Other-Value-2") == "animal Sekret-Value1 [SECRET]"


def test_with_secrets_none_drops_all_secrets():
    base = make([R("fox", "animal")], ["Sekret-Value1"])
    assert base.with_secrets([]).apply("fox Sekret-Value1") == "animal Sekret-Value1"
    assert base.with_secrets([]).stats.secret_patterns == 0
