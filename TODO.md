# TODO - Hermes Censor (Hermes Agent plugin)

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
- [x] Opt-in `censor_auxiliary_calls` (in-place edit in `pre_auxiliary_call`, undone in `post_auxiliary_call`): code,
      unit + integration tests, docs; safety/perf experiment kept in `bench/experiment_aux_hook.py`

## To do (non-blocking)
- [ ] Test on Linux (system/Flatpak `keepassxc-cli`, terminal channel, missing Tk) - in CI
- [ ] Verify the "not verified" paths of the matrix (Bedrock, native Gemini, gateway/cron, non-streaming, MoA, multimodal)
- [ ] Ask Hermes upstream to apply `llm_request` middleware to auxiliary calls (compression, title, vision): confirmed leak on
      a real session; the opt-in `censor_auxiliary_calls` setting is only a workaround on an undocumented behaviour
- [ ] Validate `censor_auxiliary_calls` on a real session (enable, restart Hermes, `/compress` with a rule word, check the
      summary in `state.db`); rerun `tests/integration/test_hermes_aux.py` after every Hermes update
- [ ] `censor_auxiliary_calls`: not exercised yet with the native Anthropic / Responses clients, the real `ContextCompressor`
      and gateway/cron; a separate `system`/`instructions` string is not reachable from the hook
- [ ] Filter local logs/state? (`agent.log` `turn_context` line and `state.db` keep the typed secret in clear)
- [ ] Choose a license; message catalogs; reduce scanner findings in `tests/`
- [ ] Publish to the Hermes catalog (public repo, tagged release, pinned SHA)
- [ ] Possible second connector (see docs/CONNECTORS.md)
