"""Plugin runtime: settings, rules, vault, state, filtering. No dependency on Hermes (testable on its own).

Design guarantees:
- no secret value in logs, statuses, exceptions or counters: values live only in the engine index (``Censor``);
  this module keeps no copy of them;
- a stale secret list (database changed, too old, failed refresh) keeps filtering but the state becomes
  DEGRADED, never ACTIVE;
- an exception while filtering lets the request go out (Hermes is "fail-open") but sets the state to ERROR.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .auxiliary import AuxiliaryEdits, edit_in_place
from .config import Settings, parse_settings
from .engine import Censor
from .keepassxc import KeePassXCConfig, KeePassXCSource
from .rules import RuleIssue, parse_rules
from .vault import LoadReport, SecretSourceError
from .walker import censor_request

logger = logging.getLogger("hermes-censor")

SETTINGS_INTERVAL = 2.0
RULES_INTERVAL = 2.0
STALE_INTERVAL = 5.0
AUTO_RETRY_INTERVAL = 60.0

DEGRADED_CODES = {
    "CONFIG_INVALID", "RULES_MISSING", "RULES_UNREADABLE", "RULES_INVALID_LINES", "SECRETS_NOT_LOADED",
    "SECRETS_LOAD_FAILED", "SECRETS_INCOMPLETE_SHORT", "SECRETS_INCOMPLETE_NOT_FOUND", "SECRETS_STALE_DB_CHANGED",
    "SECRETS_STALE_DB_MISSING", "SECRETS_STALE_AGE", "AUX_RESTART_REQUIRED",
}

REMINDER = ("Reminder: Hermes is \"fail-open\" (a plugin failure lets the request go out uncensored); search is "
            "literal (reworded, encoded or split secrets are not detected); this plugin does not protect what was "
            "already displayed, logged or stored locally before censoring.")


@dataclass
class Finding:
    code: str
    message: str
    repair: str = ""


@dataclass
class Status:
    level: str  # ACTIVE | DEGRADED | INACTIVE | ERROR
    findings: List[Finding] = field(default_factory=list)


@dataclass
class Outcome:
    ok: bool
    message: str


def _default_source_factory(cfg: KeePassXCConfig):
    return KeePassXCSource(cfg)


class Runtime:
    def __init__(self, settings_provider: Callable[[], Mapping[str, Any]], home: str,
                 source_factory: Callable[[KeePassXCConfig], Any] = _default_source_factory,
                 clock: Callable[[], float] = time.time):
        self._provider = settings_provider
        self._home = home
        self._factory = source_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._unlock_lock = threading.Lock()
        self._unlock_thread: Optional[threading.Thread] = None
        self._settings = Settings()
        self._problems: list = []
        self._raw: Any = object()  # forces the first read
        self._source: Any = None
        self._source_cfg: Optional[KeePassXCConfig] = None
        self._censor = Censor()
        # rules
        self._rules_state = "none"  # none | ok | missing | unreadable
        self._rules_fp: Any = None
        self._rules_issues: List[RuleIssue] = []
        self._rules_count = 0
        self._rules_loaded_once = False
        self._rules_key: Any = None
        # secrets
        self._report: Optional[LoadReport] = None
        self._loaded_at: Optional[float] = None
        self._secret_error: Optional[SecretSourceError] = None
        self._secret_error_kind = ""
        self._stale: Optional[str] = None
        self._auto_failed_at: Optional[float] = None
        self._secret_replacement_built = ""
        # rate-limiting clocks
        self._t_settings = self._t_rules = self._t_stale = None
        # activity (never any value)
        self._act = {"requests": 0, "tool_results": 0, "last_secret_replacements": 0, "last_rule_replacements": 0,
                     "total_secret_replacements": 0, "total_rule_replacements": 0, "failopen_count": 0,
                     "last_request_at": None, "signed_blocks_skipped": 0,
                     "aux_requests": 0, "aux_last_secret_replacements": 0, "aux_last_rule_replacements": 0,
                     "aux_total_secret_replacements": 0, "aux_total_rule_replacements": 0, "aux_failopen_count": 0}
        self._fail: Optional[str] = None
        self._aux_fail: Optional[str] = None
        self._aux = AuxiliaryEdits()
        self.aux_hooks_registered = False  # set by the Hermes glue: the hooks exist only if the setting was on at load
        self._logged_signature: Any = None

    # ------------------------------------------------------------------------------------------------ settings

    @property
    def settings(self) -> Settings:
        self._tick(force=True)
        return self._settings

    def _refresh_settings(self, now: float, force: bool) -> None:
        if not force and self._t_settings is not None and now - self._t_settings < SETTINGS_INTERVAL:
            return
        self._t_settings = now
        try:
            raw = dict(self._provider() or {})
        except Exception as exc:
            logger.warning("hermes-censor: cannot read settings (%s)", type(exc).__name__)
            return
        if raw == self._raw:
            return
        self._raw = raw
        old = self._settings
        self._settings, self._problems = parse_settings(raw, self._home)
        new = self._settings
        kp = new.keepassxc
        if kp != self._source_cfg:
            if self._source_cfg is not None:
                self._drop_secrets()  # other database / other selection: the old list is no longer valid
            self._source_cfg = kp
            self._source = self._factory(kp) if kp is not None else None
            self._auto_failed_at = None
        elif old.secret_replacement != new.secret_replacement and self._report is not None:
            self._drop_secrets()  # the token can only change by re-reading the values
        if (old.rules_file, old.ignore_case) != (new.rules_file, new.ignore_case):
            self._rules_fp = None
            self._t_rules = None

    # ------------------------------------------------------------------------------------------------ rules

    def reload_rules(self, force: bool = False) -> None:
        with self._lock:
            self._refresh_rules(self._clock(), force=True if force else False, ignore_interval=True)

    def _refresh_rules(self, now: float, force: bool, ignore_interval: bool = False) -> None:
        if not ignore_interval and self._t_rules is not None and now - self._t_rules < RULES_INTERVAL:
            return
        self._t_rules = now
        path = self._settings.rules_file
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._rules_state = "missing" if (self._settings.rules_file_explicit or self._rules_loaded_once) else "none"
            return
        except OSError:
            self._rules_state = "unreadable"
            return
        fp = (st.st_mtime_ns, st.st_size, path, self._settings.ignore_case)
        if not force and fp == self._rules_fp and self._rules_state == "ok":
            return
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            self._rules_state = "unreadable"
            return
        parsed = parse_rules(text)
        self._censor = self._censor.with_rules(parsed.rules, ignore_case=self._settings.ignore_case)
        self._rules_fp = fp
        self._rules_state = "ok"
        self._rules_loaded_once = True
        self._rules_issues = parsed.issues
        self._rules_count = len(parsed.rules)

    # ------------------------------------------------------------------------------------------------ secrets

    def _drop_secrets(self) -> None:
        self._censor = self._censor.with_secrets([])
        self._report = None
        self._loaded_at = None
        self._stale = None
        self._secret_error = None

    def forget(self) -> Outcome:
        with self._lock:
            self._drop_secrets()
            self._auto_failed_at = None
        return Outcome(True, "Secrets forgotten: secret protection is unavailable until the next unlock.")

    def unlock(self, interactive: bool) -> Outcome:
        """Load (or reload) the secrets. ``interactive`` allows the local password prompt."""
        self._tick(force=True, auto=False)
        source = self._source
        if source is None:
            return Outcome(False, "KeePassXC vault not configured or invalid configuration (see /censor).")
        if not self._unlock_lock.acquire(blocking=False):
            return Outcome(False, "An unlock is already in progress.")
        try:
            try:
                load = source.load(interactive=interactive)
            except SecretSourceError as exc:
                self._record_secret_error(exc.code, exc.message, exc.repair)
                return Outcome(False, f"{exc.message}. {exc.repair}".strip())
            except Exception as exc:  # never a raw message: it could quote data
                self._record_secret_error("unexpected", f"unexpected connector error ({type(exc).__name__})", "")
                return Outcome(False, f"Unexpected connector error ({type(exc).__name__}).")
            with self._lock:
                self._censor = self._censor.with_secrets(load.values, secret_replacement=self._settings.secret_replacement)
                self._secret_replacement_built = self._settings.secret_replacement
                load.values.clear()
                self._report = load.report
                self._loaded_at = self._clock()
                self._secret_error = None
                self._stale = None
                self._auto_failed_at = None
            rep = load.report
            extra = "" if rep.complete else " (incomplete selection: see /censor)"
            return Outcome(True, f"{rep.values_loaded} value(s) loaded from {rep.entries_selected} entry(ies){extra}.")
        finally:
            self._unlock_lock.release()

    def start_unlock(self):
        """Run ``unlock`` in the background (never blocking: the local prompt can take a while). -> (Outcome, thread|None)"""
        self._tick(force=True, auto=False)
        if self._source is None:
            return Outcome(False, "KeePassXC vault not configured or invalid configuration (see /censor)."), None
        if self._unlock_lock.locked():
            return Outcome(False, "An unlock is already in progress."), None
        thread = threading.Thread(target=self.unlock, args=(True,), name="hermes-censor-unlock", daemon=True)
        thread.start()
        self._unlock_thread = thread
        return Outcome(True, "Unlock started: if a prompt window opens on this machine, type the master password "
                             "there (never in the chat). Then type /censor to check the state."), thread

    def _record_secret_error(self, code: str, message: str, repair: str) -> None:
        with self._lock:
            self._secret_error = SecretSourceError(code, message, repair)
            self._auto_failed_at = self._clock()

    def _check_stale(self, now: float, force: bool) -> None:
        if self._report is None or self._source is None:
            self._stale = None
            return
        if not force and self._t_stale is not None and now - self._t_stale < STALE_INTERVAL:
            return
        self._t_stale = now
        stale = None
        try:
            fp = self._source.fingerprint()
        except Exception:
            fp = self._report.fingerprint
        if fp is None:
            stale = "SECRETS_STALE_DB_MISSING"
        elif self._report.fingerprint is not None and fp != self._report.fingerprint:
            stale = "SECRETS_STALE_DB_CHANGED"
        elif self._settings.max_age_minutes and self._loaded_at is not None \
                and now - self._loaded_at > self._settings.max_age_minutes * 60:
            stale = "SECRETS_STALE_AGE"
        self._stale = stale

    def _auto_wanted(self, now: float) -> bool:
        kp = self._settings.keepassxc
        if kp is None or kp.unlock != "keyfile-only" or self._source is None:
            return False
        if self._report is not None and self._stale is None:
            return False
        return self._auto_failed_at is None or now - self._auto_failed_at >= AUTO_RETRY_INTERVAL

    # ------------------------------------------------------------------------------------------------ tick

    def _tick(self, force: bool = False, auto: bool = True) -> None:
        with self._lock:
            now = self._clock()
            self._refresh_settings(now, force)
            if self._settings.enabled:
                self._refresh_rules(now, force=False)
                self._check_stale(now, force)
            want_auto = auto and self._settings.enabled and self._auto_wanted(now)
        if want_auto:  # outside the lock: reading the vault can take a while; a single thread handles it
            self.unlock(interactive=False)

    # ------------------------------------------------------------------------------------------------ requests

    def process_request(self, request: Any) -> Optional[Any]:
        """Return the filtered request, or ``None`` if there is nothing to change (or on failure: fail-open)."""
        try:
            self._tick()
            if not self._settings.enabled:
                return None
            censor = self._censor
            self._fail = None
            self._act["requests"] += 1
            self._act["last_request_at"] = self._clock()
            self._note_state_once()
            if censor.is_empty:
                self._set_last(0, 0)
                return None
            out, report = censor_request(request, censor)
            self._set_last(report.secret_replacements, report.rule_replacements)
            self._act["signed_blocks_skipped"] += report.signed_blocks_skipped
            return out if report.changed else None
        except Exception as exc:
            self._note_failure(exc)
            return None

    def process_tool_result(self, result: Any) -> Optional[str]:
        try:
            self._tick()
            s = self._settings
            if not s.enabled or not s.censor_tool_results or not isinstance(result, str) or self._censor.is_empty:
                return None
            out, n_secret, n_rule = self._censor.apply_counted(result)
            self._act["tool_results"] += 1
            return out if out is not result else None
        except Exception as exc:
            self._note_failure(exc)
            return None

    def process_auxiliary_before(self, messages: Any, key: Any) -> None:
        """Auxiliary call about to go out: filter ``messages`` IN PLACE (undone by ``process_auxiliary_after``)."""
        try:
            self._tick()
            if not self._settings.enabled or not self._settings.censor_auxiliary_calls:
                return
            censor = self._censor
            self._aux_fail = None
            self._act["aux_requests"] += 1
            if censor.is_empty:
                self._set_last_aux(0, 0)
                return
            saved, report = edit_in_place(messages, censor)
            if saved:
                self._aux.keep(key, saved)
            self._set_last_aux(report.secret_replacements, report.rule_replacements)
            self._act["signed_blocks_skipped"] += report.signed_blocks_skipped
        except Exception as exc:
            self._aux_fail = type(exc).__name__
            self._act["aux_failopen_count"] += 1
            logger.warning("hermes-censor: exception %s while filtering an auxiliary call: it goes out UNCENSORED",
                           self._aux_fail)

    def process_auxiliary_after(self, key: Any) -> None:
        """Auxiliary attempt finished (success or failure): give the caller's message objects back as they were."""
        try:
            self._aux.undo(key)
        except Exception as exc:
            logger.warning("hermes-censor: could not undo an auxiliary edit (%s)", type(exc).__name__)

    def _set_last_aux(self, secrets: int, rules: int) -> None:
        a = self._act
        a["aux_last_secret_replacements"], a["aux_last_rule_replacements"] = secrets, rules
        a["aux_total_secret_replacements"] += secrets
        a["aux_total_rule_replacements"] += rules

    def _set_last(self, secrets: int, rules: int) -> None:
        a = self._act
        a["last_secret_replacements"], a["last_rule_replacements"] = secrets, rules
        a["total_secret_replacements"] += secrets
        a["total_rule_replacements"] += rules

    def _note_failure(self, exc: Exception) -> None:
        self._fail = type(exc).__name__
        self._act["failopen_count"] += 1
        logger.warning("hermes-censor: exception %s while filtering: the request goes out UNCENSORED", self._fail)

    def _note_state_once(self) -> None:
        with self._lock:
            st = self._compute_status()
        signature = (st.level, tuple(sorted(f.code for f in st.findings if f.code in DEGRADED_CODES)))
        if signature != self._logged_signature:
            self._logged_signature = signature
            if st.level != "ACTIVE":
                logger.warning("hermes-censor: state %s (%s); see \"/censor\" or \"hermes censor status\" for details",
                               st.level, ", ".join(signature[1]) or "no rule and no vault configured")

    def activity(self) -> Dict[str, Any]:
        return dict(self._act)

    # ------------------------------------------------------------------------------------------------ status

    def status(self) -> Status:
        self._tick(force=True)
        with self._lock:
            return self._compute_status()

    def _compute_status(self) -> Status:
        s = self._settings
        findings: List[Finding] = []
        if not s.enabled:
            return Status("INACTIVE", [Finding("DISABLED", "The plugin is disabled by its configuration (enabled: false).",
                                               "Set enabled: true to re-enable it.")])
        for p in self._problems:
            findings.append(Finding("CONFIG_INVALID", p.message, p.repair or "Fix plugins.entries.hermes-censor.settings."))
        # rules
        if self._rules_state == "missing":
            keep = "; the last valid version stays applied" if self._rules_loaded_once else ""
            findings.append(Finding("RULES_MISSING", f"Rules file not found{keep}.",
                                    "Create the file or fix rules_file."))
        elif self._rules_state == "unreadable":
            keep = "; the last valid version stays applied" if self._rules_loaded_once else ""
            findings.append(Finding("RULES_UNREADABLE", f"Rules file unreadable (UTF-8 expected){keep}.",
                                    "Check the file permissions and its UTF-8 encoding."))
        errors = [i for i in self._rules_issues if i.severity == "error"]
        if errors:
            lines = ", ".join(f"line {i.line} ({i.code})" for i in errors[:10])
            more = f" and {len(errors) - 10} more" if len(errors) > 10 else ""
            findings.append(Finding("RULES_INVALID_LINES", f"{len(errors)} invalid rule line(s) ignored: {lines}{more}.",
                                    "Fix these lines (format <pattern>:<replacement>)."))
        conflicts = [i for i in self._rules_issues if i.severity == "warning"]
        if conflicts:
            findings.append(Finding("RULE_CONFLICTS", f"{len(conflicts)} duplicate rule(s) with a different replacement "
                                    "(the first one is kept).", ""))
        # vault
        kp = s.keepassxc
        if kp is not None:
            if kp.unlock == "keyfile-only":
                findings.append(Finding("KEYFILE_ONLY_NOTICE", "Unlock by key file only: the vault's security rests "
                                        "on the key file (see docs/KEEPASSXC.md).", ""))
            if self._report is None:
                if self._secret_error is None:
                    findings.append(Finding("SECRETS_NOT_LOADED", "Secret protection unavailable: no secret is loaded.",
                                            "Run /censor unlock (local password prompt); plain rules stay active."))
            else:
                r = self._report
                if r.skipped_short:
                    findings.append(Finding("SECRETS_INCOMPLETE_SHORT",
                                            f"{r.skipped_short} value(s) too short, NOT protected: " + ", ".join(r.short_fields[:10]),
                                            "Lengthen these secrets, exclude these entries (keepassxc.exclude) or lower min_secret_length."))
                if r.not_found:
                    findings.append(Finding("SECRETS_INCOMPLETE_NOT_FOUND",
                                            "Selection not found in the database: " + ", ".join(r.not_found[:10]),
                                            "Fix keepassxc.groups / keepassxc.entries (paths are relative to the root group)."))
            if self._secret_error is not None:
                e = self._secret_error
                keep = " The previous list is kept but may be stale." if self._report is not None else ""
                findings.append(Finding("SECRETS_LOAD_FAILED", f"Cannot load the secrets: {e.message}.{keep}", e.repair))
            if self._stale == "SECRETS_STALE_DB_CHANGED":
                findings.append(Finding("SECRETS_STALE_DB_CHANGED", "The KeePassXC database changed since loading: the list is stale.",
                                        "Run /censor refresh."))
            elif self._stale == "SECRETS_STALE_DB_MISSING":
                findings.append(Finding("SECRETS_STALE_DB_MISSING", "The database file is missing: the loaded list may be stale.",
                                        "Check keepassxc.database then run /censor refresh."))
            elif self._stale == "SECRETS_STALE_AGE":
                findings.append(Finding("SECRETS_STALE_AGE", f"Secrets loaded more than {s.max_age_minutes:g} minute(s) ago: list considered stale.",
                                        "Run /censor refresh."))
        if s.censor_auxiliary_calls:
            if not self.aux_hooks_registered:
                findings.append(Finding("AUX_RESTART_REQUIRED",
                                        "censor_auxiliary_calls is on but the auxiliary-call hooks were not registered "
                                        "when Hermes loaded the plugin: auxiliary calls are NOT filtered.",
                                        "Restart Hermes (these hooks are only registered at load time)."))
            else:
                findings.append(Finding("AUXILIARY_NOTICE",
                                        "Auxiliary calls (compression, title, vision...) are filtered through an "
                                        "undocumented Hermes side effect that is not verified at run time "
                                        "(docs/LIMITATIONS.md).", ""))
        if self._aux_fail:
            findings.append(Finding("AUX_REQUEST_FAILED_OPEN",
                                    f"The last auxiliary call was sent UNCENSORED (exception {self._aux_fail}).",
                                    "Check the Hermes logs (logger \"hermes-censor\"); report the problem."))
        if self._fail:
            findings.append(Finding("REQUEST_FAILED_OPEN", f"The last request was sent UNCENSORED (exception {self._fail}).",
                                    "Check the Hermes logs (logger \"hermes-censor\"); report the problem."))
        if self._fail or self._aux_fail:
            return Status("ERROR", findings)
        if any(f.code in DEGRADED_CODES for f in findings):
            return Status("DEGRADED", findings)
        if self._censor.is_empty and kp is None and self._rules_state in ("none", "missing"):
            findings.append(Finding("NOTHING_CONFIGURED", "No rule and no vault configured: nothing is filtered.",
                                    f"Create {s.rules_file} (one <pattern>:<replacement> rule per line) or configure keepassxc."))
            return Status("INACTIVE", findings)
        return Status("ACTIVE", findings)

    # ------------------------------------------------------------------------------------------------ rendering

    def render_status(self, verbose: bool = False) -> str:
        st = self.status()
        s = self._settings
        lines = [f"Hermes Censor - state: {st.level}"]
        if s.enabled:
            lines.append(f"  Rules: {self._rules_count} rule(s) loaded" + (f" from {s.rules_file}" if verbose else ""))
            kp = s.keepassxc
            if kp is None:
                lines.append("  Secrets: vault not configured")
            elif self._report is None:
                lines.append("  Secrets: NONE loaded - secret protection unavailable")
            else:
                r = self._report
                lines.append(f"  Secrets: {r.values_loaded} value(s) loaded from {r.entries_selected} entry(ies)")
                lines.append(f"  Exact scope read: {r.scope}")
            if kp is not None and self._report is None:
                lines.append(f"  Configured scope: {self._source.describe() if self._source else '?'}")
        for f in st.findings:
            lines.append(f"  - [{f.code}] {f.message}" + (f"\n      Action: {f.repair}" if f.repair else ""))
        a = self._act
        lines.append(f"  Activity: {a['requests']} request(s) examined, last one: {a['last_secret_replacements']} secret(s) and "
                     f"{a['last_rule_replacements']} rule(s) applied; {a['tool_results']} tool result(s) examined; "
                     f"{a['failopen_count']} filtering failure(s)")
        if s.enabled:
            if s.censor_auxiliary_calls:
                lines.append(f"  Auxiliary calls: filtered; {a['aux_requests']} examined, last one: "
                             f"{a['aux_last_secret_replacements']} secret(s) and {a['aux_last_rule_replacements']} "
                             f"rule(s) applied; {a['aux_failopen_count']} filtering failure(s)")
            else:
                lines.append("  Auxiliary calls (compression, title, vision...): NOT filtered "
                             "(censor_auxiliary_calls: false)")
        if verbose:
            c = self._censor.stats
            lines.append(f"  Index: {c.secret_patterns} secret pattern(s), {c.rule_patterns} rule pattern(s); "
                         f"cache: {c.cache_size_chars} chars, {c.cache_hits} hits, {c.scans} scans; "
                         f"signed blocks skipped: {a['signed_blocks_skipped']}")
        lines.append("  " + REMINDER)
        return "\n".join(lines)

    # ------------------------------------------------------------------------------------------------ command

    def command(self, raw_args: str, interactive: bool = True) -> str:
        arg = (raw_args or "").strip().lower().split()
        action = arg[0] if arg else "status"
        if action in ("status", "diagnostic", "doctor"):
            return self.render_status(verbose=len(arg) > 1 and arg[1] in ("-v", "--verbose", "verbose"))
        if action in ("unlock", "refresh", "reload-secrets"):
            if interactive:
                outcome, _ = self.start_unlock()  # never blocking: the local prompt can take a while
            else:
                outcome = self.unlock(interactive=False)
            return ("OK: " if outcome.ok else "FAILED: ") + outcome.message + "\n" + self.render_status()
        if action == "forget":
            return self.forget().message + "\n" + self.render_status()
        if action in ("rules", "reload"):
            self.reload_rules(force=True)
            return self.render_status()
        return ("Usage: /censor [status|unlock|refresh|forget|rules]\n"
                "  status  state, exact scope, warnings (default)\n"
                "  unlock  load the KeePassXC secrets (master password typed in a local window, outside the chat)\n"
                "  refresh same as unlock (after the database changed)\n"
                "  forget  forget the loaded secrets\n"
                "  rules   reload the rules file")
