"""Integration harness: REAL Hermes (hermes_cli/AIAgent) + local fake LLM provider.

Nothing touches the user's profile: HERMES_HOME points to a temporary folder, the plugin is copied there and
enabled, and the provider is a local HTTP server that records exactly what it receives.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IGNORE = shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache", "bench", "*.pyc", "run_tests.sh")

TEST_TOOLS_INIT = '''
import json

SCHEMA = {"name": "vault_show", "description": "Returns a fictional value (test).",
          "parameters": {"type": "object", "properties": {}, "required": []}}

def _handler(args, **kwargs):
    return json.dumps({"output": "the password is %s end" % __import__("os").environ["CENSOR_TEST_TOOL_VALUE"]})

def register(ctx):
    ctx.register_tool(name="vault_show", toolset="censor_test", schema=SCHEMA, handler=_handler)
'''
TEST_TOOLS_YAML = "name: censor-test-tools\nversion: 0.0.1\ndescription: test tool\nprovides_tools:\n  - vault_show\n"


LLM_PATHS = ("/chat/completions", "/responses", "/messages")


class FakeProvider:
    """Fake server: ``script(index, path, payload) -> dict(status=..., json=...)`` or None (default)."""

    def __init__(self, script: Optional[Callable[[int, str, dict], Any]] = None):
        self.requests: List[Dict[str, Any]] = []      # everything that arrives (including metadata probes)
        self.llm_requests: List[Dict[str, Any]] = []  # LLM calls only (chat / responses / messages)
        self.script = script
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencieux
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                payload = json.loads(raw or b"{}")
                is_llm = self.path.endswith(LLM_PATHS)
                index = len(provider.llm_requests)
                entry = {"path": self.path, "payload": payload, "raw": raw.decode("utf-8"), "headers": dict(self.headers)}
                provider.requests.append(entry)
                if is_llm:
                    provider.llm_requests.append(entry)
                reply = provider.script(index, self.path, payload) if (provider.script and is_llm) else None
                reply = reply or default_text_reply(self.path, payload, "ok")
                status = reply.get("status", 200)
                if status != 200:
                    body = json.dumps(reply.get("json") or {"error": {"message": "fake error", "type": "server_error"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if payload.get("stream"):
                    events = reply["sse"](payload) if "sse" in reply else []
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for name, data in events:
                        if name:
                            self.wfile.write(f"event: {name}\n".encode())
                        self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                    return
                body = json.dumps(reply["json"]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @property
    def anthropic_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self) -> List[str]:
        """Raw bodies of the LLM calls received (what the "provider" actually saw)."""
        return [r["raw"] for r in self.llm_requests]

    def all_bodies(self) -> List[str]:
        return [r["raw"] for r in self.requests]


# ---------- canned replies ----------

def _chat_sse(message: dict, finish: str):
    def build(payload):
        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": payload.get("model", "m")}
        events = []
        delta = {"role": "assistant"}
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(message["tool_calls"])]
        events.append((None, json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})))
        events.append((None, json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})))
        events.append((None, json.dumps({**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})))
        events.append((None, "[DONE]"))
        return events
    return build


def chat_reply(message: dict, finish: str = "stop") -> dict:
    return {"json": {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "fake-model",
                     "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            "sse": _chat_sse(message, finish)}


def chat_tool_call(name: str, arguments: str = "{}", call_id: str = "call_1") -> dict:
    return chat_reply({"content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}, "tool_calls")


def responses_reply(text: str, tool_call: Optional[tuple] = None) -> dict:
    if tool_call:
        name, args, call_id = tool_call
        output = [{"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name, "arguments": args, "status": "completed"}]
    else:
        output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]}]
    resp = {"id": "resp_1", "object": "response", "created_at": 1, "status": "completed", "model": "fake-model",
            "output": output, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

    def sse(payload):
        events = [("response.created", json.dumps({"type": "response.created", "response": {**resp, "status": "in_progress", "output": []}}))]
        for i, item in enumerate(output):
            events.append(("response.output_item.added", json.dumps({"type": "response.output_item.added", "output_index": i, "item": item})))
            events.append(("response.output_item.done", json.dumps({"type": "response.output_item.done", "output_index": i, "item": item})))
        events.append(("response.completed", json.dumps({"type": "response.completed", "response": resp})))
        return events
    return {"json": resp, "sse": sse}


def anthropic_reply(text: str, tool_use: Optional[tuple] = None) -> dict:
    if tool_use:
        name, inp, tid = tool_use
        content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
        stop = "tool_use"
    else:
        content = [{"type": "text", "text": text}]
        stop = "end_turn"
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "fake-model", "content": content,
           "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}

    def sse(payload):
        ev = [("message_start", json.dumps({"type": "message_start", "message": {**msg, "content": []}}))]
        for i, block in enumerate(content):
            if block["type"] == "text":
                ev.append(("content_block_start", json.dumps({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})))
                ev.append(("content_block_delta", json.dumps({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}})))
            else:
                ev.append(("content_block_start", json.dumps({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})))
                ev.append(("content_block_delta", json.dumps({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})))
            ev.append(("content_block_stop", json.dumps({"type": "content_block_stop", "index": i})))
        ev.append(("message_delta", json.dumps({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}})))
        ev.append(("message_stop", json.dumps({"type": "message_stop"})))
        return ev
    return {"json": msg, "sse": sse}


def default_text_reply(path: str, payload: dict, text: str) -> dict:
    if path.endswith("/responses"):
        return responses_reply(text)
    if path.endswith("/messages"):
        return anthropic_reply(text)
    return chat_reply({"content": text})


# ---------- isolated Hermes profile ----------

def make_hermes_home(root: str, settings: Dict[str, Any], extra_config: Optional[Dict[str, Any]] = None) -> str:
    """Prepare an ISOLATED HERMES_HOME (never the user's) with the plugin and a test tool plugin.

    ``HERMES_TEST_HOME``: persistent test profile whose Hermes dependency runtime is already provisioned
    (the first bootstrap of a blank profile takes several minutes); otherwise a folder under ``root``.
    Plugins and config.yaml are rewritten on each call; nothing else is required there.
    """
    import yaml
    home = os.environ.get("HERMES_TEST_HOME") or os.path.join(root, "hermes_home")
    real = os.path.normcase(os.path.abspath(os.path.join(os.path.expanduser("~"), ".hermes")))
    local = os.path.normcase(os.path.abspath(os.path.join(os.environ.get("LOCALAPPDATA", "?"), "hermes")))
    if os.path.normcase(os.path.abspath(home)) in (real, local):
        raise RuntimeError("HERMES_TEST_HOME must never be the user's real profile")
    for sub in ("hermes-censor", "censor-test-tools"):
        shutil.rmtree(os.path.join(home, "plugins", sub), ignore_errors=True)
    os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, os.path.join(home, "plugins", "hermes-censor"), ignore=IGNORE, dirs_exist_ok=True)
    tools = os.path.join(home, "plugins", "censor-test-tools")
    os.makedirs(tools, exist_ok=True)
    with open(os.path.join(tools, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(TEST_TOOLS_INIT)
    with open(os.path.join(tools, "plugin.yaml"), "w", encoding="utf-8") as fh:
        fh.write(TEST_TOOLS_YAML)
    config = {"plugins": {"enabled": ["hermes-censor", "censor-test-tools"],
                          "entries": {"hermes-censor": {"settings": settings}}},
              "tools": {"tool_search": {"enabled": "off"}}}
    if extra_config:
        config.update(extra_config)
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True)
    return home
