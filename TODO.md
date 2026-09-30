# TODO - Hermes Censor (Hermes Agent plugin)

## Done (v0.2.0)
- [x] Opt-in `censor_auxiliary_calls` (in-place edit in `pre_auxiliary_call`, undone in `post_auxiliary_call`): code,
      unit + integration tests, docs; safety/perf experiment kept in `bench/experiment_aux_hook.py`; validated on a real
      `/compress` (2026-09-30)
- [x] `hermes censor selftest`: checks on demand that the auxiliary mechanism still works on the installed Hermes
- [x] `tests/test_install_scan.py`: runs Hermes' own install scanner on the tree (critical/high findings fail the test)
- [x] GitHub Actions: unit tests on Linux and Windows, Python 3.11 and 3.13 (`.github/workflows/tests.yml`)
- [x] Version 0.2.0

## Done (v0.1.0)
- [x] Verified the Hermes contracts (docs + v0.21.5 source) and KeePassXC CLI 2.7.12
- [x] Rules parser, pure-Python Aho-Corasick engine (secrets take priority, case, Unicode, JSON forms), cache
- [x] Request walker (chat, Responses, Anthropic, Converse, Gemini, unknown structures)
- [x] `SecretSource` interface + KeePassXC connector (isolated helper, askpass, Tk, key-file-only)
- [x] ACTIVE / DEGRADED / INACTIVE / ERROR states, `/censor`, `hermes censor ...`
- [x] `llm_request` middleware + `transform_tool_result` hook
- [x] Tests: unit + real KeePassXC + integration with the real Hermes; `doctor --ci` OK; install/enable/remove cycle OK
- [x] Docs: README, RULES, KEEPASSXC, CONFIGURATION, CONNECTORS, LIMITATIONS, CALL_PATHS, VALIDATION_REPORT
- [x] Whole project translated to English (package `censor_core`, plugin `hermes-censor`)

## To do (non-blocking)
- [ ] Linux: the CI is green there (157 unit tests incl. the real-KeePassXC ones with Ubuntu's `keepassxc-cli`; Tk tests
      skipped, no display). Still to test by hand: the Flatpak `keepassxc-cli`, the terminal channel (`/dev/tty`), the Tk
      prompt, and the Hermes integration on Linux
- [ ] Choose a license (MIT suggested: same as Hermes) and add `LICENSE`; needed before the Hermes catalog
- [ ] Publish to the Hermes catalog (public repo, tagged release, pinned SHA)
- [ ] Verify the "not verified" paths of the matrix (Bedrock, native Gemini, gateway/cron, non-streaming, MoA, multimodal)
- [ ] Ask Hermes upstream to apply `llm_request` middleware to auxiliary calls (compression, title, vision): confirmed leak on
      a real session; the opt-in `censor_auxiliary_calls` setting is only a workaround on an undocumented behaviour
- [ ] `censor_auxiliary_calls`: not exercised yet with the native Anthropic / Responses clients, the real `ContextCompressor`
      and gateway/cron; a separate `system`/`instructions` string is not reachable from the hook
- [ ] Ask Hermes upstream for a plugin API that redacts EXACT values in logs (see "Local logs" in docs/LIMITATIONS.md: the
      existing `ctx.register_redaction_patterns` was tried and is not suitable); until then the typed secret stays in
      `agent.log` (start of the message) and `state.db`
- [ ] Message catalogs; reduce scanner findings in `tests/` (11 medium today: the `subprocess` calls are intentional)
- [ ] Possible second connector (see docs/CONNECTORS.md)
