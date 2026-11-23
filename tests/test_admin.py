from dataclasses import replace
import hashlib
import re

from fastapi.testclient import TestClient
import pytest

from conftest import BROWSER_PASSWORD, CLI_PASSWORD, representation_headers
from junkyard.app import create_app
from junkyard.browser_auth import LOGIN_COOKIE, SESSION_COOKIE, IDLE_SECONDS
from junkyard.file_options import FileOptions
from junkyard.rate_limits import RateLimited


@pytest.fixture
def client(settings, database, clock):
    app = create_app(replace(settings, anonymous_burst=100))
    app.state.browser_auth._clock = clock
    app.state.limits.authentication._clock = clock
    app.state.limits.anonymous._clock = clock
    with TestClient(app, base_url=settings.public_url, follow_redirects=False) as client:
        yield client


def csrf(response):
    assert response.status_code == 200
    return re.search(r'<meta name="csrf-token" content="([0-9a-f]{64})">', response.text)[1]


def login(client):
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf(client.get("/admin/login"))}
    response = client.post("/admin/login", json={"password": BROWSER_PASSWORD}, headers=headers)
    assert response.status_code == 200, response.text
    headers["X-CSRF-Token"] = csrf(client.get("/admin/"))
    headers["Accept"] = "application/json"
    return headers


def test_login_cookie_flags_and_authentication_separation(client, database):
    page = client.get("/admin/login")
    challenge = client.cookies[LOGIN_COOKIE]
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf(page)}
    assert client.post("/admin/login", json={"password": CLI_PASSWORD}, headers=headers).status_code == 401
    response = client.post("/admin/login", json={"password": BROWSER_PASSWORD}, headers=headers)
    assert response.status_code == 200
    token = client.cookies[SESSION_COOKIE]
    assert token != challenge and len(token) == 43
    assert LOGIN_COOKIE not in client.cookies
    header = next(h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE))
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=43200"):
        assert flag in header
    assert "Domain=" not in header
    assert token not in client.get("/admin/").text
    assert client.get("/auth/check").status_code == 401
    assert client.put("/u/test", content=b"unauthorized").status_code == 401
    assert client.get("/admin/", auth=("admin", CLI_PASSWORD)).status_code == 401
    client.cookies.clear()
    assert client.get("/admin/").headers["location"] == "/admin/login"
    assert client.put("/admin/files?name=test", content=b"no", auth=("admin", CLI_PASSWORD)).status_code == 401


