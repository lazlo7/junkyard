from dataclasses import replace
from html.parser import HTMLParser
from io import BytesIO

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from junkyard.app import create_app
from junkyard.file_options import FileOptions
from junkyard.presentation import ASSETS, wants_html
from junkyard.previews import TEXT_LIMIT, inspect_preview
from conftest import CLI_PASSWORD

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 100


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.fixture
def client(settings, database):
    with TestClient(create_app(replace(settings, anonymous_burst=100))) as client:
        yield client


def publish(client, content=b"hello", name="notes.txt", lifetime=None):
    store = client.app.state.files
    upload = store.begin(FileOptions(name=name, lifetime=lifetime))
    try:
        store.write(upload, content)
        return store.finish(upload)
    finally:
        store.abort(upload)


@pytest.mark.parametrize("accept,html", [
    ("", False), ("*/*", False), ("text/*", False), ("text/html", True),
    ("text/html;q=0,*/*;q=1", False), ("text/html;q=0.5,application/octet-stream;q=1", False),
    ("text/html;q=0.5,application/octet-stream;q=0,*/*;q=1", True),
    ("text/html;q=0.5,application/json", False), ("text/html;q=0.5,text/plain", False),
    ("text/html;q=wat", False), ("text/html;q=NaN", False), ("text/html;q=1.1", False),
    ("text/html;q=1;q=0", False), ("text/html;level=1", False),
    ("TEXT/HTML; charset=utf-8", True), (HTML["Accept"], True),
])
def test_accept_negotiation(accept, html):
    request = Request({"type": "http", "headers": [(b"accept", accept.encode())]})
    assert wants_html(request) is html


