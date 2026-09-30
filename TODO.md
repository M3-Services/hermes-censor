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

## To do (non-blocking)
- [ ] Test on Linux (system/Flatpak `keepassxc-cli`, terminal channel, missing Tk) - in CI
- [ ] Verify the "not verified" paths of the matrix (Bedrock, native Gemini, gateway/cron, non-streaming, MoA, multimodal)
- [ ] Ask/verify on the Hermes side: `llm_request` for auxiliary calls (compression, title, vision)
- [ ] Choose a license; message catalogs; reduce scanner findings in `tests/`
- [ ] Publish to the Hermes catalog (public repo, tagged release, pinned SHA)
- [ ] Possible second connector (see docs/CONNECTORS.md)
