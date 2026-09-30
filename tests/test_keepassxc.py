"""KeePassXC connector against the REAL keepassxc-cli, with fictional databases only."""
import json
import logging
import sys
import textwrap
import time

import pytest

from censor_core.keepassxc import KeePassXCConfig, KeePassXCSource
from censor_core.vault import SecretSourceError
from kpx_fixtures import FICTIVE_TREE, E, G, build_kdbx, find_cli

CLI = find_cli()
pytestmark = pytest.mark.skipif(CLI is None, reason="keepassxc-cli introuvable")

MASTER = "Fictional-Master-1"
EXPECTED_HERMES = {"FictionalPassword-AAA", "fictional-api-key-BBB-2222", "CurrentValue-777", "DeepSecret-999"}


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


@pytest.fixture
def db(tmp_path):
    return build_kdbx(CLI, str(tmp_path / "fictional.kdbx"), FICTIVE_TREE, password=MASTER)


def cfg(db, tmp_path, password=MASTER, **kw):
    askpass = (script(tmp_path, "askpass.py",
                     f"import sys; sys.stdout.buffer.write({password!r}.encode('utf-8'))")  # contrat : UTF-8
               if password is not None else None)
    base = dict(database=db, cli=[CLI], groups=["Hermes"], askpass=askpass, timeout_seconds=60)
    base.update(kw)
    return KeePassXCConfig(**base)


def test_load_with_local_askpass_returns_exact_selection(db, tmp_path):
    load = KeePassXCSource(cfg(db, tmp_path)).load(interactive=True)
    assert set(load.values) == EXPECTED_HERMES
    rep = load.report
    assert rep.entries_selected >= 5 and rep.values_loaded == 4
    assert rep.skipped_short == 1 and rep.skipped_empty == 1 and rep.duplicates == 1
    assert rep.complete is False  # a value that is too short is not protected
    assert rep.fingerprint


def test_scope_text_is_exact_and_never_claims_the_whole_vault(db, tmp_path):
    scope = KeePassXCSource(cfg(db, tmp_path)).load(interactive=True).report.scope
    assert "Hermes" in scope and "Password" in scope and "recycle bin" in scope
    assert "all the values" not in scope.lower()