def test_viewer_metadata_controls_canonical_share_link_and_head(client):
    record = publish(client, b"example text", lifetime=0)
    path = "/" + record.id
    response = client.get(path, headers={**HTML, "Host": "attacker.invalid", "Range": "bytes=0-1"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["vary"] == "Accept"
    assert "content-range" not in response.headers
    assert "etag" not in response.headers
    elements = Elements(response.text).elements
    assert ("input", {"id": "share-link", "type": "url", "readonly": None,
                      "value": f"https://files.example.com/{record.id}", "spellcheck": "false"}) in elements
    assert any(tag == "a" and attrs["href"] == path + "/download" for tag, attrs in elements)
    for value in (record.id, record.name, "12 bytes", "Uploaded", "Last modified", "Expires", "Never", "UTC", "example text"):
        assert value in response.text
    assert "attacker.invalid" not in response.text
    head = client.head(path, headers=HTML)
    assert head.content == b""
    assert dict(head.headers) == dict(response.headers)
    assert not client.app.state.files._pins
    assert client.app.state.files._transfers == 0
    for suffix in ("", "/raw", "/download"):
        raw = client.get(path + suffix, headers=HTML if suffix else {})
        assert raw.content == b"example text"
        assert raw.headers["content-disposition"].startswith("attachment;")
        assert raw.headers["content-type"] == "application/octet-stream"
    assert client.get(path).headers["vary"] == "Accept"


def test_text_and_filename_are_escaped_without_creating_markup(client):
    name = '<img src=x onerror="alert(1)"> & notes.txt'
    text = '<script>alert(1)</script><img src="https://attacker.invalid/leak"> & ${evil}'
    record = publish(client, text.encode(), name)
    response = client.get("/" + record.id, headers=HTML)
    assert "&lt;script&gt;" in response.text
    assert "&lt;img" in response.text
    elements = Elements(response.text).elements
    assert not any(tag in ("img", "iframe", "object", "embed") for tag, _ in elements)
    scripts = [attrs for tag, attrs in elements if tag == "script"]
    assert scripts == [{"src": "/assets/viewer.js", "defer": None}]
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'none'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name,body", [
    ("attack.html", b"<script>alert(1)</script>"), ("attack.SVG", b"<svg onload='alert(1)'/>"),
    ("document.pdf", b"%PDF-1.7"), ("attack.png", b"<svg onload='alert(1)'/>"),
    ("attack.jpg", b"<!doctype html><script>alert(1)</script>"),
    ("archive.bin", b"\x00\xff"), ("binary.txt", b"hello\x00world"),
    ("invalid.txt", b"\xff\xfe"), ("image.html", PNG),
])
def test_active_unknown_and_mislabeled_content_is_download_only(client, name, body):
    record = publish(client, body, name)
    path = "/" + record.id
    page = client.get(path, headers=HTML)
    assert "No browser preview" in page.text
    for suffix in ("/raw", "/download"):
        response = client.get(path + suffix, headers=HTML)
        assert response.content == body
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"] == "application/octet-stream"
        assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("name,body,kind,mime", [
    ("pixel.png", PNG, "image", "image/png"),
    ("photo.jpg", b"\xff\xd8\xff\xe0", "image", "image/jpeg"),
    ("animation.gif", b"GIF89a", "image", "image/gif"),
    ("image.webp", b"RIFF1234WEBP", "image", "image/webp"),
    ("sound.wav", b"RIFF1234WAVE", "audio", "audio/wav"),
    ("sound.mp3", b"ID3\x04\x00", "audio", "audio/mpeg"),
    ("sound.flac", b"fLaC1234", "audio", "audio/flac"),
    ("sound.ogg", b"OggS\x00", "audio", "audio/ogg"),
    ("movie.ogv", b"OggS\x00", "video", "video/ogg"),
    ("movie.mp4", MP4, "video", "video/mp4"),
    ("sound.m4a", MP4, "audio", "audio/mp4"),
    ("movie.webm", b"\x1a\x45\xdf\xa3\x87\x42\x82\x84webm", "video", "video/webm"),
])
def test_media_preview_raw_headers_and_seeking(client, name, body, kind, mime):
    record = publish(client, body, name)
    path = "/" + record.id
    page = client.get(path, headers=HTML)
    elements = Elements(page.text).elements
    assert any(tag == ("img" if kind == "image" else kind) for tag, _ in elements)
    assert any(attrs.get("src") == path + "/raw" for _, attrs in elements)
    raw = client.get(path + "/raw", headers=HTML)
    assert raw.content == body
    assert raw.headers["content-type"] == mime
    assert raw.headers["content-disposition"].startswith("inline;")
    head = client.head(path + "/raw", headers=HTML)
    assert head.content == b""
    assert dict(head.headers) == dict(raw.headers)
    ranged = client.get(path + "/raw", headers={"Range": "bytes=1-3", "If-Range": raw.headers["etag"]})
    assert ranged.status_code == 206
    assert ranged.content == body[1:4]
    assert ranged.headers["content-type"] == mime
    assert ranged.headers["content-range"] == f"bytes 1-3/{len(body)}"
    attachment = client.get(path + "/download", headers=HTML)
    assert attachment.headers["content-type"] == "application/octet-stream"
    assert attachment.headers["content-disposition"].startswith("attachment;")
    assert client.app.state.files._transfers == 0


