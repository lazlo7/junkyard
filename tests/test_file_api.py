import asyncio
from dataclasses import replace
import errno
from urllib.parse import urlsplit

import anyio
from fastapi.testclient import TestClient
import pytest
from starlette.requests import ClientDisconnect, Request

from junkyard.app import create_app
from junkyard.file_options import FileError
from junkyard.file_routes import DownloadResponse, upload_file
from junkyard.files import CHUNK_SIZE
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


@pytest.fixture
def client(settings, database):
    settings = replace(settings, max_file_bytes=100, storage_quota_bytes=200)
    with TestClient(create_app(settings)) as client:
        yield client


def create(client, path="/u/example.txt", data=b"example", **kwargs):
    return client.put(path, content=data, auth=("admin", CLI_PASSWORD), **kwargs)


def test_crud_and_metadata_responses(client):
    created = create(client, headers={"Accept": "application/json", "Host": "untrusted.example"})
    assert created.status_code == 201
    record = created.json()
    assert len(record["id"]) == 6
    assert record["url"] == "https://files.example.com/" + record["id"]
    assert record["filename"] == "example.txt"
    assert record["expires_at"] - record["created_at"] == 30 * 86400
    assert "blob" not in record
    path = "/" + record["id"]
    for suffix in ("", "/raw", "/download"):
        response = client.get(path + suffix)
        assert response.content == b"example"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-length"] == "7"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    replaced = create(client, path, b"new", headers={"Accept": "application/json"})
    assert replaced.status_code == 200
    assert replaced.json()["filename"] == "example.txt"
    assert replaced.json()["expires_at"] == record["expires_at"]
    changed = client.patch(path + "?name=renamed.txt&e=0", auth=("admin", CLI_PASSWORD), headers={"Accept": "application/json"})
    assert changed.status_code == 200
    assert changed.json()["filename"] == "renamed.txt"
    assert changed.json()["expires_at"] is None
    assert changed.json()["revision"] == 3
    assert client.get(path).content == b"new"
    assert client.delete(path, auth=("admin", CLI_PASSWORD)).status_code == 204
    assert client.get(path).status_code == 404
    assert client.app.state.files.used_bytes == 0


def test_long_id_default_name_and_plain_url_response(client):
    response = create(client, "/u?s=1&e=7d", b"")
    assert response.status_code == 201
    assert response.text == response.headers["location"] + "\n"
    file_id = urlsplit(response.text.strip()).path[1:]
    assert len(file_id) == 32
    record = client.app.state.files._find(file_id)
    assert record.name == file_id
    assert record.expires_at - record.created_at == 7 * 86400
    assert client.get("/" + file_id).content == b""


@pytest.mark.parametrize("method,path", [("PUT", "/u/name"), ("PUT", "/AbCd12"), ("PATCH", "/AbCd12?e=0"), ("DELETE", "/AbCd12")])
def test_all_mutations_require_cli_credentials(client, method, path):
    for auth in (None, ("admin", BROWSER_PASSWORD)):
        response = client.request(method, path, auth=auth)
        assert response.status_code == 401
    assert client.app.state.files.used_bytes == 0


@pytest.mark.parametrize("path", [
    "/u/%2e%2e", "/u/dir%2Fname", "/u/dir%5Cname", "/u/%00bad",
    "/u/name?name=evil%0D%0AX-Injected%3Ayes", "/u/%FF", "/u/" + "a" * 256,
    "/u/name?name=../escape", "/u/name?name=", "/u/name?e=-1d",
    "/u/name?e=0d", "/u/name?e=tomorrow", "/u/name?e=999999999999d",
    "/u/name?e=1d&e=2d", "/u/name?s=0", "/u/name?unexpected=1",
    "/u/name?name=%FF", "/u/name?e=%ZZ",
])
def test_invalid_names_encoding_and_parameters_leave_no_uploads(client, path):
    response = create(client, path)
    assert response.status_code == 400
    store = client.app.state.files
    assert store.used_bytes == 0
    assert not list(store.staging.iterdir())
    assert not list(store.blobs.iterdir())


def test_filename_quotes_unicode_and_literal_percent_are_safe(client):
    response = create(client, "/u/name?name=%D1%84%D0%B0%D0%B9%D0%BB%22%25.txt")
    assert response.status_code == 201
    downloaded = client.get(urlsplit(response.text.strip()).path)
    disposition = downloaded.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%22%25.txt" in disposition
    assert "\r" not in disposition and "\n" not in disposition


def test_newline_path_is_rejected_by_router(client):
    response = create(client, "/u/evil%0D%0AX-Injected%3Ayes")
    assert response.status_code == 404
    assert "x-injected" not in response.headers
    assert client.app.state.files.used_bytes == 0


