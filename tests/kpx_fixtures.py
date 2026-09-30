"""Factory for FICTIONAL KeePassXC databases (never the user's database).

A tree described in Python is serialised to KeePass XML and imported with ``keepassxc-cli import`` to obtain a
real .kdbx file; ``xml_for`` returns the XML alone (selection unit tests).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

_WIN_DEFAULT = r"C:\Program Files\KeePassXC\keepassxc-cli.exe"


def find_cli() -> Optional[str]:
    found = shutil.which("keepassxc-cli")
    if found:
        return found
    if os.name == "nt" and os.path.exists(_WIN_DEFAULT):
        return _WIN_DEFAULT
    return None


@dataclass
class E:
    """Entry: fields = {key: value}; ``protected`` lists the keys marked ProtectInMemory."""
    title: str
    fields: Dict[str, str] = field(default_factory=dict)
    protected: Tuple[str, ...] = ("Password",)
    history: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class G:
    name: str
    entries: List[E] = field(default_factory=list)
    groups: List["G"] = field(default_factory=list)
    recycle_bin: bool = False


def _uuid() -> str:
    return base64.b64encode(uuid.uuid4().bytes).decode()


def _entry_xml(e: E, history=None) -> str:
    strings = {"Title": e.title, **e.fields}
    body = []
    for key, value in strings.items():
        prot = ' ProtectInMemory="True"' if key in e.protected else ""
        body.append(f"<String><Key>{escape(key)}</Key><Value{prot}>{escape(value)}</Value></String>")
    hist = ""
    if e.history:
        items = []
        for old in e.history:
            hi = E(e.title, old, e.protected)
            items.append(_entry_xml(hi))
        hist = "<History>" + "".join(items) + "</History>"
    return f"<Entry><UUID>{_uuid()}</UUID>{''.join(body)}{hist}</Entry>"


def _group_xml(g: G, ids: dict) -> str:
    gid = _uuid()
    if g.recycle_bin:
        ids["recycle"] = gid
    inner = "".join(_entry_xml(e) for e in g.entries) + "".join(_group_xml(c, ids) for c in g.groups)
    return f"<Group><UUID>{gid}</UUID><Name>{escape(g.name)}</Name>{inner}</Group>"


def xml_for(root: G) -> str:
    ids: dict = {}
    body = _group_xml(root, ids)
    recycle = ids.get("recycle", "AAAAAAAAAAAAAAAAAAAAAA==")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<KeePassFile><Meta>'
            "<Generator>KeePassXC</Generator><DatabaseName>Fictional</DatabaseName>"
            f"<RecycleBinEnabled>True</RecycleBinEnabled><RecycleBinUUID>{recycle}</RecycleBinUUID></Meta>"
            f"<Root>{body}</Root></KeePassFile>")


def build_kdbx(cli: str, path: str, root: G, password: Optional[str] = None, keyfile: Optional[str] = None) -> str:
    """Create (or replace) a real fictional .kdbx database. Returns ``path``."""
    xml_path = path + ".src.xml"
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write(xml_for(root))
    if os.path.exists(path):
        os.remove(path)
    args = [cli, "import"]
    stdin = b""
    if password is not None:
        args.append("-p")
        stdin = f"{password}\n{password}\n".encode("utf-8")
    if keyfile is not None:
        if not os.path.exists(keyfile):
            subprocess.run([cli, "db-create", "--set-key-file", keyfile, path + ".tmpkey.kdbx"],
                           capture_output=True, check=True)
            os.remove(path + ".tmpkey.kdbx")
        args += ["-k", keyfile]
    args += [xml_path, path]
    proc = subprocess.run(args, input=stdin, capture_output=True, timeout=120)
    os.remove(xml_path)
    if proc.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(f"cannot import the fictional database (code {proc.returncode})")
    return path


# Shared fictional data set (values are made up; none comes from a real vault)
FICTIVE_TREE = G("Root", entries=[E("Root-entry", {"Password": "RootSecret-001"})], groups=[
    G("Hermes", entries=[
        E("Server", {"UserName": "alice", "Password": "FictionalPassword-AAA", "ApiKey": "fictional-api-key-BBB-2222"},
          protected=("Password", "ApiKey")),
        E("Empty", {"Password": ""}),
        E("Short", {"Password": "abc"}),
        E("Duplicate", {"Password": "FictionalPassword-AAA"}),
        E("History", {"Password": "CurrentValue-777"}, history=[{"Password": "OldValue-666"}]),
    ], groups=[G("Sub", entries=[E("Deep", {"Password": "DeepSecret-999"})])]),
    G("Other", entries=[E("Elsewhere", {"Password": "OtherSecret-555"})]),
    G("Recycle", entries=[E("Discarded", {"Password": "DiscardedSecret-333"})], recycle_bin=True),
])
