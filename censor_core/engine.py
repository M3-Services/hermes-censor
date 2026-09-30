"""Filtering engine: multi-pattern index (Aho-Corasick, pure Python, no dependency) plus conflict resolution.

This module knows nothing about Hermes or KeePassXC: it receives rules (``rules.Rule``) and a list of
secret values (``str``) and transforms strings.

Semantics (deterministic, a single pass over the original text, never any cascading):
1. all patterns (secrets and rules) are searched in the ORIGINAL text;
2. secrets: leftmost first, then longest; overlapping matches are dropped;
3. rules: any match overlapping a retained secret is dropped (the secret wins), then leftmost / longest /
   file order;
4. replacement text is never rescanned.

Each secret is indexed under several forms (raw, NFC, NFD, JSON-escaped ASCII and non-ASCII) because Hermes
tool results are often JSON strings; rules follow the same variants, with the replacement escaped the same way
so that JSON stays valid.
"""
from __future__ import annotations

import json
import threading
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .rules import Rule

DEFAULT_SECRET_REPLACEMENT = "[SECRET]"
LINE_MIN_LENGTH = 16  # lines of a multi-line secret are indexed on their own from this length
CACHE_MIN_TEXT = 256  # shorter texts are scanned every time (negligible cost)
DEFAULT_CACHE_CHARS = 16_000_000


class _Fold(dict):
    """1-to-1 case folding (never changes the length) for case-insensitive search."""

    def __missing__(self, ch):
        low = ch.lower()
        value = low if len(low) == 1 else ch
        self[ch] = value
        return value


class Automaton:
    """Aho-Corasick: unique patterns -> integer payload; ``find_all`` returns every occurrence."""

    def __init__(self, patterns: Iterable[Tuple[str, int]], ignore_case: bool = False):
        self._fold = _Fold() if ignore_case else None
        goto: List[Dict[str, int]] = [{}]
        out: List[Optional[Tuple[int, int]]] = [None]
        fail: List[int] = [0]
        self.min_len = 0
        for pattern, payload in patterns:
            if not pattern:
                continue
            key = "".join(self._fold[c] for c in pattern) if self._fold is not None else pattern
            state = 0
            for ch in key:
                nxt = goto[state].get(ch)
                if nxt is None:
                    goto.append({})
                    out.append(None)
                    fail.append(0)
                    nxt = len(goto) - 1
                    goto[state][ch] = nxt
                state = nxt
            if out[state] is None:  # the first pattern wins
                out[state] = (len(pattern), payload)
                self.min_len = len(pattern) if not self.min_len else min(self.min_len, len(pattern))
        queue = deque(goto[0].values())
        while queue:
            r = queue.popleft()
            for ch, s in goto[r].items():
                queue.append(s)
                f = fail[r]
                while f and ch not in goto[f]:
                    f = fail[f]
                target = goto[f].get(ch, 0)
                fail[s] = target if target != s else 0
        dict_link = [0] * len(goto)
        order = deque(goto[0].values())
        while order:  # BFS: fail[s] is always computed before s
            r = order.popleft()
            f = fail[r]
            dict_link[r] = f if out[f] is not None else dict_link[f]
            order.extend(goto[r].values())
        self._goto, self._fail, self._out, self._dl = goto, fail, out, dict_link

    def find_all(self, text: str) -> List[Tuple[int, int, int]]:
        goto, fail, out, dl = self._goto, self._fail, self._out, self._dl
        stream = map(self._fold.__getitem__, text) if self._fold is not None else text
        state = 0
        found: List[Tuple[int, int, int]] = []
        for i, ch in enumerate(stream):
            while state and ch not in goto[state]:
                state = fail[state]
            state = goto[state].get(ch, 0)
            if state:
                node = state if out[state] is not None else dl[state]
                while node:
                    length, payload = out[node]
                    found.append((i + 1 - length, i + 1, payload))
                    node = dl[node]
        return found


def _json_escape(text: str, ensure_ascii: bool) -> str:
    return json.dumps(text, ensure_ascii=ensure_ascii)[1:-1]


