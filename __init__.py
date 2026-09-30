"""Hermes Censor - Hermes Agent plugin (entry point ``register(ctx)``).

Hermes surfaces used (all documented):
- ``register_middleware("llm_request")``: rewrites the provider request before it runs (main filtering);
- ``register_hook("transform_tool_result")``: filters a tool result before it is added to the conversation;
- ``register_command("censor")``: the /censor slash command (state, unlock, refresh);
- ``register_cli_command("censor")``: ``hermes censor status|check-rules|test-unlock|mask``.
All the logic lives in ``censor_core`` (no dependency on Hermes).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from .censor_core.runtime import Runtime

logger = logging.getLogger("hermes-censor")

SETTING_KEYS = ("enabled", "rules_file", "ignore_case", "secret_replacement", "censor_tool_results",
                "min_secret_length", "max_age_minutes", "keepassxc")
_RUNTIME: Optional[Runtime] = None


def _hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _settings_provider(ctx):
    def read() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in SETTING_KEYS:
            value = ctx.get_config(key)
            if value is not None:
                out[key] = value
        return out
    return read


def get_runtime() -> Optional[Runtime]:
    return _RUNTIME


def register(ctx) -> None:
    global _RUNTIME
    from .censor_core.cli import register_cli
    runtime = Runtime(settings_provider=_settings_provider(ctx), home=_hermes_home())
    _RUNTIME = runtime

    def on_llm_request(**kwargs):
        out = runtime.process_request(kwargs.get("request"))
        if out is None:
            return None
        a = runtime.activity()
        return {"request": out, "source": "hermes-censor",
                "reason": f"{a['last_secret_replacements']} secret(s), {a['last_rule_replacements']} rule(s) applied"}

    def on_transform_tool_result(**kwargs):
        return runtime.process_tool_result(kwargs.get("result"))

    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    ctx.register_command(
        "censor", handler=lambda raw_args="": runtime.command(raw_args, interactive=True),
        description="Hermes Censor: state, KeePassXC unlock, refresh",
        args_hint="[status|unlock|refresh|forget|rules]")
    register_cli(ctx, runtime_factory=lambda channels: _cli_runtime(ctx, channels))
    logger.info("hermes-censor loaded (details: /censor or hermes censor status)")


def _cli_runtime(ctx, channels) -> Runtime:
    """Runtime dedicated to the ``hermes censor`` subcommands (additionally allows typing in the terminal)."""
    from dataclasses import replace

    from .censor_core.keepassxc import KeePassXCSource

    def factory(cfg):
        return KeePassXCSource(replace(cfg, channels=tuple(channels)))

    return Runtime(settings_provider=_settings_provider(ctx), home=_hermes_home(), source_factory=factory)
