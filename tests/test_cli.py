import getpass
import warnings

import pytest

from junkyard import cli
from junkyard.config import load_settings
from junkyard.database import Database
from junkyard.security import PASSWORD_HASHER, PasswordError
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


def prompts(monkeypatch, answers):
    answers = iter(answers)
    monkeypatch.setattr("junkyard.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("junkyard.cli.getpass.getpass", lambda prompt: next(answers))


def test_init_and_reset_use_distinct_confirmed_passwords(config_file, monkeypatch, capsys):
    prompts(monkeypatch, [CLI_PASSWORD, CLI_PASSWORD, BROWSER_PASSWORD, BROWSER_PASSWORD])
    assert cli.main(["--config", str(config_file), "init"]) == 0
    database = Database(load_settings(config_file).data_dir)
    browser_hash = database.password_hash("browser")
    new_password = "replacement CLI passphrase"
    prompts(monkeypatch, [new_password, new_password])
    assert cli.main(["--config", str(config_file), "passwd", "cli"]) == 0
    assert PASSWORD_HASHER.verify(database.password_hash("cli"), new_password)
    assert database.password_hash("browser") == browser_hash
    output = capsys.readouterr()
    for secret in (CLI_PASSWORD, BROWSER_PASSWORD, new_password, browser_hash):
        assert secret not in output.out + output.err


@pytest.mark.parametrize("answers", [
    [CLI_PASSWORD, "different confirmation"],
    ["short", "short"],
    [CLI_PASSWORD, CLI_PASSWORD, CLI_PASSWORD, CLI_PASSWORD],
])
def test_invalid_prompts_do_not_initialize(config_file, monkeypatch, answers):
    prompts(monkeypatch, answers)
    assert cli.main(["--config", str(config_file), "init"]) == 1
    assert not load_settings(config_file).data_dir.exists()


def test_noninteractive_password_input_is_refused(config_file, monkeypatch, capsys):
    monkeypatch.setattr("junkyard.cli.sys.stdin.isatty", lambda: False)
    assert cli.main(["--config", str(config_file), "init"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_echoing_fallback_is_refused(monkeypatch):
    monkeypatch.setattr("junkyard.cli.sys.stdin.isatty", lambda: True)

    def unsafe_prompt(prompt):
        warnings.warn("echo unavailable", getpass.GetPassWarning)
        pytest.fail("must not reach echoing fallback")

    monkeypatch.setattr("junkyard.cli.getpass.getpass", unsafe_prompt)
    with pytest.raises(PasswordError, match="echo"):
        cli.prompt_password("CLI")


def test_password_arguments_are_rejected_without_echoing(capsys):
    secret = "do-not-echo-this-password"
    with pytest.raises(SystemExit) as error:
        cli.main(["init", "--password", secret])
    assert error.value.code == 2
    assert secret not in capsys.readouterr().err


def test_browser_password_cannot_be_reused_for_cli(database, config_file, monkeypatch):
    old_hash = database.password_hash("cli")
    prompts(monkeypatch, [BROWSER_PASSWORD, BROWSER_PASSWORD])
    assert cli.main(["--config", str(config_file), "passwd", "cli"]) == 1
    assert database.password_hash("cli") == old_hash


def test_serve_refuses_missing_credentials(config_file, monkeypatch):
    monkeypatch.setattr("junkyard.cli.uvicorn.run", lambda *args, **kwargs: pytest.fail("server must not start"))
    assert cli.main(["--config", str(config_file), "serve"]) == 1


def test_serve_uses_private_single_worker_defaults(database, config_file, monkeypatch):
    calls = []
    monkeypatch.setattr("junkyard.cli.uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    assert cli.main(["--config", str(config_file), "serve"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8000, "workers": 1,
                      "proxy_headers": False, "access_log": False, "server_header": False}]


def test_migrate_command_upgrades_without_password_prompts(database, config_file, monkeypatch, capsys):
    original_hash = database.password_hash("cli")
    with database.connect() as connection, connection:
        connection.execute("DROP TABLE browser_sessions")
        connection.execute("DROP TABLE files")
        connection.execute("PRAGMA user_version = 1")
    monkeypatch.setattr("junkyard.cli.getpass.getpass", lambda prompt: pytest.fail("migration must not prompt for credentials"))
    assert cli.main(["--config", str(config_file), "serve"]) == 1
    assert "migrate" in capsys.readouterr().err
    assert cli.main(["--config", str(config_file), "migrate"]) == 0
    database.require_file_schema()
    assert database.password_hash("cli") == original_hash

