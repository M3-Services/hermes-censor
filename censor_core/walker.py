"""Walk a provider request (Hermes kwargs) and filter the text meant for the model.

Principle: the WHOLE request is walked (chat, Responses, Anthropic, Converse, Gemini and unknown structures)
and strings are filtered, except:
- identity/structure fields (``role``, ``type``, ``id``, ``*_id``, ``*Id``, ``name``, ``model``...);
- headers, API keys and authentication parameters;
- signed/encrypted blocks (``signature``, ``encrypted_content``...): rewriting them would invalidate the request;
- binary data (``data:`` URLs, ``data``/``b64_json`` fields...);
- inside ``tools``/``toolConfig``/``functions``: only ``description`` and ``title`` are filtered (names, enums
  and property names are used by Hermes to validate tool calls).
Inside tool arguments (a ``tool_use`` ``input``, ``args``, ``response``...) everything is user content: no field
is excluded there.

The input request is never modified; when nothing changes the original object is returned.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Tuple

from .engine import Censor

# Values that are never filtered (normal mode), at any depth.
SKIP_KEYS = frozenset({
    "role", "type", "id", "name", "tool_name", "object", "status", "index", "finish_reason", "stop_reason",
    "model", "modelId", "media_type", "mime_type", "mimeType", "format", "detail",
    "data", "b64_json", "bytes", "file_data",
    "api_key", "apiKey", "authorization", "Authorization", "extra_headers", "headers", "extra_query",
    "tool_choice", "stream_options", "service_tier", "cache_control", "user",
    "prompt_cache_key", "safety_identifier",
})
SIGNED_MARKERS = ("signature", "encrypted_content", "thinking_signature", "thoughtSignature", "thought_signature")
TOOL_CONTAINERS = frozenset({"tools", "toolConfig", "tool_config", "functions"})
TOOL_TEXT_KEYS = frozenset({"description", "title"})
CONTENT_KEYS = frozenset({"args", "response", "json", "arguments"})  # arguments given as a dict
TOOL_USE_TYPES = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})
MAX_DEPTH = 64
DATA_URL_MIN = 256


@dataclass
class RequestReport:
    changed: bool = False
    secret_replacements: int = 0
    rule_replacements: int = 0
    strings_scanned: int = 0
    signed_blocks_skipped: int = 0
    depth_limit_hits: int = 0


def _is_identity_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return key in SKIP_KEYS or key.endswith("_id") or key.endswith("Id") or key.endswith("_ID")


class _Walker:
    def __init__(self, censor: Censor):
        self.censor = censor
        self.report = RequestReport()

    # -- leaves ---------------------------------------------------------------------------------------------

    def text(self, value: str) -> str:
        if len(value) >= DATA_URL_MIN and value.startswith("data:"):
            return value
        out, n_secret, n_rule = self.censor.apply_counted(value)
        self.report.strings_scanned += 1
        if out is not value:
            self.report.secret_replacements += n_secret
            self.report.rule_replacements += n_rule
            self.report.changed = True
        return out

    # -- modes ----------------------------------------------------------------------------------------------

    def walk(self, node: Any, depth: int = 0, content: bool = False) -> Any:
        """Normal mode (``content=False``) or user-content mode (tool arguments, ``content=True``)."""
        if isinstance(node, str):
            return self.text(node)
        if depth >= MAX_DEPTH:
            self.report.depth_limit_hits += 1
            return node
        if isinstance(node, dict):
            if not content and self._signed(node):
                self.report.signed_blocks_skipped += 1
                return node
            return self._walk_dict(node, depth, content)
        if isinstance(node, (list, tuple)):
            items = [self.walk(x, depth + 1, content) for x in node]
            if all(a is b for a, b in zip(items, node)):
                return node
            return tuple(items) if isinstance(node, tuple) else items
        return node

    @staticmethod
    def _signed(node: dict) -> bool:
        return any(isinstance(node.get(k), str) and node.get(k) for k in SIGNED_MARKERS)

    def _walk_dict(self, node: dict, depth: int, content: bool) -> Any:
        changed = {}
        node_type = node.get("type") if isinstance(node.get("type"), str) else None
        for key, value in node.items():
            if not content and _is_identity_key(key):
                continue
            if isinstance(key, str) and not content and key in TOOL_CONTAINERS:
                new = self.tool_definitions(value, depth + 1)
            elif isinstance(key, str) and not content and self._starts_content(key, node, node_type, value):
                new = self.walk(value, depth + 1, content=True)
            else:
                new = self.walk(value, depth + 1, content)
            if new is not value:
                changed[key] = new
        if not changed:
            return node
        result = dict(node) if type(node) is dict else copy.copy(node)
        result.update(changed)
        return result

    @staticmethod
    def _starts_content(key: str, node: dict, node_type: Any, value: Any) -> bool:
        if not isinstance(value, (dict, list, tuple)):
            return False
        if key in CONTENT_KEYS:
            return True
        return key == "input" and (node_type in TOOL_USE_TYPES or "toolUseId" in node)

    def tool_definitions(self, node: Any, depth: int) -> Any:
        """Tool definitions: only description texts are filtered."""
        if depth >= MAX_DEPTH:
            self.report.depth_limit_hits += 1
            return node
        if isinstance(node, dict):
            changed = {}
            for key, value in node.items():
                if isinstance(value, str):
                    new = self.text(value) if key in TOOL_TEXT_KEYS else value
                else:
                    new = self.tool_definitions(value, depth + 1)
                if new is not value:
                    changed[key] = new
            if not changed:
                return node
            result = dict(node) if type(node) is dict else copy.copy(node)
            result.update(changed)
            return result
        if isinstance(node, (list, tuple)):
            items = [self.tool_definitions(x, depth + 1) for x in node]
            if all(a is b for a, b in zip(items, node)):
                return node
            return tuple(items) if isinstance(node, tuple) else items
        return node


def censor_request(request: Any, censor: Censor) -> Tuple[Any, RequestReport]:
    """Return (filtered request, report). ``request`` is never modified."""
    walker = _Walker(censor)
    if not isinstance(request, dict):
        return request, walker.report
    out = walker.walk(request)
    return out, walker.report
