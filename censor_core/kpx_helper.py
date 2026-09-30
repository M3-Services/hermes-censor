"""KeePassXC helper: a short-lived, isolated process using the standard library only.

Launched by the plugin (or by ``hermes censor test-unlock``): ``python kpx_helper.py``; the job arrives as JSON
on stdin (paths and selection, NEVER a password), the result goes back as (ASCII) JSON on stdout.

Security:
- the master password is typed through a LOCAL channel (Tk window, terminal, or a configured "askpass" command);
  it never enters the Hermes process, the chat, a command line or a file;
- it is passed to ``keepassxc-cli`` through its standard input only;
- this process reads the full XML export of the vault, keeps only the requested selection, then exits;
- stderr stays empty: no value and no CLI message is relayed.

This file is also imported by the tests for ``select_secrets`` (a pure function).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_FIELDS = ["Password", "@protected"]
DEFAULT_MIN_LENGTH = 6
CREATE_NO_WINDOW = 0x08000000


class HelperError(Exception):
    """Controlled error: stable ``code``, ``detail`` never contains a secret value."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class _ChannelUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Selection (pure)
# ---------------------------------------------------------------------------------------------------------------

def _segments(path: str) -> List[str]:
    return [s for s in str(path).split("/") if s]


def _is_protected(value_el: ET.Element) -> bool:
    return value_el.get("ProtectInMemory") == "True" or value_el.get("Protected") == "True"


