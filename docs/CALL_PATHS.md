# LLM call path matrix

Established **experimentally** with the real Hermes (v0.21.5, Python 3.14.7) and a local fake provider that records
the bytes it receives (`tests/integration/`). Every "covered" path is run **twice**: with the plugin (the fictional
value appears in no received body) and **without** the plugin (control: the value MUST appear, otherwise the test
would prove nothing).

Legend: **tested and covered** = the protected string does not reach the provider; **tested and not covered** = it
does (observed); **not verified** = not exercised here, never to be read as "covered".

## What goes through `llm_request`

`apply_llm_request_middleware` is called in **a single place** in Hermes: `agent/turn_api_request.py`, on every
attempt of the main conversation loop (read from the code, confirmed by the tests).

| # | Path | Status | Evidence |
|---|---|---|---|
| 1 | Main loop, Chat Completions API (streaming), tool result containing a fictional KeePassXC value | **tested and covered** | `test_main_loop_tool_result_and_history_are_censored` + control `test_control_with_plugin_disabled...` |
| 2 | Retry after a 500 server error | **tested and covered** | `test_retry_after_provider_error_is_censored_on_every_attempt[...]` (both attempts carrying the tool result) |
| 3 | Fallback provider (`fallback_model`, primary answering 429) | **tested and covered** (primary AND fallback) | `test_fallback_provider_receives_censored_request[...]` |
| 4 | Subagent (`delegate_task`, asynchronous: its call arrives from another thread) | **tested and covered** | `test_subagent_requests_are_censored[...]` |
| 5 | Responses API (`api_mode: codex_responses`) with a tool call | **tested and covered** | `test_responses_api_path[...]` |
| 6 | Anthropic Messages API (`anthropic_messages`, SDK 0.87.0) with `tool_use` / `tool_result` | **tested and covered** | `test_anthropic_messages_path[...]` |
| 7 | Bedrock Converse | **not verified** end to end (walking the `system/messages/toolUse/toolResult/toolConfig` structure is tested on a sample payload) | `test_walker.py::test_bedrock_converse` |
| 8 | Native Gemini (`contents/parts`) | **not verified** end to end (generic structure tested on a sample) | `test_walker.py::test_gemini_contents_parts` |
| 9 | Multimodal content (`image_url`, base64 `image`) | **not verified** end to end (text filtered, binary data left intact: tested on sample payloads) | `test_walker.py` |
| 10 | Non-streaming path of the main loop | **not verified** (the tests observe the streaming path that Hermes chooses) | - |
| 11 | Gateway sessions (Telegram...), cron, TUI, Desktop | **not verified**: same `AIAgent` objects as the CLI according to the code, but none of these surfaces was exercised | - |
| 12 | MoA (mixture of agents) | **not verified** | - |

## What does NOT go through `llm_request`

| # | Path | Status | Evidence |
|---|---|---|---|
| 13 | **Auxiliary calls** (context compression, session title, vision, approval, MoA advisors...): `agent.auxiliary_client` | **tested and not covered by default** (sample: the `compression` task through `call_llm` - the fictional value and the rules' word reach the provider as-is). **Tested and covered with the opt-in `censor_auxiliary_calls: true`** for the messages of synchronous, retried, streaming and asynchronous `call_llm` / `async_call_llm` calls (tasks `compression`, `title_generation`, `session_search`, `approval`, `moa_aggregator`); a separate `system`/`instructions` string and tool definitions stay unfiltered; relies on an undocumented Hermes behaviour (see `LIMITATIONS.md`). | `test_auxiliary_calls_do_NOT_traverse_llm_request` (option off), `tests/integration/test_hermes_aux.py` (option on) |
| 14 | `codex_app_server` mode (the whole turn is delegated to a Codex subprocess before the loop) | **not covered according to the code** (`conversation_loop.py`), **not exercised** | - |

Observed outside the test harness (real session, `/compress` then inspection of `state.db`): the compression summary
and the generated session title contained a rule word in clear, so both auxiliary tasks received unfiltered text
(details in `LIMITATIONS.md`, section 3).

### Note on auxiliary calls (opt-in, undocumented mechanism)

The official `pre_auxiliary_call` hook fires for every auxiliary attempt but is **observer-only** (return value
ignored). The experiment `test_experiment_pre_auxiliary_call_hook_is_observer_only` shows that an **in-place
mutation** of the message dictionaries received by this hook *does* reach the provider (Hermes copies only the list,
not the dictionaries). This is an undocumented side effect. The plugin uses it **only if you turn on
`censor_auxiliary_calls`** (off by default), and undoes its edit afterwards so the caller's objects are left intact;
the experiment test stays in the suite as a canary for a Hermes change. Limits, measurements and how to check it on
your installation: `LIMITATIONS.md`, section 3. The real fix belongs in Hermes: call `apply_llm_request_middleware`
from the auxiliary funnel.

## Complementary layers

| Layer | Role | Status |
|---|---|---|
| `transform_tool_result` | censors a tool result **before** it is added to the conversation: the kept history (hence any later request, including auxiliary/compression) already no longer contains the value | **tested and covered** (`result["messages"]` history without the value; survives a middleware failure: `test_defense_in_depth_...`). **Does not cover** what was already displayed or logged earlier, nor secrets typed by the user. |
| Logs / display / local session database | unchanged by the plugin | see `LIMITATIONS.md` |

## Fail-open (real demonstration)

`test_failopen_is_real_and_is_detected`: when the plugin raises an exception, the request **goes out uncensored** (the
fictional value is received by the provider) and the agent carries on normally; the plugin switches to **ERROR**
(`REQUEST_FAILED_OPEN`, exception name only) and returns to ACTIVE as soon as a request is processed.
