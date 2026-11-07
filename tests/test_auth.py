import base64

import pytest
from fastapi.testclient import TestClient

from junkyard.app import create_app
from junkyard.database import StoreError
from junkyard.security import CLIAuthenticator, hash_password
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


@pytest.fixture
def client(settings, database):
    with TestClient(create_app(settings)) as client:
        yield client


def test_only_cli_credentials_authorize(client):
    response = client.get("/auth/check", auth=("admin", CLI_PASSWORD))
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    for username, password in (("admin", BROWSER_PASSWORD), ("wrong", CLI_PASSWORD), ("admin", "wrong")):
        response = client.get("/auth/check", auth=(username, password))
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid CLI credentials"}
        assert 'charset="UTF-8"' in response.headers["www-authenticate"]


@pytest.mark.parametrize("header", [
    None, "Bearer token", "Basic !invalid!", "Basic YWRtaW4=",
    "Basic " + base64.b64encode(b"admin:\xff").decode(),
    "Basic " + base64.b64encode(b"admin:has\x00null").decode(),
    "Basic " + base64.b64encode(b"admin:" + b"x" * 1025).decode(),
    "Basic " + "A" * 4096,
])
def test_invalid_authorization_is_a_generic_challenge(client, header):
    response = client.get("/auth/check", headers={"Authorization": header} if header else {})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid CLI credentials"}
    assert response.headers["cache-control"] == "no-store"


def test_duplicate_headers_and_cookies_do_not_authorize(client):
    header = "Basic " + base64.b64encode(f"admin:{CLI_PASSWORD}".encode()).decode()
    assert client.get("/auth/check", headers=[("Authorization", header), ("Authorization", header)]).status_code == 401
    assert client.get("/auth/check", headers={"Cookie": "session=fake; password=" + CLI_PASSWORD}).status_code == 401


def test_unicode_password_and_colon_are_supported(client, database):
    password = "пароль: достаточно длинный"
    database.replace_password("cli", hash_password(password))
    assert client.get("/auth/check", auth=("admin", password)).status_code == 204


def test_reset_revokes_old_cli_password_immediately(client, database):
    password = "new CLI password for recovery"
    database.replace_password("cli", hash_password(password))
    assert client.get("/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 401
    assert client.get("/auth/check", auth=("admin", password)).status_code == 204


def test_corrupted_hash_fails_closed_without_exposing_it(client, database):
    database.replace_password("cli", "damaged-secret-hash")
    response = client.get("/auth/check", auth=("admin", CLI_PASSWORD))
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication unavailable"}


def test_verification_concurrency_is_bounded(client):
    slots = client.app.state.cli_authenticator._slots
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    try:
        response = client.get("/auth/check", auth=("admin", CLI_PASSWORD))
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
    finally:
        slots.release()
        slots.release()
    assert client.get("/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 204


def test_reset_during_verification_rejects_old_password(database, monkeypatch):
    original_read = database.password_hash
    calls = 0

    def changing_hash(kind):
        nonlocal calls
        calls += 1
        return original_read(kind) if calls == 1 else "changed-during-verification"

    monkeypatch.setattr(database, "password_hash", changing_hash)
    assert not CLIAuthenticator(database).verify("admin", CLI_PASSWORD)


def test_server_requires_initialization(settings):
    with pytest.raises(StoreError, match="missing"):
        with TestClient(create_app(settings)):
            pytest.fail("uninitialized application must not start")
    assert not settings.data_dir.exists()


def test_no_public_setup_or_documentation_endpoint(client):
    for path in ("/setup", "/init", "/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_authentication_never_logs_passwords(client, caplog):
    client.get("/auth/check", auth=("admin", CLI_PASSWORD))
    client.get("/auth/check", auth=("admin", BROWSER_PASSWORD))
    assert CLI_PASSWORD not in caplog.text
    assert BROWSER_PASSWORD not in caplog.text
