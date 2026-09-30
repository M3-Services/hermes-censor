# Vault connector interface (and what adding one takes)

Version 1 ships **a single connector: KeePassXC** (`censor_core/keepassxc.py`). There is no "pass", GPG, Bitwarden or
1Password connector. The filtering engine (`engine.py`, `walker.py`) knows nothing about KeePassXC: it only receives
lists of `str`. The filtering tests (`test_engine.py`, `test_walker.py`) use no connector, and `test_runtime.py`
uses a fake source that implements only the interface.

## Contract (`censor_core/vault.py`)

```python
class SecretSource(Protocol):
    name: str
    def load(self, *, interactive: bool) -> SecretLoad: ...   # values + report WITHOUT values, or SecretSourceError
    def fingerprint(self) -> Optional[str]: ...               # cheap fingerprint of the vault state (no secret)
    def describe(self) -> str: ...                            # EXACT scope of what is read (for diagnostics)
```

- `load` returns `SecretLoad(values, report)`. `report` (`LoadReport`) holds counters, the exact scope, the paths not
  found, the fields that were too short and the fingerprint - never a value.
- `load` raises `SecretSourceError(code, message, repair)` on failure: `message` and `repair` never contain a value,
  and `repair` tells the user what to do.
- `interactive=False`: a session with nobody at the screen; a connector that needs a prompt must fail cleanly
  (`no_prompt_channel`) instead of waiting.
- `fingerprint()` is used to detect that a loaded list is **stale**; if it returns `None` or a value different from
  `report.fingerprint`, the state becomes DEGRADED (`SECRETS_STALE_*`) and the list is no longer presented as up to date.

## What remains to plug in another connector

1. Write a class that honours the contract above (a new module, no change to `engine.py`/`walker.py`).
2. Add a settings block in `config.py` (`parse_settings`) and instantiate it in `runtime._refresh_settings` (today:
   `source_factory(KeePassXCConfig)`; the factory will have to be chosen according to the block that is present).
3. Decide the vault-specific policy (unlock, selection, empty/short values), with the same requirements: no secret
   in clear on disk, no password in a process argument or in the chat.
4. Add tests with a **fictional** vault, the locked/modified/unavailable cases, and "no value in logs/diagnostics"
   (see `test_keepassxc.py`).

The `pass` connector is **not** shipped: no automatic protection is therefore provided for a secret known only to
`pass`. Its output is only filtered if it matches a plain rule or a loaded KeePassXC value.
