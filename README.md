# Hermes Censor - a Hermes Agent plugin

Before every call to an LLM provider, **Hermes Censor** replaces or removes, in the content sent to the model:

1. the strings **you** define in a local rules file (`<pattern>:<replacement>`);
2. the **secret values of a local KeePassXC database** (a selection of groups/entries that you choose).

Everything happens inside the plugin: no proxy, no additional service, no network telemetry, nothing to install
(pure Python, home-made Aho-Corasick). Tested only on Hermes v0.21.5 (documented plugin API: `register(ctx)`,
`llm_request` middleware, `transform_tool_result` hook).

> **Practical protection, not a guarantee.** Hermes runs middlewares *fail-open*: a failure lets the request go out
> uncensored; search is literal; some calls (compression, titles...) do not go through the middleware at all (an
> opt-in, experimental setting, `censor_auxiliary_calls`, can cover them: see the limitations). Read
> [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) and [`docs/CALL_PATHS.md`](docs/CALL_PATHS.md) before relying on it.
> The plugin runs with Hermes' privileges: **review the code** before trusting it with access to your vault.

## What it does

- `llm_request` middleware: rewrites the provider request (system/developer/user/assistant messages, tool results
  and arguments, Responses API input, textual parts of multimodal content, tool descriptions) while
  **preserving the structure**, roles, identifiers, headers and API keys.
- `transform_tool_result` hook: censors a tool result **before** it is added to the conversation.
- Optional (`censor_auxiliary_calls: true`, off by default, restart Hermes): also filters the messages of **auxiliary
  calls** (context compression, session title, vision, approval...), which bypass the middleware. It relies on an
  **undocumented** Hermes behaviour that a Hermes update could silently remove: read
  [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) section 3 first.
- **ACTIVE / DEGRADED / INACTIVE / ERROR** state with a reason and a repair action: `/censor`, `hermes censor status`.
- A secret that is not loaded (locked vault, value too short, selection not found, database changed...) yields a
  **DEGRADED** state, never a false sense of full protection.
- The master password is typed **outside the chat** (local window or `askpass` command) and never enters the Hermes
  process, a command line or a file.

## Installation

Requirements: Hermes Agent and, for secrets, **KeePassXC 2.7+** (`keepassxc-cli`). Tested on Windows; Linux is untested.

**From Git** (Hermes' install scanner flags the plugin's three `subprocess` calls: this is expected, see
`docs/LIMITATIONS.md` section 7):

```bash
hermes plugins install M3-Services/hermes-censor --no-enable
hermes plugins enable hermes-censor
```

**Manually**: copy this folder to `<HERMES_HOME>/plugins/hermes-censor/`, then `hermes plugins enable hermes-censor`
(plugins are opt-in; activation takes effect on the next session, restart the gateway if you run one).

## Minimal configuration

1. Rules: create `<HERMES_HOME>/plugin-data/hermes-censor/rules.txt` (template: [`examples/rules.txt`](examples/rules.txt),
   syntax: [`docs/RULES.md`](docs/RULES.md)).
2. Vault (optional): add the block from [`examples/config.yaml`](examples/config.yaml) to `config.yaml` - the database
   path **and an explicit selection** of groups; no secret value belongs in that file.
3. In Hermes: `/censor unlock` -> a local window asks for the master password -> `/censor` should show **ACTIVE**.

Full reference: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md); KeePassXC details, unlock modes, non-interactive
sessions and refresh: [`docs/KEEPASSXC.md`](docs/KEEPASSXC.md).

## Diagnostics

| | |
|---|---|
| `/censor` | state, **exact scope read** from the vault, warnings, activity (never a value) |
| `/censor unlock` / `refresh` / `forget` / `rules` | (re)load / forget the secrets, reload the rules |
| `hermes censor status` / `check-rules` / `test-unlock` / `mask` | the same checks from a terminal |
| `hermes censor selftest` | does `censor_auxiliary_calls` still work on this Hermes? (run it after a Hermes update) |
| `hermes plugins list` | is the plugin enabled? (a disabled plugin cannot warn you) |

## Uninstall

```bash
hermes plugins disable hermes-censor
hermes plugins remove hermes-censor
```

Then remove the `plugins.entries.hermes-censor` block from `config.yaml` and, if you wish,
`<HERMES_HOME>/plugin-data/hermes-censor/`. The plugin leaves no secret value on disk.

## Development and tests

```bash
bash run_tests.sh tests --ignore=tests/integration        # unit + real KeePassXC (fictional databases): Python >= 3.11
python bench/bench_engine.py                              # engine benchmark (fictional data)
```

If you use `censor_auxiliary_calls`, run **`hermes censor selftest`** after every Hermes update: the option relies on an
undocumented Hermes behaviour and nothing else notices when it disappears (`tests/integration/test_hermes_aux.py` checks
the same thing for developers). `bench/experiment_aux_hook.py` is the safety/performance experiment behind the option.

`tests/test_install_scan.py` runs Hermes' own install scanner on the tree (needs the Hermes sources, `HERMES_SRC`):
a critical finding would make `hermes plugins install/update` refuse the plugin. GitHub Actions
(`.github/workflows/tests.yml`) runs the unit tests on Linux and Windows.

Integration tests with the **real Hermes** and a local fake provider need a Hermes **test** profile
(`HERMES_TEST_HOME`, short path, never your real profile), the Python managed by that profile, the Hermes sources
(`HERMES_SRC`) and `HERMES_DISABLE_LAZY_INSTALLS=1`. See [`docs/VALIDATION_REPORT.md`](docs/VALIDATION_REPORT.md)
for the real results and how to rerun them. All tests use only **fictional** databases and values.

Architecture: `censor_core/engine.py` (index + resolution), `walker.py` (request walking), `rules.py`,
`vault.py` (connector interface), `keepassxc.py` + `kpx_helper.py` (v1 connector), `runtime.py` (state),
`__init__.py` (Hermes glue). Adding another vault: [`docs/CONNECTORS.md`](docs/CONNECTORS.md).

## License

[MIT](LICENSE), the same license as Hermes Agent.
