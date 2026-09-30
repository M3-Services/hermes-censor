"""Filtering of Hermes auxiliary calls (context compression, session title, vision, approval...).

Hermes applies no request middleware to these calls. The only lever a plugin has is a side effect that Hermes does
NOT document: ``pre_auxiliary_call`` receives a *copy of the list* of messages, not copies of the message
dictionaries, so editing those dictionaries in place changes what is sent to the provider.

That has a side effect of its own: the dictionaries can belong to the caller, who could then keep the filtered text
in its own history. Every edit is therefore recorded and undone when ``post_auxiliary_call`` fires for the same
attempt (``AuxiliaryEdits.restore``).

Only the messages are reachable (chat ``messages``, Responses ``input``). A separate ``system`` / ``instructions``
string, tool definitions and other request fields are not: Hermes does not hand them to the hook in an editable form.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Hashable, List, Optional, Tuple

from .engine import Censor
from .walker import RequestReport, censor_request

MAX_PENDING = 256  # edits waiting for their post hook: bounded, oldest restored first if a post hook never comes

Saved = List[Tuple[Any, Any, Any]]  # (message dict, key, original value)


def edit_in_place(messages: Any, censor: Censor) -> Tuple[Saved, RequestReport]:
    """Replace, inside the message dictionaries themselves, every value the filter changes.

    Returns what was changed (to undo it) and the report. If an edit fails midway, what was already edited is undone
    and the exception propagates.
    """
    saved: Saved = []
    if not isinstance(messages, list):
        return saved, RequestReport()
    filtered, report = censor_request({"messages": messages}, censor)
    if not report.changed:
        return saved, report
    try:
        for old, new in zip(messages, filtered["messages"]):
            if old is new or not isinstance(old, dict) or not isinstance(new, dict):
                continue
            for key, value in new.items():
                if old.get(key) is not value:
                    saved.append((old, key, old.get(key)))
                    old[key] = value
    except Exception:
        restore(saved)
        raise
    return saved, report


def restore(saved: Saved) -> None:
    """Put the original values back (best effort: a message that cannot be written to is left as it is)."""
    for target, key, original in reversed(saved):
        try:
            target[key] = original
        except Exception:
            pass


class AuxiliaryEdits:
    """Edits made before an auxiliary call, waiting to be undone after it (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: "OrderedDict[Hashable, Saved]" = OrderedDict()

    def keep(self, key: Hashable, saved: Saved) -> None:
        overflow: List[Saved] = []
        with self._lock:
            self._pending.setdefault(key, []).extend(saved)
            while len(self._pending) > MAX_PENDING:
                overflow.append(self._pending.popitem(last=False)[1])
        for old in overflow:
            restore(old)

    def undo(self, key: Hashable) -> int:
        with self._lock:
            saved: Optional[Saved] = self._pending.pop(key, None)
        if saved:
            restore(saved)
        return len(saved or ())

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)