def test_admin_file_lifecycle_and_rotation(client, database):
    headers = login(client)
    browser_hash = database.password_hash("browser")
    response = client.put("/admin/files?name=example.txt&s=1&e=7d", content=b"original", headers=headers)
    assert response.status_code == 201
    original = response.json()
    file_id = original["id"]
    assert len(file_id) == 32 and original["filename"] == "example.txt"
    assert client.get("/" + file_id).content == b"original"
    endpoint = "/admin/files/" + file_id
    replaced = client.put(endpoint, content=b"replacement", headers=headers)
    assert replaced.status_code == 200
    assert replaced.json()["expires_at"] == original["expires_at"]
    assert replaced.json()["filename"] == original["filename"]
    modified = client.patch(endpoint + "?name=renamed.txt&e=0", headers=headers)
    assert modified.status_code == 200 and modified.json()["expires_at"] is None
    page = client.get("/admin/")
    assert "renamed.txt" in page.text and "11" in page.text
    head = client.head("/admin/")
    assert not head.content and representation_headers(head) == representation_headers(page)
    payload = {"browser_password": BROWSER_PASSWORD, "new_password": "new CLI passphrase test",
               "confirmation": "new CLI passphrase test"}
    assert client.post("/admin/cli-password", json=payload, headers=headers).status_code == 204
    assert database.password_hash("browser") == browser_hash
    assert client.get("/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 401
    assert client.get("/auth/check", auth=("admin", payload["new_password"])).status_code == 204
    assert client.get("/admin/").status_code == 200
    assert client.delete(endpoint, headers=headers).status_code == 204
    assert client.get("/" + file_id).status_code == 404
    token = client.cookies[SESSION_COOKIE]
    assert client.post("/admin/logout", headers=headers).status_code == 204
    assert SESSION_COOKIE not in client.cookies
    assert client.app.state.browser_auth.resolve(token) is None
    assert client.get("/admin/").status_code == 303


def test_file_token_lifecycle(client, database):
    admin = login(client)
    for endpoint in ("/u/report.txt?e=7d", "/admin/files?name=report.txt&e=7d"):
        auth = ("admin", CLI_PASSWORD) if endpoint.startswith("/u/") else None
        created = client.put(endpoint, headers=admin, auth=auth, content=b"first")
        assert created.status_code == 201
        original = created.json()
        assert "token" not in original
        file_id = original["id"]
        path = "/" + file_id
        manage = "/admin/files" + path + "/token"
        with database.connect() as connection:
            assert connection.execute("SELECT file_token_hash FROM files WHERE id = ?", (file_id,)).fetchone()[0] is None
        response = client.post(manage, headers=admin)
        assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
        token = response.json()["token"]
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with database.connect() as connection:
            assert connection.execute("SELECT file_token_hash FROM files WHERE id = ?", (file_id,)).fetchone()[0] == digest
        assert client.post(manage, headers=admin).status_code == 409
        page = client.get("/admin/").text
        assert "Token active." in page and token not in page and digest not in page
        bearer = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        for revision, contents in enumerate((b"second", b"third"), start=2):
            response = client.put(path, content=contents, headers=bearer)
            assert response.status_code == 200
            updated = response.json()
            assert updated["revision"] == revision and updated["size"] == len(contents)
            for key in ("id", "url", "filename", "created_at", "expires_at"):
                assert updated[key] == original[key]
            assert client.get(path).content == contents
        assert client.delete(manage, headers=admin).status_code == 204
        assert client.put(path, content=b"blocked", headers=bearer).status_code == 401
        assert client.get(path).content == b"third"
        new_token = client.post(manage, headers=admin).json()["token"]
        assert new_token != token
        assert client.put(path, content=b"blocked", headers=bearer).status_code == 401
        assert client.put(path, content=b"fourth", headers={"Authorization": "Bearer " + new_token}).status_code == 200


@pytest.fixture
def file_token(client):
    admin = login(client)
    path = "/" + client.put("/admin/files?e=1h", content=b"original", headers=admin).json()["id"]
    manage = "/admin/files" + path + "/token"
    token = client.post(manage, headers=admin).json()["token"]
    return path, manage, admin, {"Authorization": "Bearer " + token}


@pytest.mark.parametrize("method,target,status", [
    ("PUT", "/other", 401), ("PUT", "/u", 401), ("PUT", "/u/new", 401),
    ("PATCH", "/target", 401), ("DELETE", "/target", 401), ("GET", "/target", 401),
    ("GET", "/auth/check", 401), ("POST", "/manage", 401), ("DELETE", "/manage", 401),
    ("PUT", "/target?name=new", 400), ("PUT", "/target?e=0", 400), ("PUT", "/target?extra=1", 400),
])
def test_file_token_scope_before_body(client, file_token, method, target, status):
    path, manage, admin, bearer = file_token
    other = "/" + client.put("/admin/files", headers=admin, content=b"other").json()["id"]
    target = target.replace("/target", path).replace("/manage", manage).replace("/other", other)
    client.cookies.clear()

    def unread():
        pytest.fail("Rejected token request consumed the body")
        yield b"unreachable"

    assert client.request(method, target, headers=bearer, content=unread()).status_code == status
    assert client.get(path).content == b"original" and client.get(other).content == b"other"


def test_file_token_management_requires_browser_session(client, file_token):
    _, manage, admin, _ = file_token
    client.cookies.clear()
    for method in ("POST", "DELETE"):
        assert client.request(method, manage, headers=admin).status_code == 401
        assert client.request(method, manage, headers=admin, auth=("admin", CLI_PASSWORD)).status_code == 401


def test_revocation_and_reissue_during_stream(client, file_token):
    path, _, _, bearer = file_token
    store = client.app.state.files

    def body():
        yield b"part one"
        store.revoke_file_token(path[1:])
        store.generate_file_token(path[1:])
        yield b"part two"

    assert client.put(path, headers=bearer, content=body()).status_code == 401
    assert client.get(path).content == b"original"
    assert store.used_bytes == len(b"original") and store._transfers == 0
    assert not list(store.staging.iterdir())


def test_file_token_ends_with_file_even_if_id_reused(client, file_token, monkeypatch):
    path, manage, admin, bearer = file_token
    store = client.app.state.files
    expires_at = store._find(path[1:]).expires_at
    monkeypatch.setattr(store, "_clock", lambda: expires_at)
    assert client.put(path, headers=bearer, content=b"expired").status_code == 401
    assert client.post(manage, headers=admin).status_code == 404
    store.cleanup()
    monkeypatch.setattr(store, "_new_id", lambda secure: path[1:])
    for _ in range(2):
        assert client.put("/admin/files", headers=admin, content=b"new file").status_code == 201
        assert client.put(path, headers=bearer, content=b"wrong file").status_code == 401
        token = client.post(manage, headers=admin).json()["token"]
        bearer = {"Authorization": "Bearer " + token}
        assert client.delete("/admin/files" + path, headers=admin).status_code == 204


def test_bad_file_tokens_share_authentication_budget(client, file_token):
    path, _, _, bearer = file_token
    duplicate = [*bearer.items(), *bearer.items()]
    assert client.put(path, headers=duplicate).status_code == 401
    for invalid in ("", "x" * 42, "a" * 4097, "a" * 43):
        assert client.put(path, headers={"Authorization": "Bearer " + invalid}).status_code == 401
    assert client.put(path, headers=bearer).status_code == 429
    assert client.get("/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 429


@pytest.mark.parametrize("mutation", ["login", "upload", "replace", "edit", "delete", "logout", "rotate",
                                      "generate_token", "revoke_token"])
@pytest.mark.parametrize("attack", ["missing_origin", "cross_origin", "missing_csrf", "wrong_csrf"])
def test_all_mutations_require_origin_and_bound_csrf(client, mutation, attack):
    if mutation == "login":
        headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf(client.get("/admin/login"))}
    else:
        headers = login(client)
    if attack == "missing_origin":
        headers.pop("Origin")
    elif attack == "cross_origin":
        headers["Origin"] = "https://evil.example.com"
    elif attack == "missing_csrf":
        headers.pop("X-CSRF-Token")
    else:
        headers["X-CSRF-Token"] = "0" * 64
    method, path = {
        "login": ("POST", "/admin/login"), "upload": ("PUT", "/admin/files"),
        "replace": ("PUT", "/admin/files/NoSuch"), "edit": ("PATCH", "/admin/files/NoSuch"),
        "delete": ("DELETE", "/admin/files/NoSuch"), "logout": ("POST", "/admin/logout"),
        "rotate": ("POST", "/admin/cli-password"),
        "generate_token": ("POST", "/admin/files/NoSuch/token"), "revoke_token": ("DELETE", "/admin/files/NoSuch/token"),
    }[mutation]
    consumed = []

    def body():
        consumed.append(True)
        yield b"untrusted body"

    response = client.request(method, path, content=body(), headers=headers)
    assert response.status_code == 403
    assert not consumed  # Security checks must run before parsing or streaming input.


def test_csrf_from_one_session_does_not_authorize_another(client):
    old_headers = login(client)
    client.cookies.clear()
    new_headers = login(client)
    assert old_headers["X-CSRF-Token"] != new_headers["X-CSRF-Token"]
    assert client.post("/admin/logout", headers=old_headers).status_code == 403
    assert client.post("/admin/logout", headers=new_headers).status_code == 204


def test_idle_expiration_and_password_reset_deny_new_requests(client, clock, database, hashes):
    headers = login(client)
    clock.advance(IDLE_SECONDS)
    assert client.put("/admin/files", content=b"no", headers=headers).status_code == 401
    headers = login(client)
    database.replace_password("browser", hashes[1])
    assert client.put("/admin/files", content=b"no", headers=headers).status_code == 401
    assert client.get("/admin/").status_code == 303


def test_reset_during_upload_aborts_staging(client, database, hashes):
    headers = login(client)

    def body():
        yield b"partial"
        database.replace_password("browser", hashes[1])
        yield b"remaining"

    response = client.put("/admin/files?name=test.txt", content=body(), headers=headers)
    assert response.status_code == 401
    store = client.app.state.files
    assert store.used_bytes == 0 and store._transfers == 0
    assert not list(store.staging.iterdir())
    assert store.list_files(1)["total"] == 0


def test_login_and_rotation_share_pre_hash_limits_with_cli(client, monkeypatch):
    headers = login(client)
    calls = []

    def fail(password):
        from junkyard.file_options import FileError
        calls.append(password)
        raise FileError(401, "Invalid browser credentials")

    monkeypatch.setattr(client.app.state.browser_auth, "_browser_credential", fail)
    payload = {"browser_password": "incorrect password", "new_password": "new CLI passphrase", "confirmation": "new CLI passphrase"}
    for _ in range(5):
        assert client.post("/admin/cli-password", json=payload, headers=headers).status_code == 401
    limited = client.post("/admin/cli-password", json=payload, headers=headers)
    assert limited.status_code == 429 and len(calls) == 5
    assert limited.headers["retry-after"] == "12"
    assert client.get("/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 429
    assert client.get("/admin/").status_code == 200


def test_failed_login_throttle_and_success_refund(client):
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf(client.get("/admin/login"))}
    for _ in range(5):
        assert client.post("/admin/login", json={"password": "wrong password"}, headers=headers).status_code == 401
    assert client.post("/admin/login", json={"password": BROWSER_PASSWORD}, headers=headers).status_code == 429


@pytest.mark.parametrize("body,content_type,status", [
    (b'{"password":"one", "password":"two"}', "application/json", 400),
    (b'{"password":"\\ud800"}', "application/json", 400),
    (b'{"password":null}', "application/json", 400),
    (b'[]', "application/json", 400), (b'bad', "application/json", 400),
    (b'{"password":"x"}', "text/plain", 415), (b'x' * 8193, "application/json", 413),
])
def test_login_rejects_untrusted_json_without_echoing_it(client, body, content_type, status):
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf(client.get("/admin/login")),
               "Content-Type": content_type}
    response = client.post("/admin/login", content=body, headers=headers)
    assert response.status_code == status
    assert SESSION_COOKIE not in client.cookies


