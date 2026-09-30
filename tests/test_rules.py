"""Parser for the plain rules file: ``<pattern>:<replacement>``."""
from censor_core.rules import parse_rules


def only(text):
    result = parse_rules(text)
    assert not result.issues, result.issues
    return [(r.pattern, r.replacement) for r in result.rules]


def test_replacement_and_deletion():
    assert only("fox:animal\nwolf:\n") == [("fox", "animal"), ("wolf", "")]


def test_split_on_first_unescaped_colon_only():
    assert only("a:b:c") == [("a", "b:c")]


def test_escaped_colon_in_pattern_and_replacement():
    assert only(r"C\:\\Users:x") == [("C:\\Users", "x")]
    assert only(r"a:b\:c") == [("a", "b:c")]


def test_other_backslashes_are_literal():
    # only \: \\ and \# (start of line) are escapes
    assert only(r"C\:\new\dir:x") == [("C:\\new\\dir", "x")]
    assert only("a:b\\") == [("a", "b\\")]


def test_blank_and_whitespace_only_lines_ignored():
    assert only("\n   \n\t\nfox:a\n") == [("fox", "a")]


def test_comments_only_at_column_zero():
    assert only("# comment\nfox:a\n") == [("fox", "a")]
    assert only(r"\#tag:x") == [("#tag", "x")]
    # a "#" preceded by a space is NOT a comment
    assert only(" #tag:x") == [(" #tag", "x")]


def test_whitespace_is_significant_but_crlf_is_not():
    assert only("fox :a b\r\nwolf:c \r\n") == [("fox ", "a b"), ("wolf", "c ")]


def test_utf8_bom_and_unicode_are_preserved_without_normalization():
    nfd = "cafe\u0301"  # e + combining accent
    assert only("\ufeff" + nfd + ":x") == [(nfd, "x")]


def test_empty_pattern_is_an_error_with_line_number():
    result = parse_rules("ok:1\n:empty\n")
    assert [(i.line, i.code, i.severity) for i in result.issues] == [(2, "EMPTY_PATTERN", "error")]
    assert [r.pattern for r in result.rules] == ["ok"]


def test_missing_separator_is_an_error():
    result = parse_rules("no colon here\nok:1")
    assert [(i.line, i.code) for i in result.issues] == [(1, "MISSING_SEPARATOR")]
    assert [r.pattern for r in result.rules] == ["ok"]


def test_identical_duplicates_are_silent_and_kept_once():
    result = parse_rules("a:1\na:1\n")
    assert not result.issues
    assert len(result.rules) == 1


def test_conflicting_duplicates_first_wins_warning():
    result = parse_rules("a:1\nb:2\na:3\n")
    assert [(r.pattern, r.replacement) for r in result.rules] == [("a", "1"), ("b", "2")]
    assert [(i.line, i.code, i.severity) for i in result.issues] == [(3, "CONFLICT", "warning")]


def test_issue_messages_never_echo_rule_content():
    result = parse_rules("absolutelysecretword\n:absolutelysecretword2\n")
    for issue in result.issues:
        assert "absolutelysecretword" not in issue.message


def test_rule_records_its_source_line():
    result = parse_rules("# c\n\nfox:a\n")
    assert result.rules[0].line == 3