def _variants(text: str) -> List[Tuple[str, str]]:
    """Equivalent forms of a string: list of (form, mode) with mode in {raw, json_ascii, json_utf8}."""
    forms: List[Tuple[str, str]] = []
    seen = set()
    bases = [text]
    for norm in ("NFC", "NFD"):
        n = unicodedata.normalize(norm, text)
        if n not in bases:
            bases.append(n)
    for base in bases:
        for mode in ("raw", "json_utf8", "json_ascii"):
            if mode == "raw":
                form = base
            else:
                form = _json_escape(base, ensure_ascii=(mode == "json_ascii"))
            if form and form not in seen:
                seen.add(form)
                forms.append((form, mode))
    return forms


def _escape_for(mode: str, replacement: str) -> str:
    if mode == "raw" or not replacement:
        return replacement
    return _json_escape(replacement, ensure_ascii=(mode == "json_ascii"))


def _secret_units(value: str) -> List[str]:
    """Strings to index for one secret value (the whole value, CRLF variant, long lines)."""
    units = [value]
    if "\n" in value:
        crlf = value.replace("\r\n", "\n").replace("\n", "\r\n")
        if crlf not in units:
            units.append(crlf)
        for line in value.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if len(line) >= LINE_MIN_LENGTH and not line.startswith("-----") and line not in units:
                units.append(line)
    return units


@dataclass
class EngineStats:
    scans: int = 0
    cache_hits: int = 0
    cache_size_chars: int = 0
    secret_patterns: int = 0
    rule_patterns: int = 0


