"""Payload walking: what is filtered, what is preserved."""
import copy
import json

from censor_core.engine import Censor
from censor_core.rules import Rule
from censor_core.walker import censor_request

S = "Sekret-Value-42"
CENSOR = Censor.build(rules=[Rule("fox", "animal", 1)], secrets=[S])


def run(request):
    before = copy.deepcopy(request)
    out, report = censor_request(request, CENSOR)
    assert request == before, "the input request must never be modified"
    return out, report


def test_unchanged_request_returns_same_object_and_no_change():
    req = {"model": "m", "messages": [{"role": "user", "content": "nothing"}]}
    out, report = run(req)
    assert out is req and not report.changed


# ---------- OpenAI chat completions ----------

def test_chat_messages_roles_ids_names_preserved():
    req = {
        "model": "fox-model",
        "messages": [
            {"role": "system", "content": f"sys {S}"},
            {"role": "user", "content": "the fox", "name": "fox"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_fox", "type": "function",
                 "function": {"name": "fox_tool", "arguments": json.dumps({"pw": S, "n": "fox"})}}]},
            {"role": "tool", "tool_call_id": "call_fox", "name": "fox_tool", "content": f"out {S}"},
        ],
        "stream": True,
    }
    out, report = run(req)
    assert out["model"] == "fox-model" and out["stream"] is True
    m = out["messages"]
    assert m[0] == {"role": "system", "content": "sys [SECRET]"}
    assert m[1] == {"role": "user", "content": "the animal", "name": "fox"}
    call = m[2]["tool_calls"][0]
    assert call["id"] == "call_fox" and call["type"] == "function"
    assert call["function"]["name"] == "fox_tool"
    assert json.loads(call["function"]["arguments"]) == {"pw": "[SECRET]", "n": "animal"}
    assert m[3] == {"role": "tool", "tool_call_id": "call_fox", "name": "fox_tool", "content": "out [SECRET]"}
    assert report.changed and report.secret_replacements == 3 and report.rule_replacements == 2


def test_chat_content_parts_text_only():
    req = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": f"here is {S}"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA" + "A" * 3000, "detail": "high"}},
        {"type": "image_url", "image_url": {"url": f"https://ex.test/?k={S}"}},
    ]}]}
    out, _ = run(req)
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "here is [SECRET]"}
    assert parts[1] == req["messages"][0]["content"][1]  # data: URL intact
    assert parts[2]["image_url"]["url"] == "https://ex.test/?k=[SECRET]"


def test_tools_only_descriptions_are_filtered():
    req = {"tools": [{"type": "function", "function": {
        "name": "fox_search", "description": f"searches {S} and fox",
        "parameters": {"type": "object", "properties": {
            "fox": {"type": "string", "description": "the fox", "enum": ["fox", "wolf"]}},
            "required": ["fox"]}}}],
        "tool_choice": {"type": "function", "function": {"name": "fox_search"}}}
    out, _ = run(req)
    fn = out["tools"][0]["function"]
    assert fn["name"] == "fox_search"
    assert fn["description"] == "searches [SECRET] and animal"
    prop = fn["parameters"]["properties"]["fox"]
    assert prop["description"] == "the animal"
    assert prop["enum"] == ["fox", "wolf"] and fn["parameters"]["required"] == ["fox"]
    assert out["tool_choice"] == req["tool_choice"]


def test_reasoning_text_scanned_but_signed_blocks_untouched():
    req = {"messages": [{"role": "assistant", "content": "ok", "reasoning_content": f"thinks {S}",
                         "reasoning_details": [
                             {"type": "reasoning.text", "text": f"signed {S}", "signature": "sigSIG"},
                             {"type": "reasoning.summary", "summary": f"summary {S}"}]}]}
    out, report = run(req)
    msg = out["messages"][0]
    assert msg["reasoning_content"] == "thinks [SECRET]"
    assert msg["reasoning_details"][0]["text"] == f"signed {S}"  # signature: never rewritten
    assert msg["reasoning_details"][1]["summary"] == "summary [SECRET]"
    assert report.signed_blocks_skipped == 1


# ---------- Responses API ----------

def test_responses_api_input_and_instructions():
    req = {"model": "m", "instructions": f"rule {S}", "input": [
        {"type": "message", "role": "user", "id": "msg_1", "content": [{"type": "input_text", "text": f"a {S}"}]},
        {"type": "function_call", "call_id": "call_1", "name": "run", "arguments": json.dumps({"k": S})},
        {"type": "function_call_output", "call_id": "call_1", "output": f"res {S}"},
        {"type": "function_call_output", "call_id": "call_2", "output": [{"type": "input_text", "text": f"r2 {S}"}]},
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC" + S, "summary": []},
    ], "store": False}
    out, _ = run(req)
    assert out["instructions"] == "rule [SECRET]"
    i = out["input"]
    assert i[0] == {"type": "message", "role": "user", "id": "msg_1",
                    "content": [{"type": "input_text", "text": "a [SECRET]"}]}
    assert json.loads(i[1]["arguments"]) == {"k": "[SECRET]"} and i[1]["call_id"] == "call_1"
    assert i[2] == {"type": "function_call_output", "call_id": "call_1", "output": "res [SECRET]"}
    assert i[3]["output"][0]["text"] == "r2 [SECRET]"
    assert i[4]["encrypted_content"] == "ENC" + S


def test_responses_input_can_be_plain_string():
    out, _ = run({"input": f"hello {S}"})
    assert out["input"] == "hello [SECRET]"


