import sqlite3

import pytest
from starlette.requests import Request

from conftest import BROWSER_PASSWORD, CLI_PASSWORD
from junkyard.browser_auth import (
    ABSOLUTE_SECONDS, IDLE_SECONDS, LOGIN_COOKIE, LOGIN_SECONDS, MAX_SESSIONS,
    BrowserAuth, check_csrf, check_origin, csrf_token, token_hash,
)
from junkyard.database import StoreError
from junkyard.file_options import FileError, FileOptions
from junkyard.files import FileStore
from junkyard.security import AuthenticationBusy, CLIAuthenticator, PASSWORD_HASHER


@pytest.fixture
def auth(database, clock):
    return BrowserAuth(database, CLIAuthenticator(database), clock=clock)


def test_sessions_are_hashed_persistent_and_rotated(auth, database, clock):
    token = auth.login(BROWSER_PASSWORD)
    session = auth.resolve(token)
    assert session.csrf != token
    with database.connect() as connection:
        row = dict(connection.execute("SELECT * FROM browser_sessions").fetchone())
    assert row["token_hash"] == token_hash(token)
    assert token not in str(row) and session.csrf not in str(row)
    restarted = BrowserAuth(database, CLIAuthenticator(database), clock=clock)
    assert restarted.resolve(token) == session
    replacement = auth.login(BROWSER_PASSWORD, previous=token)
    assert replacement != token
    assert auth.resolve(token) is None
    assert auth.resolve(replacement) is not None
    auth.logout(auth.resolve(replacement))
    assert auth.resolve(replacement) is None


def test_idle_deadline_and_absolute_expiration(auth, clock):
    token = auth.login(BROWSER_PASSWORD)
    clock.advance(IDLE_SECONDS - 1)
    assert auth.resolve(token) is not None
    clock.advance(IDLE_SECONDS)
    assert auth.resolve(token) is None
    token = auth.login(BROWSER_PASSWORD)
    start = clock.now
    for _ in range(24):
        clock.advance(ABSOLUTE_SECONDS / 24 - 1)
        assert auth.resolve(token) is not None
    clock.now = start + ABSOLUTE_SECONDS
    assert auth.resolve(token) is None


def test_reset_revokes_all_browser_sessions_but_cli_reset_does_not(auth, database, hashes):
    tokens = [auth.login(BROWSER_PASSWORD), auth.login(BROWSER_PASSWORD)]
    database.replace_password("cli", hashes[0])
    assert all(auth.resolve(token) for token in tokens)
    database.replace_password("browser", hashes[1])
    assert all(auth.resolve(token) is None for token in tokens)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0] == 0


def test_session_cap_and_expired_cleanup(auth, clock, database, monkeypatch):
    # Authentication itself is covered elsewhere; avoid dozens of redundant hashes.
    with database.connect() as connection:
        credential = connection.execute("SELECT password_hash, version FROM credentials WHERE kind = 'browser'").fetchone()
    monkeypatch.setattr(auth, "_browser_credential", lambda password: credential)
    tokens = [auth.login(BROWSER_PASSWORD) for _ in range(MAX_SESSIONS + 1)]
    assert auth.resolve(tokens[0]) is None
    assert all(auth.resolve(token, touch=False) for token in tokens[1:])
    clock.advance(IDLE_SECONDS)
    newest = auth.login(BROWSER_PASSWORD)
    with database.connect() as connection:
        rows = connection.execute("SELECT token_hash FROM browser_sessions").fetchall()
    assert [row[0] for row in rows] == [token_hash(newest)]


def test_reset_during_password_verification_cannot_issue_session(auth, database, hashes, monkeypatch):
    verify = auth._browser_credential

    def reset(password):
        credential = verify(password)
        database.replace_password("browser", hashes[1])
        return credential

    monkeypatch.setattr(auth, "_browser_credential", reset)
    with pytest.raises(FileError) as error:
        auth.login(BROWSER_PASSWORD)
    assert error.value.status == 401
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0] == 0


def test_rotation_requires_live_session_and_does_not_change_browser_credential(auth, database):
    token = auth.login(BROWSER_PASSWORD)
    session = auth.resolve(token)
    browser_hash = database.password_hash("browser")
    auth.rotate_cli(session, BROWSER_PASSWORD, "replacement CLI passphrase")
    assert database.password_hash("browser") == browser_hash
    assert auth.resolve(token) is not None
    assert auth.authenticator.verify("admin", "replacement CLI passphrase")
    assert not auth.authenticator.verify("admin", CLI_PASSWORD)
    auth.logout(session)
    with pytest.raises(FileError) as error:
        auth.rotate_cli(session, BROWSER_PASSWORD, "another replacement passphrase")
    assert error.value.status == 401