class Censor:
    """Immutable string transformer (the index never changes after ``build``)."""

    def __init__(self):
        self._secret_auto: Optional[Automaton] = None
        self._rule_auto: Optional[Automaton] = None
        self._secret_repl: List[str] = []
        self._rule_repl: List[str] = []
        self._min_len = 0
        self._ignore_case = False
        self._secret_replacement = DEFAULT_SECRET_REPLACEMENT
        self._cache: "OrderedDict[str, Tuple[str, int, int]]" = OrderedDict()
        self._cache_chars_max = DEFAULT_CACHE_CHARS
        self._lock = threading.Lock()
        self.stats = EngineStats()

    @classmethod
    def build(cls, rules: Sequence[Rule] = (), secrets: Sequence[str] = (), *, ignore_case: bool = False,
              secret_replacement: str = DEFAULT_SECRET_REPLACEMENT,
              cache_chars: int = DEFAULT_CACHE_CHARS) -> "Censor":
        self = cls()
        self._cache_chars_max = cache_chars
        self._ignore_case = ignore_case
        self._secret_replacement = secret_replacement
        self._set_secrets(secrets)
        self._set_rules(rules)
        return self

    def with_rules(self, rules: Sequence[Rule], *, ignore_case: Optional[bool] = None) -> "Censor":
        """New Censor: same secrets (shared, immutable index), new rules."""
        new = self._clone()
        if ignore_case is not None:
            new._ignore_case = ignore_case
        new._set_rules(rules)
        return new

    def with_secrets(self, secrets: Sequence[str], *, secret_replacement: Optional[str] = None) -> "Censor":
        """New Censor: same rules (shared, immutable index), new secrets."""
        new = self._clone()
        if secret_replacement is not None:
            new._secret_replacement = secret_replacement
        new._set_secrets(secrets)
        return new

    def _clone(self) -> "Censor":
        new = Censor()
        new._cache_chars_max = self._cache_chars_max
        new._ignore_case = self._ignore_case
        new._secret_replacement = self._secret_replacement
        new._secret_auto, new._secret_repl = self._secret_auto, self._secret_repl
        new._rule_auto, new._rule_repl = self._rule_auto, self._rule_repl
        new.stats.secret_patterns = self.stats.secret_patterns
        new.stats.rule_patterns = self.stats.rule_patterns
        new._refresh_min_len()
        return new

    def _set_secrets(self, secrets: Sequence[str]) -> None:
        patterns: List[Tuple[str, int]] = []
        repls: List[str] = []
        payloads: Dict[str, int] = {}
        for value in secrets:
            for unit in _secret_units(value):
                for form, mode in _variants(unit):
                    if form in payloads:
                        continue
                    payloads[form] = len(repls)
                    repls.append(_escape_for(mode, self._secret_replacement))
                    patterns.append((form, payloads[form]))
        self._secret_repl = repls
        self._secret_auto = Automaton(patterns) if patterns else None
        self.stats.secret_patterns = len(patterns)
        self._refresh_min_len()

    def _set_rules(self, rules: Sequence[Rule]) -> None:
        patterns: List[Tuple[str, int]] = []
        repls: List[str] = []
        payloads: Dict[str, int] = {}
        fold = _Fold() if self._ignore_case else None
        for rule in rules:
            for form, mode in _variants(rule.pattern):
                key = "".join(fold[c] for c in form) if fold is not None else form
                if key in payloads:
                    continue
                payloads[key] = len(repls)
                repls.append(_escape_for(mode, rule.replacement))
                patterns.append((form, payloads[key]))
        self._rule_repl = repls
        self._rule_auto = Automaton(patterns, ignore_case=self._ignore_case) if patterns else None
        self.stats.rule_patterns = len(patterns)
        self._refresh_min_len()

    def _refresh_min_len(self) -> None:
        lens = [a.min_len for a in (self._secret_auto, self._rule_auto) if a is not None]
        self._min_len = min(lens) if lens else 0

    # -- API ---------------------------------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return self._secret_auto is None and self._rule_auto is None

    def apply(self, text: str) -> str:
        return self.apply_counted(text)[0]

    def apply_counted(self, text: str) -> Tuple[str, int, int]:
        """Return (text, secret_replacement_count, rule_replacement_count)."""
        if self.is_empty or len(text) < self._min_len:
            return text, 0, 0
        cacheable = len(text) >= CACHE_MIN_TEXT
        if cacheable:
            with self._lock:
                hit = self._cache.get(text)
                if hit is not None:
                    self._cache.move_to_end(text)
                    self.stats.cache_hits += 1
                    return hit
        result = self._scan(text)
        if cacheable:
            self._cache_put(text, result)
        return result

    # -- internals -----------------------------------------------------------------------------------------

    def _scan(self, text: str) -> Tuple[str, int, int]:
        self.stats.scans += 1
        secrets = self._secret_auto.find_all(text) if self._secret_auto is not None else []
        rules = self._rule_auto.find_all(text) if self._rule_auto is not None else []
        if not secrets and not rules:
            return text, 0, 0
        secrets.sort(key=lambda m: (m[0], m[0] - m[1]))
        chosen_secrets: List[Tuple[int, int, str]] = []
        last = -1
        for start, end, payload in secrets:
            if start >= last:
                chosen_secrets.append((start, end, self._secret_repl[payload]))
                last = end
        rules.sort(key=lambda m: (m[0], m[0] - m[1], m[2]))
        chosen_rules: List[Tuple[int, int, str]] = []
        last = -1
        j = 0
        for start, end, payload in rules:
            while j < len(chosen_secrets) and chosen_secrets[j][1] <= start:
                j += 1
            if j < len(chosen_secrets) and chosen_secrets[j][0] < end:
                continue  # overlaps a secret: the secret takes priority
            if start >= last:
                chosen_rules.append((start, end, self._rule_repl[payload]))
                last = end
        merged = sorted(chosen_secrets + chosen_rules, key=lambda m: m[0])
        parts: List[str] = []
        cursor = 0
        for start, end, repl in merged:
            parts.append(text[cursor:start])
            parts.append(repl)
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts), len(chosen_secrets), len(chosen_rules)

    def _cache_put(self, text: str, result: Tuple[str, int, int]) -> None:
        with self._lock:
            if text in self._cache:
                return
            self._cache[text] = result
            self.stats.cache_size_chars += len(text) + (0 if result[0] is text else len(result[0]))
            while self.stats.cache_size_chars > self._cache_chars_max and len(self._cache) > 1:
                old_text, old = self._cache.popitem(last=False)
                self.stats.cache_size_chars -= len(old_text) + (0 if old[0] is old_text else len(old[0]))
