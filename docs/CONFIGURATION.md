# Configuration reference

Settings live in `<HERMES_HOME>/config.yaml`, under `plugins.entries.hermes-censor.settings` (example:
`examples/config.yaml`). **No secret value belongs there.** Any unknown or invalid key is reported
(`CONFIG_INVALID`, DEGRADED state) so that a typo cannot silently remove a protection. Settings are re-read at most
every 2 s.

| Key | Type | Default | Role |
|---|---|---|---|
| `enabled` | bool | `true` | `false` -> INACTIVE, requests untouched |
| `rules_file` | path | `<HERMES_HOME>/plugin-data/hermes-censor/rules.txt` | plain rules file (`RULES.md`) |
| `ignore_case` | bool | `false` | plain rules ignore case (never the secrets) |
| `secret_replacement` | str | `[SECRET]` | replaces a secret value (changing it requires unlocking again) |
| `censor_tool_results` | bool | `true` | also filter through `transform_tool_result` |
| `min_secret_length` | int >= 1 | `6` | shorter = not loaded, DEGRADED state |
| `max_age_minutes` | number >= 0 | `0` | secrets stale after N minutes (0 = never) |
| `keepassxc` | block | - | connector (below); absent = no vault |

`keepassxc` block (details and policy: `KEEPASSXC.md`):

| Key | Default | Role |
|---|---|---|
| `enabled` | `true` | `false` ignores the block |
| `database` | - (required) | `.kdbx` file |
| `cli` | search PATH, then `C:\Program Files\KeePassXC\keepassxc-cli.exe` | path or argument list (e.g. Flatpak) |
| `keyfile` | - | key file (in addition to the password, or alone with `unlock: keyfile-only`) |
| `unlock` | `prompt` | `prompt` or `keyfile-only` |
| `askpass` | - | local command that prints the password as UTF-8 on stdout |
| `groups` / `entries` / `exclude` / `select_all` | - | **selection required** (at least one of the first three, or `select_all`) |
| `recursive` | `true` | subgroups included |
| `fields` | `["Password", "@protected"]` | fields read |
| `include_history` / `include_recycle_bin` | `false` / `false` | previous values / recycle bin |
| `timeout_seconds` | `90` | `keepassxc-cli` timeout |
| `prompt_timeout_seconds` | `180` | local prompt timeout |

## Commands

| Command | Effect |
|---|---|
| `/censor` or `/censor status [-v]` | state, exact scope, warnings, activity (never a value) |
| `/censor unlock` / `refresh` | load/reload the secrets; returns immediately, local prompt outside the chat |
| `/censor forget` | forget the loaded secrets |
| `/censor rules` | reload the rules file |
| `hermes censor status [-v]` | state as seen by a separate CLI process (does not see a running session) |
| `hermes censor check-rules [file]` | validate a rules file (lines and codes, never the content) |
| `hermes censor test-unlock` | try the unlock (window or terminal), prints counters only |
| `hermes censor mask [--secrets]` | filter the text read on stdin |

## States

| State | Meaning |
|---|---|
| **ACTIVE** | rules loaded without issue; if a vault is configured: expected secrets loaded, complete selection, up-to-date list |
| **DEGRADED** | partial or stale filtering: vault not unlocked, values too short / selection not found, database changed/missing/too old, failed refresh, invalid rules or missing file, invalid setting - each finding carries a code and a **repair action** |
| **INACTIVE** | disabled, or nothing configured (nothing is filtered) |
| **ERROR** | the last request went out **uncensored** (plugin exception) |

Every change to a non-ACTIVE state is also logged once (logger `hermes-censor`, WARNING level, no value).
