from dataclasses import replace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from junkyard.app import create_app
from conftest import CLI_PASSWORD, representation_headers

CONTENT = b"0123456789abcdef"


@pytest.fixture
def client(settings, database):
    with TestClient(create_app(replace(settings, anonymous_burst=100))) as client:
        response = client.put("/u/example.bin", content=CONTENT, auth=("admin", CLI_PASSWORD))
        assert response.status_code == 201
        client.file_path = urlsplit(response.text.strip()).path
        yield client


@pytest.mark.parametrize("suffix", ["", "/raw", "/download"])
def test_head_matches_get_without_body_or_pin_leak(client, suffix):
    path = client.file_path + suffix
    full = client.get(path)
    head = client.head(path)
    assert head.status_code == full.status_code == 200
    assert head.content == b""
    assert representation_headers(head) == representation_headers(full)
    assert head.headers["accept-ranges"] == "bytes"
    assert not client.app.state.files._pins
    assert client.app.state.files._transfers == 0
    ranged_head = client.head(path, headers={"Range": "bytes=1-2"})
    assert ranged_head.status_code == 200
    assert ranged_head.headers["content-length"] == str(len(CONTENT))
    assert "content-range" not in ranged_head.headers


@pytest.mark.parametrize("value,expected,content_range", [
    ("bytes=0-0", b"0", "bytes 0-0/16"),
    ("bytes=3-7", b"34567", "bytes 3-7/16"),
    ("bytes=12-", b"cdef", "bytes 12-15/16"),
    ("bytes=-4", b"cdef", "bytes 12-15/16"),
    ("bytes=-100", CONTENT, "bytes 0-15/16"),
    ("bytes=12-100", b"cdef", "bytes 12-15/16"),
])
def test_single_byte_ranges(client, value, expected, content_range):
    response = client.get(client.file_path, headers={"Range": value})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["content-range"] == content_range
    assert response.headers["cache-control"] == "no-store"
    assert client.app.state.files._transfers == 0


@pytest.mark.parametrize("value", ["bytes=16-", "bytes=8-3", "bytes=-0", "bytes=", "bytes=-",
                                    "bytes=abc", "bytes=1.0-2", "bytes=" + "9" * 1000 + "-"])
def test_invalid_or_unsatisfiable_ranges(client, value):
    response = client.get(client.file_path, headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */16"
    assert not client.app.state.files._pins
    assert client.app.state.files._transfers == 0


def test_multiple_ranges_and_unknown_units_fall_back_to_full_response(client):
    for value in ("bytes=0-1,4-5", "items=0-2", "items=" + "9" * 1000):
        response = client.get(client.file_path, headers={"Range": value})
        assert response.status_code == 200
        assert response.content == CONTENT
        assert "content-range" not in response.headers
    duplicate = client.get(client.file_path, headers=[("Range", "bytes=0-1"), ("Range", "bytes=4-5")])
    assert duplicate.status_code == 416


def test_if_range_uses_strong_validator_even_for_same_second_replacement(client):
    original = client.get(client.file_path)
    etag = original.headers["etag"]
    matching = client.get(client.file_path, headers={"Range": "bytes=0-3", "If-Range": etag})
    assert matching.status_code == 206
    assert matching.content == b"0123"
    for validator in ('"other"', "W/" + etag, original.headers["last-modified"]):
        response = client.get(client.file_path, headers={"Range": "bytes=0-3", "If-Range": validator})
        assert response.status_code == 200
        assert response.content == CONTENT
    client.put(client.file_path, content=b"replacement", auth=("admin", CLI_PASSWORD))
    changed = client.get(client.file_path, headers={"Range": "bytes=0-3", "If-Range": etag})
    assert changed.status_code == 200
    assert changed.content == b"replacement"
    assert changed.headers["etag"] != etag


def test_zero_length_file_and_missing_head(client):
    response = client.put("/u/empty", content=b"", auth=("admin", CLI_PASSWORD))
    path = urlsplit(response.text.strip()).path
    assert client.head(path).headers["content-length"] == "0"
    response = client.get(path, headers={"Range": "bytes=0-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */0"
    assert client.head("/NoSuch").status_code == 404
    assert client.head("/NoSuch").content == b""