def test_bounded_utf8_preview_and_truncation_notice(client):
    data = b"a" * (TEXT_LIMIT - 1) + "€tail".encode()
    record = publish(client, data)
    page = client.get("/" + record.id, headers=HTML)
    assert "Preview truncated" in page.text
    assert "\ufffd" not in page.text
    assert "€tail" not in page.text
    assert client.get("/" + record.id + "/download").content == data

    class BoundedRead(BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= TEXT_LIMIT + 1
            return super().read(size)

    assert inspect_preview(BoundedRead(data), "notes.txt", include_text=True).truncated
    exact = inspect_preview(BoundedRead(b"a" * TEXT_LIMIT), "notes.txt", include_text=True)
    assert not exact.truncated and len(exact.text) == TEXT_LIMIT
    assert inspect_preview(BoundedRead(b""), "empty.txt", include_text=True).kind == "text"
    assert inspect_preview(BoundedRead(b"\xef\xbb\xbfhello"), "notes.txt", include_text=True).text == "hello"
    assert inspect_preview(BoundedRead(b"hello\xe2"), "notes.txt", include_text=True).kind == "unsupported"


def test_negotiated_errors_and_head_preserve_status_and_headers(client):
    for path in ("/NoSuch", "/unknown/nested"):
        for accept, media in ((HTML, "text/html"), ({}, "application/json")):
            response = client.get(path, headers=accept)
            assert response.status_code == 404
            assert response.headers["content-type"].startswith(media)
            head = client.head(path, headers=accept)
            assert head.status_code == 404 and not head.content
            assert dict(head.headers) == dict(response.headers)
    for suffix in ("/raw", "/download"):
        response = client.get("/NoSuch" + suffix, headers=HTML)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
    record = publish(client)
    ranged = client.get("/" + record.id + "/raw", headers={**HTML, "Range": "bytes=100-"})
    assert ranged.status_code == 416
    assert ranged.headers["content-range"] == "bytes */5"
    assert ranged.headers["content-type"].startswith("application/json")


def test_throttle_errors_and_fixed_assets(settings, database):
    with TestClient(create_app(replace(settings, anonymous_burst=1, anonymous_rate=0.001))) as client:
        record = publish(client)
        path = "/" + record.id
        assert client.get(path, headers=HTML).status_code == 200
        for asset in ASSETS:
            response = client.get(asset)
            assert response.status_code == 200
            assert response.headers["x-content-type-options"] == "nosniff"
            assert client.head(asset).headers == response.headers
            assert client.head(asset).content == b""
        limited = client.get(path, headers=HTML)
        assert limited.status_code == 429
        assert "Try again in" in limited.text
        assert int(limited.headers["retry-after"]) > 0
        assert client.head(path, headers=HTML).content == b""
        assert client.get("/assets/unlisted.js").status_code == 429
        assert client.get(path).json() == {"detail": "Too many requests"}
        assert client.get(path + "/raw", headers=HTML).headers["content-type"].startswith("application/json")


def test_expired_deleted_and_unknown_viewer_pages_match(client, clock):
    store = client.app.state.files
    store._clock = clock
    expiring = publish(client, lifetime=1)
    deleted = publish(client)
    store.delete(deleted.id)
    clock.advance(2)
    responses = [client.get("/" + file_id, headers=HTML) for file_id in (expiring.id, deleted.id, "NoSuch")]
    assert all(response.status_code == 404 for response in responses)
    assert len({response.text for response in responses}) == 1


def test_preview_failure_releases_download_pin(client, monkeypatch):
    record = publish(client)

    def fail(*args, **kwargs):
        raise OSError("private blob path")

    monkeypatch.setattr("junkyard.file_routes.inspect_preview", fail)
    response = client.get("/" + record.id, headers=HTML)
    assert response.status_code == 503
    assert "private blob path" not in response.text
    assert not client.app.state.files._pins
    assert client.app.state.files._transfers == 0


def test_client_content_type_never_enables_inline_content(client):
    body = b"<script>alert(1)</script>"
    response = client.put("/u/forged.png", content=body, headers={"Content-Type": "image/png"},
                          auth=("admin", CLI_PASSWORD))
    assert response.status_code == 201
    path = "/" + response.text.strip().rsplit("/", 1)[1]
    assert "No browser preview" in client.get(path, headers=HTML).text
    assert client.get(path + "/raw").headers["content-type"] == "application/octet-stream"


def test_viewer_authentication_errors_and_method_errors(client):
    response = client.get("/auth/check", headers=HTML)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["content-type"].startswith("text/html")
    response = client.post("/NoSuch", headers=HTML)
    assert response.status_code == 405
    assert response.headers["allow"]
    assert response.json()["detail"] == "Method Not Allowed"


def test_active_preview_survives_replacement(client, monkeypatch):
    record = publish(client, b"old text")
    store = client.app.state.files

    def replace_during_inspection(file, name, **kwargs):
        upload = store.begin(FileOptions(), record.id)
        try:
            store.write(upload, b"new text")
            store.finish(upload)
        finally:
            store.abort(upload)
        store.cleanup()
        return inspect_preview(file, name, **kwargs)

    monkeypatch.setattr("junkyard.file_routes.inspect_preview", replace_during_inspection)
    response = client.get("/" + record.id, headers=HTML)
    assert response.status_code == 200
    assert "old text" in response.text and "new text" not in response.text
    assert client.get("/" + record.id).content == b"new text"
    assert not store._pins and store._transfers == 0
