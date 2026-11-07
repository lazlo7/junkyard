import os
import sqlite3
import stat

import pytest

from junkyard.database import Database, StoreError
from junkyard.security import PASSWORD_HASHER
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


def test_initialization_stores_only_salted_hashes(database, hashes):
    database.check_initialized()
    assert stat.S_IMODE(database.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.path.stat().st_mode) == 0o600
    for kind, password in (("cli", CLI_PASSWORD), ("browser", BROWSER_PASSWORD)):
        stored = database.password_hash(kind)
        assert stored.startswith("$argon2id$")
        assert PASSWORD_HASHER.verify(stored, password)
    assert database.password_hash("cli") != database.password_hash("browser")
    for file in database.directory.iterdir():
        contents = file.read_bytes()
        assert CLI_PASSWORD.encode() not in contents
        assert BROWSER_PASSWORD.encode() not in contents
    with pytest.raises(StoreError, match="already exists"):
        database.initialize(*reversed(hashes))
    assert database.password_hash("cli") == hashes[0]


def test_missing_database_does_not_get_created(settings):
    database = Database(settings.data_dir)
    with pytest.raises(StoreError, match="missing"):
        database.check_initialized()
    assert not settings.data_dir.exists()


def test_password_reset_is_persistent_and_scoped(database, hashes):
    original_browser_hash = database.password_hash("browser")
    database.replace_password("cli", hashes[1])
    reopened = Database(database.directory)
    assert reopened.password_hash("cli") == hashes[1]
    assert reopened.password_hash("browser") == original_browser_hash
    with reopened.connect() as connection:
        assert dict(connection.execute("SELECT kind, version FROM credentials").fetchall()) == {"cli": 2, "browser": 1}


def test_schema_version_and_missing_credentials_fail_closed(database):
    with database.connect() as connection, connection:
        connection.execute("DELETE FROM credentials WHERE kind = 'browser'")
    with pytest.raises(StoreError, match="both CLI and browser"):
        database.check_initialized()
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(StoreError, match="schema version"):
        database.check_initialized()


@pytest.mark.parametrize("target", ["directory", "file"])
def test_unsafe_permissions_are_rejected(database, target):
    path = database.directory if target == "directory" else database.path
    path.chmod(0o755 if target == "directory" else 0o644)
    with pytest.raises(StoreError, match="permissions"):
        database.check_initialized()


def test_symlink_directory_is_rejected(tmp_path, hashes):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(StoreError, match="symlinks"):
        Database(link).initialize(*hashes)
    assert not list(target.iterdir())


def test_symlink_database_is_rejected(database, tmp_path):
    directory = tmp_path / "other"
    directory.mkdir(mode=0o700)
    (directory / "metadata.sqlite3").symlink_to(database.path)
    with pytest.raises(StoreError, match="private regular file"):
        Database(directory).check_initialized()


def test_symlink_parent_is_rejected_before_creating_data(tmp_path, hashes):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(StoreError, match="symlinks"):
        Database(link / "data").initialize(*hashes)
    assert not list(target.iterdir())


def test_hardlinked_database_is_rejected(database):
    os.link(database.path, database.directory / "alias")
    with pytest.raises(StoreError, match="no hard links"):
        database.check_initialized()


def test_failed_initialization_does_not_publish_partial_credentials(settings, hashes, monkeypatch):
    def fail_publish(*args):
        raise OSError("injected publication failure")

    monkeypatch.setattr("junkyard.database.os.link", fail_publish)
    database = Database(settings.data_dir)
    with pytest.raises(StoreError):
        database.initialize(*hashes)
    assert not database.path.exists()
    assert not list(database.directory.iterdir())

