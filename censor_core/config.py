"""Read and validate settings (``plugins.entries.hermes-censor.settings`` in Hermes' config.yaml).

Any unknown or invalid key is reported (``ConfigProblem``): a typo ("group" instead of "groups") could otherwise
silently remove a protection. No secret value belongs in the settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from .engine import DEFAULT_SECRET_REPLACEMENT
from .keepassxc import KeePassXCConfig
from .kpx_helper import DEFAULT_MIN_LENGTH

TOP_KEYS = {"enabled", "rules_file", "ignore_case", "secret_replacement", "censor_tool_results",
            "max_age_minutes", "min_secret_length", "keepassxc"}
KP_KEYS = {"enabled", "database", "cli", "keyfile", "unlock", "askpass", "groups", "entries", "exclude",
           "select_all", "recursive", "fields", "include_history", "include_recycle_bin", "timeout_seconds",
           "prompt_timeout_seconds"}
DEFAULT_RULES_RELATIVE = os.path.join("plugin-data", "hermes-censor", "rules.txt")


@dataclass
class ConfigProblem:
    message: str
    repair: str = ""


@dataclass
class Settings:
    enabled: bool = True
    rules_file: str = ""
    rules_file_explicit: bool = False
    ignore_case: bool = False
    secret_replacement: str = DEFAULT_SECRET_REPLACEMENT
    censor_tool_results: bool = True
    max_age_minutes: float = 0
    min_secret_length: int = DEFAULT_MIN_LENGTH
    keepassxc: Optional[KeePassXCConfig] = None


def _resolve(path: str, home: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(home, path)


def _str_list(value: Any, name: str, problems: List[ConfigProblem]) -> Optional[List[str]]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    problems.append(ConfigProblem(f"{name} must be a list of strings"))
    return None


def _bool(value: Any, name: str, default: bool, problems: List[ConfigProblem]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    problems.append(ConfigProblem(f"{name} must be true or false"))
    return default


def _number(value: Any, name: str, default: float, problems: List[ConfigProblem], minimum: float = 0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum:
        return value
    problems.append(ConfigProblem(f"{name} must be a number >= {minimum}"))
    return default


def parse_settings(raw: Optional[Mapping[str, Any]], home: str) -> Tuple[Settings, List[ConfigProblem]]:
    raw = dict(raw or {})
    problems: List[ConfigProblem] = []
    s = Settings()
    for key in sorted(set(raw) - TOP_KEYS):
        problems.append(ConfigProblem(f"unknown setting: {key}", "Check the spelling (see docs/CONFIGURATION.md)."))
    s.enabled = _bool(raw.get("enabled"), "enabled", True, problems)
    s.ignore_case = _bool(raw.get("ignore_case"), "ignore_case", False, problems)
    s.censor_tool_results = _bool(raw.get("censor_tool_results"), "censor_tool_results", True, problems)
    rf = raw.get("rules_file")
    if rf is None:
        s.rules_file = os.path.join(home, DEFAULT_RULES_RELATIVE)
    elif isinstance(rf, str) and rf.strip():
        s.rules_file, s.rules_file_explicit = _resolve(rf, home), True
    else:
        problems.append(ConfigProblem("rules_file must be a path"))
        s.rules_file = os.path.join(home, DEFAULT_RULES_RELATIVE)
    rep = raw.get("secret_replacement")
    if rep is None:
        pass
    elif isinstance(rep, str):
        s.secret_replacement = rep
    else:
        problems.append(ConfigProblem("secret_replacement must be a string"))
    s.max_age_minutes = _number(raw.get("max_age_minutes"), "max_age_minutes", 0, problems)
    s.min_secret_length = int(_number(raw.get("min_secret_length"), "min_secret_length", DEFAULT_MIN_LENGTH,
                                      problems, minimum=1))
    kp = raw.get("keepassxc")
    if kp is not None:
        s.keepassxc = _parse_keepassxc(kp, s.min_secret_length, home, problems)
    return s, problems


def _parse_keepassxc(kp: Any, min_length: int, home: str, problems: List[ConfigProblem]) -> Optional[KeePassXCConfig]:
    if not isinstance(kp, dict):
        problems.append(ConfigProblem("keepassxc must be a settings block"))
        return None
    before = len(problems)
    for key in sorted(set(kp) - KP_KEYS):
        problems.append(ConfigProblem(f"keepassxc.{key}: unknown setting",
                                      "Check the spelling (see docs/CONFIGURATION.md)."))
    if not _bool(kp.get("enabled"), "keepassxc.enabled", True, problems):
        return None
    cfg = KeePassXCConfig(min_length=min_length)
    db = kp.get("database")
    if isinstance(db, str) and db.strip():
        cfg.database = _resolve(db, home)
    else:
        problems.append(ConfigProblem("keepassxc.database is required", "Give the path of the .kdbx file."))
    cli = kp.get("cli")
    if cli is None or isinstance(cli, str):
        cfg.cli = cli or None
    elif isinstance(cli, list) and cli and all(isinstance(c, str) for c in cli):
        cfg.cli = list(cli)
    else:
        problems.append(ConfigProblem("keepassxc.cli must be a path or a list of arguments"))
    keyfile = kp.get("keyfile")
    if keyfile is None or isinstance(keyfile, str):
        cfg.keyfile = _resolve(keyfile, home) if keyfile else None
    else:
        problems.append(ConfigProblem("keepassxc.keyfile must be a path"))
    unlock = kp.get("unlock", "prompt")
    if unlock in ("prompt", "keyfile-only"):
        cfg.unlock = unlock
        if unlock == "keyfile-only" and not cfg.keyfile:
            problems.append(ConfigProblem("keepassxc.unlock: keyfile-only requires keepassxc.keyfile"))
    else:
        problems.append(ConfigProblem("keepassxc.unlock must be prompt or keyfile-only"))
    askpass = kp.get("askpass")
    if askpass is not None:
        lst = _str_list(askpass, "keepassxc.askpass", problems)
        cfg.askpass = lst or None
    for key, attr in (("groups", "groups"), ("entries", "entries"), ("exclude", "exclude")):
        lst = _str_list(kp.get(key), f"keepassxc.{key}", problems)
        if lst is not None:
            setattr(cfg, attr, lst)
    fields = kp.get("fields")
    if fields is not None:
        lst = _str_list(fields, "keepassxc.fields", problems)
        if lst:
            cfg.fields = lst
        elif lst is not None:
            problems.append(ConfigProblem("keepassxc.fields cannot be empty"))
    cfg.select_all = _bool(kp.get("select_all"), "keepassxc.select_all", False, problems)
    cfg.recursive = _bool(kp.get("recursive"), "keepassxc.recursive", True, problems)
    cfg.include_history = _bool(kp.get("include_history"), "keepassxc.include_history", False, problems)
    cfg.include_recycle_bin = _bool(kp.get("include_recycle_bin"), "keepassxc.include_recycle_bin", False, problems)
    cfg.timeout_seconds = _number(kp.get("timeout_seconds"), "keepassxc.timeout_seconds", 90, problems, minimum=1)
    cfg.prompt_timeout_seconds = _number(kp.get("prompt_timeout_seconds"), "keepassxc.prompt_timeout_seconds", 180,
                                         problems, minimum=5)
    if not (cfg.groups or cfg.entries or cfg.select_all):
        problems.append(ConfigProblem("keepassxc: no selection (groups, entries or select_all)",
                                      "List the groups/entries to protect: only those are read."))
    if len(problems) > before:
        return None
    return cfg
