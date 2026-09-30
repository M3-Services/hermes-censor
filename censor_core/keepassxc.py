"""KeePassXC connector (v1): reads an XML export through ``keepassxc-cli`` inside an isolated helper.

What this connector does (and does not do) is described in docs/KEEPASSXC.md. In short:
- it reads the .kdbx FILE with ``keepassxc-cli``; it does not connect to the running KeePassXC instance (the CLI
  does not share the GUI's unlocked state);
- the master password is typed locally in the helper (``kpx_helper.py``), never in the Hermes process;
- only the configured selection (groups / entries / fields) is kept.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union

from .kpx_helper import CREATE_NO_WINDOW, DEFAULT_FIELDS, DEFAULT_MIN_LENGTH
from .vault import LoadReport, SecretLoad, SecretSourceError

HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kpx_helper.py")
_WIN_DEFAULT = r"C:\Program Files\KeePassXC\keepassxc-cli.exe"

_ERRORS: Dict[str, tuple] = {
    "cli_not_found": ("keepassxc-cli not found or not executable",
                      "Install KeePassXC or set keepassxc.cli (path to keepassxc-cli)."),
    "db_not_found": ("KeePassXC database file not found",
                     "Check keepassxc.database (path to the .kdbx file)."),
    "keyfile_not_found": ("key file not found", "Check keepassxc.keyfile."),
    "unlock_failed": ("unlock refused (invalid credentials or unreadable database)",
                      "Run /censor unlock again and check the master password and the key file."),
    "unlock_cancelled": ("password prompt cancelled or timed out",
                         "Run /censor unlock again and type the password in the local window."),
    "no_prompt_channel": ("no local prompt channel available (non-interactive session?)",
                          "Unlock from a session with a desktop (/censor unlock), or configure "
                          "keepassxc.askpass, or use keepassxc.unlock: keyfile-only (see docs/KEEPASSXC.md)."),
    "cli_timeout": ("keepassxc-cli did not answer in time",
                    "Increase keepassxc.timeout_seconds (slow key derivation?)."),
    "empty_selection": ("no selection configured: nothing to load",
                        "Set keepassxc.groups or keepassxc.entries (or keepassxc.select_all: true)."),
    "xml_invalid": ("vault export unreadable", "Check the keepassxc-cli version (2.7+ required)."),
    "unsupported_password": ("password cannot be passed through standard input",
                             "Use keepassxc.askpass or a key file."),
    "helper_failed": ("the vault reading process failed", "Run \"hermes censor test-unlock\"."),
    "helper_timeout": ("the vault reading process did not answer in time",
                       "Run /censor unlock again; increase keepassxc.prompt_timeout_seconds if needed."),
    "internal_error": ("internal error in the KeePassXC helper", "Run \"hermes censor test-unlock\"."),
    "bad_job": ("invalid KeePassXC configuration", "Check keepassxc.* (see hermes censor status)."),
}


@dataclass
class KeePassXCConfig:
    database: str = ""
    cli: Union[str, Sequence[str], None] = None
    keyfile: Optional[str] = None
    unlock: str = "prompt"  # "prompt" | "keyfile-only"
    askpass: Optional[Sequence[str]] = None
    channels: Sequence[str] = ("askpass", "gui")
    groups: List[str] = field(default_factory=list)
    entries: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    select_all: bool = False
    recursive: bool = True
    fields: List[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    include_history: bool = False
    include_recycle_bin: bool = False
    min_length: int = DEFAULT_MIN_LENGTH
    timeout_seconds: float = 90
    prompt_timeout_seconds: float = 180


def resolve_cli(cli: Union[str, Sequence[str], None]) -> List[str]:
    if cli:
        return [cli] if isinstance(cli, str) else list(cli)
    found = shutil.which("keepassxc-cli")
    if found:
        return [found]
    if os.name == "nt" and os.path.exists(_WIN_DEFAULT):
        return [_WIN_DEFAULT]
    return ["keepassxc-cli"]


class KeePassXCSource:
    """``vault.SecretSource`` implementation for KeePassXC."""

    def __init__(self, config: KeePassXCConfig):
        self.config = config
        self.name = f"keepassxc:{os.path.basename(config.database) or '?'}"

    # -- SecretSource ---------------------------------------------------------------------------------------

    def describe(self) -> str:
        c = self.config
        parts = [f"database \"{os.path.basename(c.database)}\""]
        if c.select_all:
            parts.append("all entries (select_all)")
        if c.groups:
            parts.append("groups: " + ", ".join(g or "/" for g in c.groups)
                         + (" (with subgroups)" if c.recursive else " (without subgroups)"))
        if c.entries:
            parts.append("entries: " + ", ".join(c.entries))
        if c.exclude:
            parts.append("exclusions: " + ", ".join(c.exclude))
        parts.append("fields: " + ", ".join("protected fields" if f == "@protected" else f for f in c.fields))
        parts.append("recycle bin " + ("included" if c.include_recycle_bin else "excluded"))
        parts.append("history " + ("included" if c.include_history else "excluded"))
        parts.append(f"values shorter than {c.min_length} characters ignored")
        parts.append("unlock " + ("by key file only" if c.unlock == "keyfile-only" else "by local prompt"))
        return "; ".join(parts)

    def fingerprint(self) -> Optional[str]:
        try:
            st = os.stat(self.config.database)
        except OSError:
            return None
        return f"{st.st_mtime_ns}:{st.st_size}"

    def load(self, *, interactive: bool) -> SecretLoad:
        c = self.config
        if c.unlock != "keyfile-only" and not interactive:
            code = "no_prompt_channel"
            raise SecretSourceError(code, *_ERRORS[code])
        job = {
            "db": c.database, "cli": resolve_cli(c.cli), "keyfile": c.keyfile, "unlock": c.unlock,
            "channels": list(c.channels), "askpass": list(c.askpass) if c.askpass else None,
            "timeout": c.timeout_seconds, "prompt_timeout": c.prompt_timeout_seconds,
            "selection": {
                "groups": list(c.groups), "entries": list(c.entries), "exclude": list(c.exclude),
                "select_all": c.select_all, "recursive": c.recursive, "fields": list(c.fields),
                "include_history": c.include_history, "include_recycle_bin": c.include_recycle_bin,
                "min_length": c.min_length,
            },
        }
        flags = 0 if "tty" in c.channels or os.name != "nt" else CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [sys.executable, "-I", HELPER_PATH], input=json.dumps(job).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=c.prompt_timeout_seconds + c.timeout_seconds + 30, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise SecretSourceError("helper_timeout", *_ERRORS["helper_timeout"]) from None
        except OSError:
            raise SecretSourceError("helper_failed", *_ERRORS["helper_failed"]) from None
        try:
            data = json.loads(proc.stdout.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise SecretSourceError("helper_failed", *_ERRORS["helper_failed"]) from None
        if not data.get("ok"):
            code = data.get("code") if data.get("code") in _ERRORS else "helper_failed"
            raise SecretSourceError(code, *_ERRORS[code])
        rep = data["report"]
        fp = data.get("fingerprint")
        report = LoadReport(
            source=self.name, scope=self.describe(), entries_selected=rep["entries_selected"],
            values_loaded=rep["values_loaded"], skipped_empty=rep["skipped_empty"],
            skipped_short=rep["skipped_short"], duplicates=rep["duplicates"], not_found=list(rep["not_found"]),
            short_fields=list(rep["short_fields"]), fingerprint=f"{fp[0]}:{fp[1]}" if fp else None)
        return SecretLoad(values=list(data["values"]), report=report)
