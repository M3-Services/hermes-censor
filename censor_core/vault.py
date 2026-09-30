"""Internal interface to a secret vault (implemented in v1 by ``keepassxc.KeePassXCSource``).

Minimal contract (see docs/CONNECTORS.md): a connector can
- ``load(interactive)``: return the values to protect plus a report WITHOUT values, or raise ``SecretSourceError``;
- ``fingerprint()``: cheap fingerprint of the vault state (no secret, no unlock) used to detect that a loaded
  list is stale;
- ``describe()``: exact description of what is read, for diagnostics.
The filtering engine and its tests depend only on this interface and on lists of ``str``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Protocol


class SecretSourceError(Exception):
    """Load failure. ``code`` is stable; ``message`` and ``repair`` NEVER contain a secret value."""

    def __init__(self, code: str, message: str, repair: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair = repair


@dataclass
class LoadReport:
    source: str
    scope: str  # exact description of what was read
    entries_selected: int = 0
    values_loaded: int = 0
    skipped_empty: int = 0
    skipped_short: int = 0
    duplicates: int = 0
    not_found: List[str] = field(default_factory=list)
    short_fields: List[str] = field(default_factory=list)
    fingerprint: Optional[str] = None
    loaded_at: float = field(default_factory=time.time)

    @property
    def complete(self) -> bool:
        """True if nothing selected was left unprotected (empty values are irrelevant and ignored)."""
        return not self.not_found and self.skipped_short == 0


@dataclass
class SecretLoad:
    values: List[str]
    report: LoadReport


class SecretSource(Protocol):
    name: str

    def load(self, *, interactive: bool) -> SecretLoad: ...

    def fingerprint(self) -> Optional[str]: ...

    def describe(self) -> str: ...