@pytest.mark.parametrize("interference", ["browser_reset", "cli_reset", "logout"])
def test_rotation_races_cannot_overwrite_reset_or_use_revoked_session(auth, database, hashes, monkeypatch, interference):
    session = auth.resolve(auth.login(BROWSER_PASSWORD))
    original_hash = database.password_hash("cli")
    from junkyard.browser_auth import hash_password

    def interrupted(password):
        result = hash_password(password)
        if interference == "logout":
            auth.logout(session)
        else:
            kind = "browser" if interference == "browser_reset" else "cli"
            database.replace_password(kind, hashes[1] if kind == "browser" else hashes[0])
        return result

    monkeypatch.setattr("junkyard.browser_auth.hash_password", interrupted)
    with pytest.raises(FileError) as error:
        auth.rotate_cli(session, BROWSER_PASSWORD, "replacement CLI passphrase")
    assert error.value.status == (409 if interference == "cli_reset" else 401)
    assert database.password_hash("cli") == original_hash


def test_browser_and_cli_share_hashing_concurrency_limit(auth):
    slots = auth.authenticator._slots
    slots.acquire()
    slots.acquire()
    try:
        with pytest.raises(AuthenticationBusy):
            auth.login(BROWSER_PASSWORD)
    finally:
        slots.release()
        slots.release()


def test_corrupt_browser_hash_fails_closed(auth, database):
    database.replace_password("browser", "invalid-sensitive-hash")
    with pytest.raises(StoreError):
        auth.login(BROWSER_PASSWORD)


def make_request(headers):
    return Request({"type": "http", "headers": [(key.lower().encode(), value.encode()) for key, value in headers]})


def test_login_challenge_binding_expiry_and_restart(auth, clock, database):
    challenge = auth.login_challenge()
    request = make_request([("Cookie", f"{LOGIN_COOKIE}={challenge}"), ("X-CSRF-Token", csrf_token(challenge))])
    auth.check_login(request)
    with pytest.raises(FileError):
        BrowserAuth(database, auth.authenticator, clock=clock).check_login(request)
    clock.advance(LOGIN_SECONDS)
    with pytest.raises(FileError):
        auth.check_login(request)


@pytest.mark.parametrize("origin", [None, "null", "http://files.example.com", "https://evil.example.com",
    "https://files.example.com.evil.test", "https://files.example.com:444", "https://files.example.com:0",
    "https://files.example.com/", "https://files.example.com?", "https://files.example.com#",
    "https://user@files.example.com", "https://files.example.com https://evil.test"])
def test_origin_must_match_configured_https_origin(origin):
    request = make_request([("Origin", origin)] if origin else [])
    with pytest.raises(FileError) as error:
        check_origin(request, "https://files.example.com")
    assert error.value.status == 403


def test_duplicate_origin_and_csrf_headers_rejected():
    origin = "https://files.example.com"
    for allowed in (origin, origin + ":443"):
        check_origin(make_request([("Origin", allowed)]), origin)
    with pytest.raises(FileError):
        check_origin(make_request([("Origin", origin), ("Origin", origin)]), origin)
    with pytest.raises(FileError):
        check_csrf(make_request([("X-CSRF-Token", "a" * 64), ("X-CSRF-Token", "a" * 64)]), "a" * 64)


def test_v2_migration_preserves_files_and_credentials(database, settings, hashes):
    store = FileStore(database, settings)
    store.start()
    upload = store.begin(FileOptions(name="preserved.txt"))
    try:
        store.write(upload, b"preserved")
        record = store.finish(upload)
    finally:
        store.abort(upload)
        store.close()
    with database.connect() as connection, connection:
        connection.execute("DROP TABLE browser_sessions")
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(StoreError, match="migrate"):
        database.require_file_schema()
    database.migrate()
    database.migrate()
    assert (database.password_hash("cli"), database.password_hash("browser")) == hashes
    store.start()
    download = store.open_download(record.id)
    try:
        assert download.file.read() == b"preserved"
    finally:
        store.close_download(download)
        store.close()


def test_v2_migration_failure_rolls_back(database, monkeypatch):
    with database.connect() as connection, connection:
        connection.execute("DROP TABLE browser_sessions")
        connection.execute("PRAGMA user_version = 2")

    def fail(connection):
        connection.execute("CREATE TABLE partial_sessions (id INTEGER)")
        raise sqlite3.OperationalError("injected error")

    monkeypatch.setattr("junkyard.database.create_session_schema", fail)
    with pytest.raises(StoreError):
        database.migrate()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='partial_sessions'").fetchone() is None
