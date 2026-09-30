# Validation report (real results)

Date: 2026-09-30. Machine: Windows 11 Pro 10.0.26200. Hermes Agent v0.21.5 (git 5eb1381), managed Python 3.14.7,
KeePassXC / `keepassxc-cli` 2.7.12. **Fictional** databases and values only; no personal database was read.

## Contracts verified (official documentation + Hermes source)

- `register(ctx)`; `ctx.register_middleware("llm_request", fn)` -> `fn(**kw)` receives `request`/`original_request`
  and returns `{"request": ...}`; order: build kwargs -> `llm_request` -> `pre_api_request` -> execution.
- Fail-open: confirmed in `hermes_cli/middleware.py` and `agent/turn_api_request.py` (`except Exception` -> original
  request).
- `llm_request` is only called from `agent/turn_api_request.py` (per attempt); auxiliary calls only have observer
  hooks (`pre_auxiliary_call`, return ignored); `codex_app_server` delegates the turn before the loop.
- `transform_tool_result`: runs after `post_tool_call`, before the result is added to the conversation, first `str`
  return wins.
- User-facing surfaces usable without touching the model: slash command, CLI subcommand, logs, `hermes plugins list`.
  `ctx.inject_message` injects a **user** message (thus read by the model): rejected.
- Hermes' "Secret Source" API: unrelated (it supplies credentials to the process, it does not filter prompts).
- Catalog: submission by pull request, public repository with a release, pinned commit SHA, automatic validation.

## KeePassXC feasibility (real tests)

See `KEEPASSXC.md`: the CLI does not share the GUI's unlocked state; password via stdin (UTF-8 works); full XML
export with `ProtectInMemory`; key-file-only without interaction; localized messages. **No requirement turned out
to be impossible** without exposing the master password or modifying Hermes: unlocking happens in a local helper,
outside the chat. Chosen adaptation: no automatic unlock (except key-file-only, an explicit opt-in).

## Test results

| Suite | Interpreter | Result |
|---|---|---|
| unit + real KeePassXC (parser, engine, walker, connector, helper, runtime, vault e2e, Tk, examples) | Python 3.11.16 | **141 passed** |
| same | Python 3.14.7 | **140 passed, 1 skipped** (`yaml` missing outside a Hermes profile) |
| integration with the real Hermes + local fake provider (`tests/integration/`) | Python 3.14.7 managed by Hermes | **20 passed** |
| `hermes plugins doctor <path> --ci` | real Hermes | **OK** ("runtime discovery, manifest parsing, import, and registration passed", 1 hook) |
| real `hermes plugins install` cycle (local Git repository) -> `enable` -> `hermes censor status/check-rules/mask --secrets` -> `disable` -> `remove` | real Hermes, isolated test profile | **OK** |

Note: the project was translated to English and renamed (`censeur` -> `censor`, plugin `hermes-censor`) after the
first validation. The 141 unit tests, the 20 integration tests, `hermes plugins doctor --ci` and the
install -> enable -> `hermes censor status` -> disable -> remove cycle were all re-run successfully under the new names.

Platforms actually tested: **Windows 11 only.** Linux: the engine/parser were not run on Linux (the available WSL only
has Python 3.8, below the 3.11 minimum), the Linux `keepassxc-cli` and the `/dev/tty` terminal channel are
**untested**; Tk was driven on Windows only; the Windows terminal channel is not automatically tested.

Hermes install scanner: *caution* verdict - 3 intentional `subprocess` calls in `censor_core/`, plus findings in
`tests/` (fictional constants, `subprocess`). One HIGH finding (a U+FEFF literal in the parser) was fixed. Installing
a community source therefore needs a confirmation (`--force` outside an interactive terminal).

## Performance (Python 3.14.7, fictional data, `bench/bench_engine.py`)

| Secrets | Text | Build | Scan (fresh text) | Replay (cache) |
|---|---|---|---|---|
| 10 | 100 KB | 0.6 ms | 23 ms | 0.01 ms |
| 100 | 1 MB | 2.8 ms | 279 ms | 0.01 ms |
| 1000 | 1 MB | 72 ms | 331 ms | 0.01 ms |

A 60-message conversation (~210 KB), 200 secrets + 30 rules: **63 ms** on the first pass, **1.3 ms** on the next turn
(messages already seen are served by the per-string cache). Measured comparison: regex alternation = 2.7 s/MB for
1000 patterns (rejected); `str.find` per pattern = fast but linear in the number of patterns; native
`pyahocorasick` is ~6x faster at scanning. **No native dependency is imposed**: pure Python is enough with the
cache; Rust is not justified by these measurements.

## Testing pitfall to know about

Running the `hermes` CLI with a test `HERMES_HOME` triggers Hermes' "source update" bootstrap, which **rewrote two
launchers of the real installation** (`hermes-agent/.hermes/bin/hermes.cmd` and `hermes-acp.cmd`) to point to the
temporary test profile's Python. It was noticed by comparing timestamps and repaired. Avoid it: run integration tests
with the Python managed by the test profile and `HERMES_DISABLE_LAZY_INSTALLS=1`, and compare those launchers'
timestamps after any CLI run outside your real profile.

## What works / limits

**Works (demonstrated)**: filtering of rules and real KeePassXC secrets on paths 1-6 of `CALL_PATHS.md`;
`transform_tool_result`; states and diagnostics; local unlock (askpass and Tk tested); key-file-only; refresh and
staleness; detected fail-open; no secret in logs/statuses/diagnostics (dedicated tests).
**Does not work / not covered**: auxiliary calls (observed), `codex_app_server` (code), surfaces not exercised
(`CALL_PATHS.md`), signed blocks, reworded/encoded secrets, Linux untested.

## To add another connector

See `CONNECTORS.md`: a class honouring `SecretSource`, a settings block, a factory in `runtime`, tests with a
fictional vault. The engine and its tests do not need to change.

## To publish to the Hermes catalog

A public repository you own, a **tagged release**, a YAML entry submitted by pull request with the **exact commit
SHA**, automatic validation (schema, SHA, reachability), no self-update. To plan: choose a license, localize the
messages, test on Linux (CI), reduce the scanner findings in `tests/` (build fictional constants differently) and
document the 3 `subprocess` calls in the submission, run the catalog validator (`hermes plugins validate`), and have
the code reviewed by a third party.

## Re-running the integration tests

A test profile with a **short path** (native DLLs fail on long paths), provisioned once (the first CLI run on a blank
profile takes several minutes), then:

```bash
export HERMES_TEST_HOME='C:\path\to\short\hermes-test-home'              # NEVER your real profile
export HERMES_PYTHON=<HERMES_TEST_HOME>/tools/python-3.14.*/python.exe   # Python managed by that profile
export HERMES_SRC=<Hermes install>/hermes-agent                          # Hermes sources
export PYTEST_PKGS=<pytest dir>:<anthropic==0.87.0 dir>                  # pytest (pure Python), pinned Anthropic SDK
export HERMES_DISABLE_LAZY_INSTALLS=1
bash run_tests.sh tests/integration -q
```

Do **not** run the `hermes` CLI with a test `HERMES_HOME` without `HERMES_DISABLE_LAZY_INSTALLS=1` (see the pitfall).