def test_limits_apply_to_declared_and_streamed_bodies(client):
    assert create(client, data=b"a" * 101).status_code == 413
    assert create(client, data=iter([b"a" * 60, b"b" * 41])).status_code == 413
    assert client.app.state.files.used_bytes == 0
    assert create(client, data=b"a" * 100).status_code == 201
    assert create(client, data=iter([b"b" * 100])).status_code == 201
    assert create(client, data=iter([b"c"])).status_code == 507
    assert client.app.state.files.used_bytes == 200
    assert not list(client.app.state.files.staging.iterdir())


def test_disk_writes_are_bounded_when_asgi_delivers_a_large_chunk(client, monkeypatch):
    store = client.app.state.files
    store.settings = replace(store.settings, max_file_bytes=CHUNK_SIZE * 4, storage_quota_bytes=CHUNK_SIZE * 4)
    sizes = []
    original_write = store.write

    def write(upload, chunk):
        sizes.append(len(chunk))
        original_write(upload, chunk)

    monkeypatch.setattr(store, "write", write)
    content = b"a" * (CHUNK_SIZE * 2 + 1)
    response = create(client, data=content)
    assert response.status_code == 201
    assert sizes == [CHUNK_SIZE, CHUNK_SIZE, 1]
    assert client.get(urlsplit(response.text.strip()).path).content == content


def test_unauthenticated_upload_does_not_read_the_body(client):
    messages = []

    async def exercise():
        async def receive():
            pytest.fail("must authenticate before receiving upload data")

        async def send(message):
            messages.append(message)

        await client.app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "PUT", "scheme": "http", "path": "/u",
            "raw_path": b"/u", "query_string": b"", "root_path": "",
            "headers": [(b"content-length", b"100")], "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }, receive, send)

    anyio.run(exercise)
    assert messages[0]["status"] == 401
    assert not list(client.app.state.files.staging.iterdir())


def test_mismatched_length_aborts_instead_of_publishing(client):
    assert create(client, data=b"abc", headers={"Content-Length": "10"}).status_code == 400
    assert client.app.state.files.used_bytes == 0


def test_expired_file_cannot_be_read_or_revived(client):
    response = create(client)
    path = urlsplit(response.text.strip()).path
    with client.app.state.files.database.connect() as connection, connection:
        connection.execute("UPDATE files SET expires_at = 1")
    assert client.get(path).status_code == 404
    assert create(client, path, b"new").status_code == 404
    assert client.patch(path + "?e=0", auth=("admin", CLI_PASSWORD)).status_code == 404


def test_disk_error_is_generic_and_aborts(client, monkeypatch):
    def fail(*args):
        raise OSError(errno.ENOSPC, "sensitive internal pathname")

    monkeypatch.setattr(client.app.state.files, "write", fail)
    response = create(client)
    assert response.status_code == 507
    assert "sensitive" not in response.text
    assert not list(client.app.state.files.staging.iterdir())


def test_request_disconnect_cleans_up_partial_upload(client):
    app = client.app

    async def exercise():
        messages = iter([
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ])

        async def receive():
            return next(messages)

        request = Request({
            "type": "http", "method": "PUT", "path": "/u", "raw_path": b"/u",
            "query_string": b"", "headers": [], "app": app,
        }, receive)
        with pytest.raises(FileError) as error:
            await upload_file(request)
        assert error.value.status == 400

    anyio.run(exercise)
    assert app.state.files.used_bytes == 0
    assert app.state.files._transfers == 0
    assert not list(app.state.files.staging.iterdir())


def test_cancelled_upload_releases_staging_and_slot(client):
    app = client.app

    async def exercise():
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"partial", "more_body": True}
            raise asyncio.CancelledError

        request = Request({
            "type": "http", "method": "PUT", "path": "/u", "raw_path": b"/u",
            "query_string": b"", "headers": [], "app": app,
        }, receive)
        with pytest.raises(asyncio.CancelledError):
            await upload_file(request)

    anyio.run(exercise)
    assert app.state.files.used_bytes == 0
    assert app.state.files._transfers == 0


def test_download_disconnect_releases_pin_even_before_body(client):
    created = create(client)
    file_id = urlsplit(created.text.strip()).path[1:]
    store = client.app.state.files
    download = store.open_download(file_id)

    async def exercise():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise OSError("disconnected while sending headers")

        with pytest.raises(ClientDisconnect):
            await DownloadResponse(store, download)({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    anyio.run(exercise)
    assert download.file.closed
    assert store._transfers == 0
    assert not store._pins
    store.delete(file_id)
    assert store.used_bytes == 0