# ---------- Anthropic Messages ----------

def test_anthropic_messages():
    req = {"model": "claude", "max_tokens": 10,
           "system": [{"type": "text", "text": f"sys {S}", "cache_control": {"type": "ephemeral"}}],
           "messages": [
               {"role": "user", "content": [{"type": "text", "text": f"u {S}"}]},
               {"role": "assistant", "content": [
                   {"type": "thinking", "thinking": f"hmm {S}", "signature": "sig"},
                   {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"name": S, "cmd": "fox"}}]},
               {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "toolu_1", "content": f"out {S}"},
                   {"type": "tool_result", "tool_use_id": "toolu_2", "content": [{"type": "text", "text": f"o2 {S}"}]},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ" * 2000}}]}],
           "tools": [{"name": "run", "description": f"d {S}", "input_schema": {"type": "object"}}]}
    out, report = run(req)
    assert out["system"][0]["text"] == "sys [SECRET]" and out["system"][0]["cache_control"] == {"type": "ephemeral"}
    a, b, c = out["messages"]
    assert a["content"][0]["text"] == "u [SECRET]"
    assert b["content"][0]["thinking"] == f"hmm {S}"  # signed block: intact
    assert b["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "run",
                               "input": {"name": "[SECRET]", "cmd": "animal"}}
    assert c["content"][0]["content"] == "out [SECRET]"
    assert c["content"][1]["content"][0]["text"] == "o2 [SECRET]"
    assert c["content"][2] == req["messages"][2]["content"][2]
    assert out["tools"][0]["description"] == "d [SECRET]" and out["tools"][0]["name"] == "run"
    assert report.signed_blocks_skipped == 1


def test_anthropic_system_plain_string():
    out, _ = run({"system": f"sys {S}", "messages": []})
    assert out["system"] == "sys [SECRET]"


# ---------- Bedrock Converse ----------

def test_bedrock_converse():
    req = {"modelId": "fox.model", "system": [{"text": f"sys {S}"}],
           "messages": [
               {"role": "user", "content": [{"text": f"u {S}"}]},
               {"role": "assistant", "content": [{"toolUse": {"toolUseId": "tu1", "name": "run", "input": {"a": S}}}]},
               {"role": "user", "content": [{"toolResult": {"toolUseId": "tu1", "content": [
                   {"text": f"t {S}"}, {"json": {"k": S}}]}}]}],
           "toolConfig": {"tools": [{"toolSpec": {"name": "run", "description": f"d {S}",
                                                  "inputSchema": {"json": {"type": "object"}}}}]}}
    out, _ = run(req)
    assert out["modelId"] == "fox.model"
    assert out["system"] == [{"text": "sys [SECRET]"}]
    m = out["messages"]
    assert m[0]["content"] == [{"text": "u [SECRET]"}]
    assert m[1]["content"][0]["toolUse"] == {"toolUseId": "tu1", "name": "run", "input": {"a": "[SECRET]"}}
    assert m[2]["content"][0]["toolResult"]["content"] == [{"text": "t [SECRET]"}, {"json": {"k": "[SECRET]"}}]
    assert out["toolConfig"]["tools"][0]["toolSpec"]["description"] == "d [SECRET]"
    assert out["toolConfig"]["tools"][0]["toolSpec"]["name"] == "run"


# ---------- native Gemini and unknown structures ----------

def test_gemini_contents_parts():
    req = {"model": "g", "contents": [{"role": "user", "parts": [
        {"text": f"u {S}"}, {"inlineData": {"mimeType": "image/png", "data": "QQ" * 2000}},
        {"functionCall": {"name": "run", "args": {"x": S}}},
        {"functionResponse": {"name": "run", "response": {"out": S}}}]}]}
    out, _ = run(req)
    parts = out["contents"][0]["parts"]
    assert parts[0]["text"] == "u [SECRET]"
    assert parts[1] == req["contents"][0]["parts"][1]
    assert parts[2]["functionCall"] == {"name": "run", "args": {"x": "[SECRET]"}}
    assert parts[3]["functionResponse"] == {"name": "run", "response": {"out": "[SECRET]"}}


def test_unknown_structures_are_scanned_but_headers_and_auth_never():
    req = {"model": "m", "extra_headers": {"Authorization": f"Bearer {S}", "x-fox": "fox"},
           "api_key": S, "headers": {"k": S}, "provider_thing": {"deep": [f"a {S}"]},
           "extra_body": {"metadata": {"note": f"n {S}"}}}
    out, _ = run(req)
    assert out["extra_headers"] == req["extra_headers"]
    assert out["api_key"] == S and out["headers"] == req["headers"]
    assert out["provider_thing"] == {"deep": ["a [SECRET]"]}
    assert out["extra_body"]["metadata"]["note"] == "n [SECRET]"


def test_non_dict_and_odd_leaves_are_left_alone():
    class Odd:
        def __deepcopy__(self, memo):
            return self
    odd = Odd()
    req = {"messages": [{"role": "user", "content": f"x {S}", "obj": odd, "n": 3, "b": b"bytes", "t": (f"{S}", 1)}]}
    out, _ = run(req)
    m = out["messages"][0]
    assert m["obj"] is odd and m["n"] == 3 and m["b"] == b"bytes"
    assert m["t"] == ("[SECRET]", 1) and isinstance(m["t"], tuple)
    assert censor_request("not a dict", CENSOR)[0] == "not a dict"


def test_report_counts_strings_scanned():
    out, report = run({"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]})
    assert report.strings_scanned == 2 and not report.changed
