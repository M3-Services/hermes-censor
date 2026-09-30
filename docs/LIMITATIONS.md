# Limitations - read before trusting the plugin

**Hermes Censor is a practical protection that reports the failures it can detect. It is NOT a guarantee of
non-disclosure, NOT a firewall and NOT a network block.**

## 1. Hermes is "fail-open" (an API limit the plugin cannot work around)

- Middleware errors are documented as fail-open: "Hermes logs a warning and continues". If the plugin raises an
  exception, the request **goes out uncensored** (demonstrated: `test_failopen_is_real_and_is_detected`). The plugin
  detects the exceptions it controls and switches to **ERROR**; it cannot prevent the request from being sent.
- **A disabled plugin, one that is not loaded, or one that crashes on import cannot display its own warning.** Check
  `hermes plugins list` and `/censor` from time to time.
- The plugin cannot block a request: no Hermes middleware API allows it.

## 2. Literal search

**Missed**: a secret that is reworded, encoded (base64, URL, hex...), split across messages or fragments, interleaved
with inserted characters, absent from the index (not loaded, too short, outside the selection), or already sent through
another surface. A secret that **you type yourself** in a message is filtered if it is in the index, but stays stored
as-is locally (see section 4). Matches are substrings: `abcdef12` is masked inside `xabcdef12y`. Forms covered in
addition to the raw form: NFC/NFD and JSON escapes (ASCII and UTF-8).

## 3. Routes not covered or not verified

See `CALL_PATHS.md`. In short: **auxiliary calls** (context compression, title, vision, approval...) **do not go
through** `llm_request` (observed) and are **not filtered by default**: turn on the opt-in `censor_auxiliary_calls`
setting (see below); `codex_app_server` bypasses it according to the code; several surfaces (Bedrock,
native Gemini, gateways, cron, non-streaming, MoA) were **not** exercised. The `transform_tool_result` layer reduces
the exposure of values coming from tools (the history no longer contains them, hence neither does compression), but
not that of a secret typed by the user.

**Smallest honest change in Hermes** (outside the plugin): call `apply_llm_request_middleware` from the auxiliary
call funnel (`agent/auxiliary_client.py`, where `pre_auxiliary_call` is emitted). Until then, the option below is the
best a plugin can do.

**Observed on a real session** (plugin active, `/compress` run in a normal Hermes conversation, then the local
`state.db` inspected). These two observations confirm the matrix of `CALL_PATHS.md` outside the test harness:

- **Context compression sent unmasked text to the provider.** The summary written by the model contained a rule word
  in clear in its "Detailed Session Log" (the user message as typed), whereas the main loop had only ever shown the
  model the replacement. The summarizer therefore received the original message, not the filtered one. The typed
  secret itself appeared as `[REDACTED]` in that part of the summary: that is the summarizer's own instruction, not
  this plugin, and it is **not** proof that the secret was absent from the compression request (very likely it was
  present, since the rule word was). `session_model_usage` had no "compression" row for that session, so the counter
  cannot confirm it either way.
- **Session title generation sent unmasked text too.** The generated title contained the rule word in clear
  (`title_generation` is an auxiliary task: same funnel, same gap).
- What a test such as "compress, then ask the model to repeat my first prompts" shows is that the **next main
  requests** are masked (the summary is stored locally and goes back through `llm_request`). It does not show that
  the compression request itself was masked.

**No documented plugin-side fix exists in Hermes.** `pre_auxiliary_call` / `post_auxiliary_call` are observer-only
(return value ignored); `llm_request` and `llm_execution` middleware are applied in the main loop only
(`agent/turn_api_request.py`, `agent/turn_api_call.py`).

### Opt-in option: `censor_auxiliary_calls` (experimental, off by default)

The only lever a plugin has is an **undocumented side effect**: Hermes copies the message *list* given to
`pre_auxiliary_call`, not the message *dictionaries*, so editing them in place changes what the provider receives.
With `censor_auxiliary_calls: true` the plugin does exactly that (same filter and exclusions as for the main request),
then undoes its edit in `post_auxiliary_call` so that the caller's own message objects (possibly its history) are left
as they were. `censor_core/auxiliary.py` holds the logic.

What was measured (`bench/experiment_aux_hook.py`, real Hermes sources, isolated profile, fake provider, fictional
secrets): provider receives only the replacement for every auxiliary task tried, including retries, streaming and
asynchronous calls; 16 threads x 5 concurrent calls with no leak or cross-talk and every caller's objects restored;
unusual message shapes (content parts with an image, `tool_calls`, `None` or empty content) unchanged structurally;
a failing hook never breaks the call. Cost: about 9 ms per auxiliary call merely because Hermes builds the hook payload
and runs the hooks in a worker thread, plus the scan itself (12-60 ms for 50-200 KB, about 0.2-0.3 s for 1 MB on first
sight, then cached): negligible against an LLM call that takes seconds.

Limits, to be read before relying on it:

- **It can silently stop working.** It relies on a Hermes implementation detail. A Hermes release that copies the
  dictionaries would make the option useless, and the plugin cannot detect that at run time. The integration tests
  (`tests/integration/test_hermes_aux.py`) fail in that case: rerun them after each Hermes update (see
  `VALIDATION_REPORT.md`).