@pytest.mark.parametrize("new,confirmation,status", [
    (BROWSER_PASSWORD, BROWSER_PASSWORD, 400), ("short", "short", 400),
    ("new CLI passphrase", "different passphrase", 400),
])
def test_invalid_rotation_does_not_change_credentials(client, database, new, confirmation, status):
    headers = login(client)
    previous = database.password_hash("cli")
    response = client.post("/admin/cli-password", headers=headers, json={
        "browser_password": BROWSER_PASSWORD, "new_password": new, "confirmation": confirmation})
    assert response.status_code == status
    assert database.password_hash("cli") == previous


def test_paginated_list_escaping_and_storage_usage_include_retained_blobs(client):
    headers = login(client)
    store = client.app.state.files
    records = []
    for i in range(26):
        upload = store.begin(FileOptions(name=f"file {i:02}.txt", lifetime=0))
        store.write(upload, b"old")
        records.append(store.finish(upload))
        store.abort(upload)
    target = records[0]
    pin = store.open_download(target.id)
    upload = store.begin(FileOptions(name='<img src=x onerror="bad">.txt'), target.id)
    try:
        store.write(upload, b"new contents")
        store.finish(upload)
        store.abort(upload)
        pending = store.begin(FileOptions(name="pending.txt"))
        try:
            store.write(pending, b"pending")
            expected = 26 * 3 + 12 + 7
            assert store.list_files(1)["used_bytes"] == expected
            first = client.get("/admin/")
            second = client.get("/admin/?page=2")
            assert first.text.count('class="file-entry"') == 25
            assert second.text.count('class="file-entry"') == 1
            combined = first.text + second.text
            assert f"<strong>{expected}</strong>" in combined
            assert "&lt;img" in combined and "<img src=x" not in combined
            assert target.blob not in combined
            assert "Next page" in first.text and "Previous page" in second.text
            assert "pending.txt" not in combined
        finally:
            store.abort(pending)
    finally:
        store.abort(upload)
        store.close_download(pin)
    assert store.used_bytes == 25 * 3 + 12
    for query in ("page=0", "page=-1", "page=1&page=2", "page=9999999", "unexpected=1"):
        assert client.get("/admin/?" + query).status_code == 400


def test_duplicate_session_cookies_fail_closed(client):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    response = client.get("/admin/", headers={"Cookie": f"{SESSION_COOKIE}={token}; {SESSION_COOKIE}={token}"})
    assert response.status_code == 303


def test_anonymous_cookie_cannot_bypass_read_limits_and_valid_session_can(client):
    login(client)
    limits = client.app.state.limits
    # TestClient's non-IP socket peer is normalized to 'unknown'.
    try:
        while True:
            limits.anonymous.take("unknown")
    except RateLimited:
        pass
    assert client.get("/admin/").status_code == 200
    client.cookies.clear()
    assert client.get("/admin/login", headers={"Cookie": f"{SESSION_COOKIE}={'x' * 43}"}).status_code == 429