def test_wrong_password_is_unlock_failed_without_leaking_it(db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    src = KeePassXCSource(cfg(db, tmp_path, password="wrong-password-9"))
    with pytest.raises(SecretSourceError) as exc:
        src.load(interactive=True)
    assert exc.value.code == "unlock_failed"
    blob = str(exc.value) + exc.value.message + exc.value.repair + caplog.text
    assert "wrong-password-9" not in blob and MASTER not in blob


def test_cancelled_prompt(db, tmp_path):
    c = cfg(db, tmp_path)
    c.askpass = script(tmp_path, "cancel.py", "import sys; sys.exit(1)")
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(c).load(interactive=True)
    assert exc.value.code == "unlock_cancelled"


def test_non_interactive_prompt_mode_reports_no_channel_without_spawning(db, tmp_path):
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(cfg(db, tmp_path)).load(interactive=False)
    assert exc.value.code == "no_prompt_channel"
    assert "keyfile-only" in exc.value.repair or "askpass" in exc.value.repair


def test_no_channel_available_is_reported(db, tmp_path):
    c = cfg(db, tmp_path)
    c.askpass = None
    c.channels = ("askpass",)  # aucun canal utilisable
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(c).load(interactive=True)
    assert exc.value.code == "no_prompt_channel"


def test_non_ascii_master_password(tmp_path):
    db = build_kdbx(CLI, str(tmp_path / "u.kdbx"), G("R", entries=[E("t", {"Password": "SecretUnicode-ééé"})]),
                    password="Maître-é123-€")
    load = KeePassXCSource(cfg(db, tmp_path, password="Maître-é123-€", groups=["/"])).load(interactive=True)
    assert load.values == ["SecretUnicode-ééé"]


def test_keyfile_only_database_loads_without_any_prompt(tmp_path):
    key = str(tmp_path / "key.keyx")
    db = build_kdbx(CLI, str(tmp_path / "k.kdbx"), FICTIVE_TREE, keyfile=key)
    c = KeePassXCConfig(database=db, cli=[CLI], keyfile=key, unlock="keyfile-only", groups=["Hermes"])
    load = KeePassXCSource(c).load(interactive=False)  # even in a non-interactive session
    assert set(load.values) == EXPECTED_HERMES


def test_password_plus_keyfile(tmp_path):
    key = str(tmp_path / "key2.keyx")
    db = build_kdbx(CLI, str(tmp_path / "pk.kdbx"), FICTIVE_TREE, password=MASTER, keyfile=key)
    load = KeePassXCSource(cfg(db, tmp_path, keyfile=key)).load(interactive=True)
    assert set(load.values) == EXPECTED_HERMES
    with pytest.raises(SecretSourceError):  # without the key file: refused
        KeePassXCSource(cfg(db, tmp_path)).load(interactive=True)


def test_missing_database_and_missing_cli(db, tmp_path):
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(cfg(str(tmp_path / "absent.kdbx"), tmp_path)).load(interactive=True)
    assert exc.value.code == "db_not_found"
    c = cfg(db, tmp_path)
    c.cli = [str(tmp_path / "not-a-cli.exe")]
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(c).load(interactive=True)
    assert exc.value.code == "cli_not_found"


def test_fingerprint_changes_when_database_is_rewritten(db, tmp_path):
    src = KeePassXCSource(cfg(db, tmp_path))
    before = src.fingerprint()
    time.sleep(0.05)
    build_kdbx(CLI, db, G("R", entries=[E("t", {"Password": "AutreValeur-123"})]), password=MASTER)
    assert src.fingerprint() != before
    assert KeePassXCSource(cfg(str(tmp_path / "nope.kdbx"), tmp_path)).fingerprint() is None


def test_password_goes_through_stdin_only_never_argv(db, tmp_path):
    log = tmp_path / "cli_calls.json"
    real = CLI.replace("\\", "\\\\")
    fake = script(tmp_path, "fake_cli.py", f"""
        import json, subprocess, sys
        data = sys.stdin.buffer.read()
        json.dump({{"argv": sys.argv[1:], "stdin": data.decode("utf-8")}}, open({str(log)!r}, "w"))
        proc = subprocess.run([{real!r}] + sys.argv[1:], input=data, capture_output=True)
        sys.stdout.buffer.write(proc.stdout)
        sys.exit(proc.returncode)
    """)
    c = cfg(db, tmp_path, cli=fake)
    load = KeePassXCSource(c).load(interactive=True)
    assert set(load.values) == EXPECTED_HERMES
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert MASTER not in " ".join(seen["argv"])
    assert seen["stdin"] == MASTER + "\n"
    assert "export" in seen["argv"] and "-q" in seen["argv"]


def test_cli_timeout(db, tmp_path):
    slow = script(tmp_path, "slow_cli.py", "import time; time.sleep(30)")
    c = cfg(db, tmp_path, cli=slow, timeout_seconds=1)
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(c).load(interactive=True)
    assert exc.value.code == "cli_timeout"


def test_empty_selection_is_rejected(db, tmp_path):
    with pytest.raises(SecretSourceError) as exc:
        KeePassXCSource(cfg(db, tmp_path, groups=[])).load(interactive=True)
    assert exc.value.code == "empty_selection"


def test_missing_group_is_reported_as_not_found_and_incomplete(db, tmp_path):
    rep = KeePassXCSource(cfg(db, tmp_path, groups=["Hermes", "Ghost"])).load(interactive=True).report
    assert rep.not_found == ["Ghost"] and rep.complete is False


def test_no_value_in_logs_or_reports(db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    load = KeePassXCSource(cfg(db, tmp_path)).load(interactive=True)
    blob = caplog.text + json.dumps(load.report.__dict__, default=str)
    for v in load.values:
        assert v not in blob
    assert MASTER not in blob


def test_source_implements_the_documented_interface(db, tmp_path):
    src = KeePassXCSource(cfg(db, tmp_path))
    assert src.name.startswith("keepassxc")
    assert isinstance(src.describe(), str) and MASTER not in src.describe()