- **Only the messages are reachable** (chat `messages`, Responses `input`). A separate `system` / `instructions`
  string (Anthropic native, Responses), tool definitions and other request fields are not handed to the hook in an
  editable form and stay unfiltered. Non-dictionary message objects are skipped.
- **Hermes bounds these hooks** (`plugins.hook_callback_timeout`, 30 s by default). A hook that exceeds it is
  abandoned and the call goes out unfiltered, then the hook is suppressed for a while. Not a realistic case at the
  speeds above, but it is a fail-open path the plugin cannot report.
- **Registered at load time only**: the hooks cost something on every auxiliary call, so they exist only if the
  setting is on when Hermes loads the plugin. Turning it on later gives the DEGRADED state `AUX_RESTART_REQUIRED` until
  Hermes is restarted; turning it off takes effect immediately (the hooks then do nothing).
- **Undo is best effort.** If a post hook never fires, the caller's dictionaries stay filtered (the safe direction:
  the local history would then hold the replacement instead of the original text). At most 256 pending edits are kept;
  older ones are undone.
- **Same fail-open rule as everywhere**: an exception while filtering an auxiliary call lets it go out unfiltered and
  sets the state to ERROR (`AUX_REQUEST_FAILED_OPEN`) until the next auxiliary call goes through cleanly.
- Not exercised: the real `ContextCompressor` / title generator end to end, auxiliary calls through the native
  Anthropic or Responses clients, and gateway/cron surfaces. To check it on your own installation: turn the option on,
  restart Hermes, run `/compress` in a session that contains a rule word, then look at the summary stored in
  `state.db`: the rule word must appear as its replacement in the model-written part.

## 4. What may already have been displayed, logged or stored before censoring

`llm_request` only rewrites **the outgoing request**. The following stay in clear, locally: the message typed by the
user in the session history (Hermes' state database, `state.db` and its `-wal` file; also copied verbatim into the
"User Messages (verbatim)" section of a context-compaction summary message); the start of the message typed by the
user in `logs/agent.log` (`agent.turn_context: conversation turn: ... msg='...'`, truncated but written before the
middleware runs; observed with a fictional secret on Hermes v0.21.x); a tool's output **if** a surface displays it, and the
`post_tool_call` events, before `transform_tool_result` (which runs **after** those events and **before** the history)
applies - the exact moment of display depends on the surface and was **not** verified here; Hermes' logs (in debug
mode in particular); the `original_request` field (the **uncensored** request) that Hermes passes to **other
plugins'** `llm_execution` middlewares. `pre_api_request` observers, on the other hand, receive the already-censored
request.

## 5. Content deliberately left unmodified

- **Signed/encrypted blocks** (`signature`, `encrypted_content`, Anthropic `thinking` blocks, signed
  `reasoning_details`): rewriting them would invalidate the request. They may contain a secret; they are left intact
  (a "signed blocks skipped" counter is visible in `/censor status -v`).
- Tool names, property names and enums of tool schemas, identifiers, roles, headers and API keys: never rewritten
  (only tool *descriptions* are).
- Binary data (`data:` URLs, image base64).

## 6. Functional side effects

The model sees `[SECRET]` (or your token) in place of the value: if it must write that value to a file or a command,
it will write the token. A rule that deletes or replaces a word can break code or a command that contains it. False
positives are possible (substring search, short secrets).

## 7. Privileges and trust

A Hermes plugin runs **with the privileges of the Hermes process**. Before trusting it with access to your vault,
**review its code** (`censor_core/`, 3 intentional `subprocess` calls: the KeePassXC helper, `keepassxc-cli`, the
configured `askpass` command). Hermes' install scanner flags these calls (verdict *caution*) and the fictional
constants in the test files: this is expected. The settings file and the `askpass` command are trusted: anyone who can
modify them can hijack the plugin.

## 8. Memory

Python cannot wipe a string. The master password exists only in the helper (a few seconds, then the process ends).
Secret values stay in the Hermes process memory, in the index, for as long as the plugin protects them (`/censor
forget` drops the index; the memory is not overwritten).

## 9. KeePassXC

- **Tested: Windows 11, KeePassXC 2.7.12 only.** Linux (system `keepassxc-cli`, Flatpak, terminal prompt via
  `/dev/tty`) is **implemented but untested**.
- Terminal channel (`hermes censor test-unlock`, `tty`): implemented, **not automatically tested** (needs a console).
- Prompt window: Tk (bundled with Python on Windows; on Linux: the `python3-tk` package and a display). Without a
  display: "no local prompt channel" (see `KEEPASSXC.md`). A `/censor unlock` received from a remote surface
  (Telegram...) opens the window **on the machine that hosts Hermes**.
- The CLI does not share the GUI's unlocked state: the plugin never "sees" the database open in KeePassXC; it re-reads
  the file. YubiKey/challenge-response: **not supported**. Attachments: not read. A password containing a line break:
  refused.
- `unlock: keyfile-only` is the only mode without interaction and **lowers the vault's security** (the key file is on
  disk in clear).

## 10. Localization

Plugin messages are in **English only** (state codes are stable). A message catalog would be needed for other
languages.
