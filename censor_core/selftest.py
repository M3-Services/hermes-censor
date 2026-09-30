"""``hermes censor selftest``: does the auxiliary-call mechanism still work on THIS Hermes?

The ``censor_auxiliary_calls`` option relies on an undocumented Hermes behaviour (edits made in place to the message
dictionaries given to ``pre_auxiliary_call`` reach the provider). Nothing at run time can notice that a Hermes update
removed it, so this command checks it on demand, with no test infrastructure:

- a throw-away rule (a random fictional canary) and a throw-away ``Runtime`` are used: your rules and secrets are not
  read and nothing you configured is touched;
- a local HTTP server on ``127.0.0.1`` plays the provider and records the bytes it receives;
- a real auxiliary call goes through Hermes' own ``call_llm`` (plain and streaming), with the plugin's own hooks
  (``auxiliary.hooks_for``, the same glue as the real plugin) registered through the public ``register_hook`` API.

Hermes is imported lazily: this module is only useful inside a Hermes process.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Tuple

from .auxiliary import hooks_for
from .runtime import Runtime

MASK = "SELFTEST_MASKED"
TASK = "censor_selftest"  # not a configured Hermes task: explicit provider/base_url below are all that is used


class _Loopback:
    """A provider that listens on 127.0.0.1 only and keeps the last request body."""

    def __init__(self) -> None:
        self.last_body = ""
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode("utf-8", "replace")
                owner.last_body = raw
                owner.requests += 1
                try:
                    streaming = bool(json.loads(raw).get("stream"))
                except Exception:
                    streaming = False
                base = {"id": "selftest", "created": 0, "model": "selftest-model"}
                usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                if streaming:
                    first = {**base, "object": "chat.completion.chunk", "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]}
                    last = {**base, "object": "chat.completion.chunk", "usage": usage,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    body = f"data: {json.dumps(first)}\n\ndata: {json.dumps(last)}\n\ndata: [DONE]\n\n".encode()
                    ctype = "text/event-stream"
                else:
                    body = json.dumps({**base, "object": "chat.completion", "usage": usage, "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _hermes_version() -> str:
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:
        return "unknown"


def run(register_hook: Callable[[str, Callable], Any], echo: Callable[[str], None] = print) -> int:
    """Run the checks; return 0 if the mechanism works here, 1 if it does not, 2 if it cannot be tested."""
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:
        echo(f"Cannot import Hermes' auxiliary client ({type(exc).__name__}); run this as `hermes censor selftest`.")
        return 2

    canary = "selftest-canary-" + uuid.uuid4().hex
    results: List[Tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-censor-selftest-") as home:
        rules = os.path.join(home, "rules.txt")
        with open(rules, "w", encoding="utf-8") as fh:
            fh.write(f"{canary}:{MASK}\n")
        runtime = Runtime(settings_provider=lambda: {"rules_file": rules, "censor_auxiliary_calls": True}, home=home)
        runtime.aux_hooks_registered = True
        pre, post = hooks_for(runtime)
        register_hook("pre_auxiliary_call", pre)
        register_hook("post_auxiliary_call", post)
        server = _Loopback()
        try:
            for label, stream in (("plain call", False), ("streaming call", True)):
                messages = [{"role": "system", "content": "summarise"},
                            {"role": "user", "content": f"the value is {canary} end"}]
                original = copy.deepcopy(messages)
                try:
                    reply = call_llm(TASK, provider="custom", base_url=server.base_url, api_key="selftest",
                                     model="selftest-model", messages=messages, stream=stream, timeout=30)
                    if stream:
                        list(reply)
                except Exception as exc:
                    results.append((label, False, f"the call failed ({type(exc).__name__})"))
                    continue
                body = server.last_body
                if not server.requests:
                    results.append((label, False, "the local provider received nothing"))
                elif canary in body:
                    results.append((label, False, "the provider received the canary UNFILTERED: the mechanism no "
                                                  "longer works on this Hermes"))
                elif MASK not in body:
                    results.append((label, False, "the canary is gone but the replacement is missing"))
                elif messages != original:
                    results.append((label, False, "filtered on the wire, but the caller's own messages were left "
                                                  "edited (the undo did not run)"))
                else:
                    results.append((label, True, "provider received only the replacement; caller's objects intact"))
        finally:
            server.close()

    echo(f"Hermes Censor selftest (Hermes {_hermes_version()}): auxiliary-call filtering")
    for label, ok, detail in results:
        echo(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    if results and all(ok for _, ok, _ in results):
        echo("Result: OK. `censor_auxiliary_calls` works on this Hermes (it still depends on an undocumented behaviour).")
        return 0
    echo("Result: FAILED. Do not rely on `censor_auxiliary_calls` with this Hermes: set it to false and report the "
         "Hermes version (docs/LIMITATIONS.md, section 3).")
    return 1
