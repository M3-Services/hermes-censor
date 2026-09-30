"""Parser for the plain rules file (one rule per line: ``<pattern>:<replacement>``).

Syntax (see docs/RULES.md):
- the separator is the first unescaped ":"; the replacement is the rest of the line (it may contain ":")
- recognised escapes: ``\\:``, ``\\\\`` and ``\\#`` at the very start of a line; any other "\\" is literal
- blank or whitespace-only lines are ignored; a line starting with "#" (column 0) is a comment
- whitespace is significant; only the trailing "\\r" is stripped; no Unicode normalisation
- issue messages never quote the content of a rule (line number and code only)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


_BOM = chr(0xFEFF)  # optional UTF-8 byte order mark at the start of the file


@dataclass(frozen=True)
class Rule:
    pattern: str
    replacement: str
    line: int


@dataclass(frozen=True)
class RuleIssue:
    line: int
    code: str  # MISSING_SEPARATOR | EMPTY_PATTERN | CONFLICT | UNREADABLE
    severity: str  # "error" | "warning"
    message: str


@dataclass
class ParseResult:
    rules: List[Rule] = field(default_factory=list)
    issues: List[RuleIssue] = field(default_factory=list)


def _split(line: str):
    """Return (pattern, replacement), or None when there is no unescaped separator."""
    pattern: List[str] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] in ":\\":
            pattern.append(line[i + 1])
            i += 2
        elif ch == ":":
            return "".join(pattern), _unescape(line[i + 1:])
        else:
            pattern.append(ch)
            i += 1
    return None


def _unescape(text: str) -> str:
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n and text[i + 1] in ":\\":
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def parse_rules(text: str) -> ParseResult:
    result = ParseResult()
    seen = {}  # pattern -> retained Rule
    if text.startswith(_BOM):
        text = text[1:]
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("\\#"):
            line = line[1:]
        parts = _split(line)
        if parts is None:
            result.issues.append(RuleIssue(number, "MISSING_SEPARATOR", "error",
                                           f"line {number}: missing ':' separator, rule ignored"))
            continue
        pattern, replacement = parts
        if pattern == "":
            result.issues.append(RuleIssue(number, "EMPTY_PATTERN", "error",
                                           f"line {number}: empty pattern, rule ignored"))
            continue
        previous = seen.get(pattern)
        if previous is not None:
            if previous.replacement != replacement:
                result.issues.append(RuleIssue(
                    number, "CONFLICT", "warning",
                    f"line {number}: same pattern as line {previous.line} with a different replacement; "
                    f"the first rule is kept"))
            continue
        rule = Rule(pattern, replacement, number)
        seen[pattern] = rule
        result.rules.append(rule)
    return result
