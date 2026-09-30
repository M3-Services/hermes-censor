"""``hermes censor ...`` subcommands (a CLI process separate from any Hermes session)."""
from __future__ import annotations

import sys
from typing import Callable

from .rules import parse_rules
from .runtime import Runtime

NOTE = ("Note: this command runs in its own process; it does not see the state of a running Hermes session "
        "(use /censor inside the session). Secrets loaded here are forgotten when the command ends.")


def register_cli(ctx, runtime_factory: Callable[[tuple], Runtime]) -> None:
    def setup(parser):
        sub = parser.add_subparsers(dest="censor_cmd")
        p = sub.add_parser("status", help="configuration state (rules, vault, warnings)")
        p.add_argument("-v", "--verbose", action="store_true")
        p = sub.add_parser("check-rules", help="validate a rules file (line numbers only, never the content)")
        p.add_argument("file", nargs="?")
        sub.add_parser("test-unlock", help="test the KeePassXC unlock (password typed in the terminal or a local "
                                           "window; prints counters only)")
        p = sub.add_parser("mask", help="filter the text read on standard input with the rules (and the secrets with --secrets)")
        p.add_argument("--secrets", action="store_true", help="also load the KeePassXC secrets (local prompt)")

    def handler(args) -> int:
        cmd = getattr(args, "censor_cmd", None) or "status"
        if cmd == "check-rules":
            return _check_rules(runtime_factory(("askpass", "gui")), getattr(args, "file", None))
        if cmd == "test-unlock":
            rt = runtime_factory(("askpass", "gui", "tty"))
            outcome = rt.unlock(interactive=True)
            print(("OK: " if outcome.ok else "FAILED: ") + outcome.message)
            print(rt.render_status(verbose=True))
            print(NOTE)
            return 0 if outcome.ok else 1
        if cmd == "mask":
            rt = runtime_factory(("askpass", "gui", "tty"))
            if getattr(args, "secrets", False):
                outcome = rt.unlock(interactive=True)
                print(("secrets: " if outcome.ok else "secrets unavailable: ") + outcome.message, file=sys.stderr)
            text = sys.stdin.read()
            out = rt.process_request({"messages": [{"role": "user", "content": text}]})
            sys.stdout.write(out["messages"][0]["content"] if out else text)
            return 0
        rt = runtime_factory(("askpass", "gui", "tty"))
        print(rt.render_status(verbose=bool(getattr(args, "verbose", False))))
        print(NOTE)
        return 0

    ctx.register_cli_command(name="censor", help="Hermes Censor: state, rules validation, unlock test",
                             setup_fn=setup, handler_fn=handler,
                             description="Anonymise the content sent to LLM providers (rules + KeePassXC).")


def _check_rules(rt: Runtime, file) -> int:
    path = file or rt.settings.rules_file
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"Unreadable file ({type(exc).__name__}): {path}")
        return 1
    result = parse_rules(text)
    print(f"{len(result.rules)} valid rule(s) in {path}")
    for issue in result.issues:
        print(f"  [{issue.severity}] {issue.message}")
    return 1 if any(i.severity == "error" for i in result.issues) else 0