def select_secrets(xml_data: Any, selection: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the values to protect from a KeePass XML export according to ``selection``.

    Returns ``{"values": [...], "report": {...}}``; ``report`` never contains a value.
    """
    groups_sel = [_segments(p) for p in selection.get("groups") or []]
    entries_sel = [_segments(p) for p in selection.get("entries") or []]
    excludes = [_segments(p) for p in selection.get("exclude") or []]
    select_all = bool(selection.get("select_all"))
    recursive = selection.get("recursive", True)
    fields = list(selection.get("fields") or DEFAULT_FIELDS)
    include_history = bool(selection.get("include_history"))
    include_bin = bool(selection.get("include_recycle_bin"))
    min_length = int(selection.get("min_length", DEFAULT_MIN_LENGTH))
    if not (groups_sel or entries_sel or select_all or selection.get("groups") or selection.get("entries")):
        raise HelperError("empty_selection")
    named = {f for f in fields if f != "@protected"}
    want_protected = "@protected" in fields

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise HelperError("xml_invalid", "unreadable XML export") from exc
    root_group = root.find("Root/Group")
    if root_group is None:
        raise HelperError("xml_invalid", "root group not found")
    bin_uuid = (root.findtext("Meta/RecycleBinUUID") or "").strip()

    matched_groups = set()
    matched_entries = set()
    values: List[str] = []
    seen = set()
    report = {"entries_selected": 0, "fields_read": 0, "values_loaded": 0, "skipped_empty": 0,
              "skipped_short": 0, "duplicates": 0, "not_found": [], "short_fields": [], "groups_matched": 0}

    def group_included(path: List[str]) -> bool:
        if select_all:
            return True
        for s in groups_sel:
            if path == s or (recursive and path[:len(s)] == s):
                return True
        return False

    def excluded(full: List[str]) -> bool:
        return any(full[:len(x)] == x for x in excludes)

    def take(entry: ET.Element, label: str) -> None:
        for string in entry.findall("String"):
            key = string.findtext("Key") or ""
            value_el = string.find("Value")
            if value_el is None:
                continue
            if not (key in named or (want_protected and _is_protected(value_el))):
                continue
            report["fields_read"] += 1
            text = value_el.text or ""
            if not text.strip():
                report["skipped_empty"] += 1
            elif len(text) < min_length:
                report["skipped_short"] += 1
                if len(report["short_fields"]) < 50:
                    report["short_fields"].append(f"{label}:{key}")
            elif text in seen:
                report["duplicates"] += 1
            else:
                seen.add(text)
                values.append(text)

    def visit(group: ET.Element, path: List[str]) -> None:
        if path and (group.findtext("UUID") or "").strip() == bin_uuid and bin_uuid and not include_bin:
            return
        for s in groups_sel:
            if path == s:
                matched_groups.add(tuple(s))
        in_group = group_included(path)
        for entry in group.findall("Entry"):
            title = entry.findtext("String[Key='Title']/Value") or ""
            full = path + [title]
            by_entry = any(full == e for e in entries_sel)
            if by_entry:
                for e in entries_sel:
                    if full == e:
                        matched_entries.add(tuple(e))
            if not (in_group or by_entry) or excluded(full):
                continue
            report["entries_selected"] += 1
            label = "/".join(full)
            take(entry, label)
            if include_history:
                for old in entry.findall("History/Entry"):
                    take(old, label + " (history)")
        for sub in group.findall("Group"):
            visit(sub, path + [sub.findtext("Name") or ""])

    visit(root_group, [])
    for s in groups_sel:
        if select_all is False and tuple(s) not in matched_groups:
            report["not_found"].append("/".join(s) or "/")
    if not select_all:
        for e in entries_sel:
            if tuple(e) not in matched_entries:
                report["not_found"].append("/".join(e))
    report["groups_matched"] = len(matched_groups)
    report["values_loaded"] = len(values)
    return {"values": values, "report": report}


# ---------------------------------------------------------------------------------------------------------------
# Local entry of the master password
# ---------------------------------------------------------------------------------------------------------------

def _ask_askpass(argv: Sequence[str], timeout: float) -> str:
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError("unlock_cancelled", "askpass command unavailable or too slow") from exc
    if proc.returncode != 0:
        raise HelperError("unlock_cancelled", "entry cancelled")
    text = proc.stdout.decode("utf-8", "replace")
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return text


def _ask_gui(title: str, timeout: float, _on_ready=None) -> str:
    """Local prompt window. ``_on_ready`` (tests only, internal Python parameter) receives (root, entry, ok, cancel)."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise _ChannelUnavailable()
    try:
        import tkinter as tk
    except Exception as exc:  # tkinter missing
        raise _ChannelUnavailable() from exc
    try:
        root = tk.Tk()
    except Exception as exc:  # no display
        raise _ChannelUnavailable() from exc
    result = {"pw": None}
    root.title(title)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    root.resizable(False, False)
    tk.Label(root, justify="left", padx=14, pady=8,
             text="KeePassXC master password\n(typed locally: it is never sent to the model)").pack()
    entry = tk.Entry(root, show="*", width=42)
    entry.pack(padx=14, pady=4)

    def ok(_event=None):
        result["pw"] = entry.get()
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    row = tk.Frame(root)
    row.pack(pady=8)
    ok_button = tk.Button(row, text="Unlock", command=ok, width=14)
    ok_button.pack(side="left", padx=6)
    cancel_button = tk.Button(row, text="Cancel", command=cancel, width=10)
    cancel_button.pack(side="left", padx=6)
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.after(int(timeout * 1000), cancel)
    root.update_idletasks()
    entry.focus_force()
    if _on_ready is not None:
        root.after(50, lambda: _on_ready(root, entry, ok_button, cancel_button))
    root.mainloop()
    pw = result["pw"]
    if not pw:
        raise HelperError("unlock_cancelled", "entry cancelled or timed out")
    return pw


def _ask_tty(prompt: str) -> str:
    if os.name == "nt":
        import ctypes
        if not ctypes.windll.kernel32.GetConsoleWindow():
            raise _ChannelUnavailable()
        import msvcrt
        for ch in prompt:
            msvcrt.putwch(ch)
        chars: List[str] = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise HelperError("unlock_cancelled", "entry interrupted")
            if ch == "\x08":
                if chars:
                    chars.pop()
            else:
                chars.append(ch)
        msvcrt.putwch("\r")
        msvcrt.putwch("\n")
        pw = "".join(chars)
    else:
        try:
            import termios
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except (OSError, ImportError) as exc:
            raise _ChannelUnavailable() from exc
        try:
            old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO
            os.write(fd, prompt.encode("utf-8"))
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            try:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = os.read(fd, 1)
                    if not chunk:
                        break
                    data += chunk
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                os.write(fd, b"\n")
        finally:
            os.close(fd)
        pw = data.decode("utf-8", "replace").rstrip("\r\n")
    if not pw:
        raise HelperError("unlock_cancelled", "empty password")
    return pw


def ask_password(channels: Sequence[str], askpass: Optional[Sequence[str]], prompt_timeout: float, title: str) -> str:
    for channel in channels:
        try:
            if channel == "askpass" and askpass:
                return _ask_askpass(askpass, prompt_timeout)
            if channel == "gui":
                return _ask_gui(title, prompt_timeout)
            if channel == "tty":
                return _ask_tty("KeePassXC master password: ")
        except _ChannelUnavailable:
            continue
    raise HelperError("no_prompt_channel")


# ---------------------------------------------------------------------------------------------------------------
# keepassxc-cli
# ---------------------------------------------------------------------------------------------------------------

def run_export(cli: Sequence[str], db: str, keyfile: Optional[str], password: Optional[str], timeout: float) -> bytes:
    """``keepassxc-cli export`` as XML. ``password=None``: database without a password (key file only)."""
    args = list(cli) + ["export", "-q", "-f", "xml"]
    if keyfile:
        args += ["-k", keyfile]
    stdin = b""
    if password is None:
        args.append("--no-password")
    else:
        if "\n" in password or "\r" in password:
            raise HelperError("unsupported_password", "line break in the password")
        stdin = (password + "\n").encode("utf-8")
    args.append(db)
    try:
        proc = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except FileNotFoundError as exc:
        raise HelperError("cli_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise HelperError("cli_timeout") from exc
    except OSError as exc:
        raise HelperError("cli_not_found", "cannot execute") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise HelperError("unlock_failed")
    return proc.stdout


# ---------------------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------------------

def run_job(job: Dict[str, Any]) -> Dict[str, Any]:
    db = job.get("db")
    cli = job.get("cli")
    if not isinstance(db, str) or not db or not isinstance(cli, list) or not cli:
        raise HelperError("bad_job")
    try:
        st = os.stat(db)
    except OSError as exc:
        raise HelperError("db_not_found") from exc
    fingerprint = [st.st_mtime_ns, st.st_size]
    keyfile = job.get("keyfile") or None
    if keyfile and not os.path.exists(keyfile):
        raise HelperError("keyfile_not_found")
    password: Optional[str]
    if job.get("unlock") == "keyfile-only":
        if not keyfile:
            raise HelperError("bad_job", "keyfile-only without a key file")
        password = None
    else:
        password = ask_password(job.get("channels") or ["gui"], job.get("askpass"),
                                float(job.get("prompt_timeout", 180)),
                                job.get("prompt_title") or "Hermes Censor - KeePassXC unlock")
    try:
        xml_data = run_export(cli, db, keyfile, password, float(job.get("timeout", 90)))
    finally:
        password = None
    result = select_secrets(xml_data, job.get("selection") or {})
    return {"ok": True, "values": result["values"], "report": result["report"], "fingerprint": fingerprint}


def main() -> int:
    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        out = run_job(job if isinstance(job, dict) else {})
    except HelperError as exc:
        out = {"ok": False, "code": exc.code, "detail": exc.detail}
    except Exception as exc:  # never a traceback: it could quote data
        out = {"ok": False, "code": "internal_error", "detail": type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=True).encode("ascii"))
    sys.stdout.buffer.flush()
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
