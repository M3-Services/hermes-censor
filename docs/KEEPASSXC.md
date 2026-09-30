# KeePassXC connector (v1)

## What was verified (KeePassXC 2.7.12, Windows)

| Finding | How |
|---|---|
| The CLI **does not share** the GUI's unlocked state: without credentials, `keepassxc-cli` refuses to open the database even if KeePassXC has it open. | tested (fictional database) |
| `keepassxc-cli` reads the password on **standard input** (never needed as an argument); a non-ASCII password works as **UTF-8**. | tested |
| `keepassxc-cli export -q -f xml` returns the full content on standard output, protected fields in clear (attribute `ProtectInMemory="True"`). | tested |
| CLI messages are **localized** (French on the test machine): the plugin never parses its text, only the exit code and the XML. | observed |
| A **key-file-only** database opens without interaction with `--no-password -k <key>`. | tested |
| The root group name is **localized** (e.g. "Mots de passe" with a French UI), so selection paths are relative to the root group. | observed |
| Tested on Windows only. **Linux: untested** (see `LIMITATIONS.md`). | - |

Other interfaces examined and **not retained** in v1: browser integration (`keepassxc-proxy`: encrypted protocol,
prior association, limited to URL lookups, no enumeration), the D-Bus Secret Service (Linux only, groups exposed by
hand, not testable here), the SSH agent. None provides "all the entries of a group" reliably and portably on both
Windows and Linux.

## Chosen method

A **short-lived helper** (`censor_core/kpx_helper.py`, standard library only) is launched by the plugin:

1. the master password is requested **locally** (Tk window, or the terminal for `hermes censor test-unlock`, or a
   configured `askpass` command) - never in the chat, never in the Hermes process;
2. it is passed to `keepassxc-cli export` through **standard input** only (never in arguments, files or logs);
3. the helper reads the export, **keeps only the configured selection**, returns the values to the plugin through a
   pipe and exits (the full XML and the password disappear with it);
4. the plugin builds an **in-memory index**; it keeps no plain list of values next to it.

> Honest limit: Python cannot wipe a string from memory. The master password exists only in the helper (for a few
> seconds); secret values stay in the Hermes process memory, as an index, for as long as the plugin protects them.

## What is loaded - explicit selection required

The plugin **never claims** to load "all the values of the vault": it reads exactly what `describe()` states, shown
by `/censor` (the "Exact scope read" line). Without a selection nothing is loaded (configuration error).

| Setting `keepassxc.*` | Role | Default |
|---|---|---|
| `groups` | groups to read, paths relative to the root group (`"Hermes"`, `"Servers/Prod"`, `"/"` = root) | - |
| `recursive` | include subgroups | `true` |
| `entries` | individual entries (`"Group/Title"`) | - |
| `exclude` | entries or groups to leave out (path prefix) | - |
| `select_all` | every entry (except the recycle bin) | `false` |
| `fields` | fields read: exact names and/or `@protected` (fields marked "protected" in KeePassXC) | `["Password", "@protected"]` |
| `include_history` | previous values of entries | `false` |
| `include_recycle_bin` | the recycle bin group | `false` |
| `min_secret_length` (top level) | minimum length | `6` |

**Attachments** are not read. Titles containing "/" cannot be targeted individually (select the group).

## Value policy

| Case | Handling | State |
|---|---|---|
| empty or blank value | ignored, counted | ACTIVE (nothing to protect) |
| value shorter than `min_secret_length` | **not loaded** (too many false positives), path listed | **DEGRADED** `SECRETS_INCOMPLETE_SHORT` |
| duplicate value | indexed once | ACTIVE |
| selected group/entry not found | listed | **DEGRADED** `SECRETS_INCOMPLETE_NOT_FOUND` |
| multi-line value (private key...) | the whole value, a CRLF variant, and each line of >= 16 characters except `-----` armor lines | ACTIVE |
| read failure (wrong password, missing database, timeout...) | previous list kept if there is one | **DEGRADED** `SECRETS_LOAD_FAILED` |

## Unlocking: three modes, none automatic by default

**1. `unlock: prompt` (default)** - the plugin never asks for anything by itself. The user types `/censor unlock`; a
local window asks for the password (never the chat). The command **does not block**: it returns immediately and the
window stays open until `prompt_timeout_seconds` (180 s). While nothing is loaded the state is DEGRADED ("secret
protection unavailable"); plain rules stay active.

**2. `keepassxc.askpass: [command, ...]`** - a local command that writes the password to its standard output, **in
UTF-8** (password manager, `pinentry`, system dialog...), modelled on `SSH_ASKPASS`. The plugin stores nothing. It runs
with the privileges of the Hermes process: only configure what you have reviewed.

**3. `unlock: keyfile-only`** - a database protected **only by a key file** (`--no-password -k`). This is the only
mode without interaction (non-interactive sessions: gateway, cron, service). **Explicit trade-off**: anyone who can
read the key file **and** the `.kdbx` file gets the vault; protect the key with filesystem permissions. The plugin
shows this as information (`KEYFILE_ONLY_NOTICE`). Loading is lazy (first request) and refreshed automatically when
the database changes; a failure is only retried after 60 s.

The master password is **never** read from a configuration file, an environment variable or `.env`.

### Non-interactive sessions

In `prompt` mode, a session without a desktop or a terminal cannot unlock: `no_prompt_channel` ("no local prompt
channel available") with the repair action. Plain rules stay active. The plugin never fakes an unlock.

## Refresh and staleness

A loaded list is marked **stale** (DEGRADED state, never ACTIVE) when: the database file changed (`mtime`/size,
checked at most every 5 s), disappeared, or is older than `max_age_minutes`. It **keeps filtering** (an old list is
better than none) but is never presented as up to date. `/censor refresh` reloads (same local prompt).
`/censor forget` forgets the secrets. The plugin **cannot know** whether KeePassXC has re-locked: it reads the file,
not the running instance; loaded values stay in memory until `forget` or the end of the process.

## Test databases

All tests use **fictional** databases built by `tests/kpx_fixtures.py` (made-up XML imported with
`keepassxc-cli import`). No test reads or modifies a personal database.
