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
through** `llm_request` (observed); `codex_app_server` bypasses it according to the code; several surfaces (Bedrock,
native Gemini, gateways, cron, non-streaming, MoA) were **not** exercised. The `transform_tool_result` layer reduces
the exposure of values coming from tools (the history no longer contains them, hence neither does compression), but
not that of a secret typed by the user.

**Smallest honest change in Hermes** (outside the plugin): call `apply_llm_request_middleware` from the auxiliary
call funnel (`agent/auxiliary_client.py`, where `pre_auxiliary_call` is emitted).

## 4. What may already have been displayed, logged or stored before censoring

`llm_request` only rewrites **the outgoing request**. The following stay in clear, locally: the message typed by the
user in the session history (Hermes' state database); a tool's output **if** a surface displays it, and the
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
