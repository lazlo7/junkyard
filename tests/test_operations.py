import asyncio
import json
import logging
import re

from fastapi.testclient import TestClient
import pytest

from junkyard.app import create_app
from junkyard.operations import OperationLogging, PrivateFormatter, RequestFailed
from conftest import CLI_PASSWORD


def request_records(caplog):
    return [json.loads(PrivateFormatter().format(record)) for record in caplog.records if record.name == "junkyard.requests"]


def test_safe_logging_on_success_auth_failure_throttle_and_bad_method(settings, database, caplog):
    caplog.set_level(logging.INFO, logger="junkyard.requests")
    with TestClient(create_app(settings)) as client:
        response = client.put("/u/private-filename?name=private-query", content=b"private-body", auth=("admin", CLI_PASSWORD),
                              headers={"X-Request-ID": "private-id", "Cookie": "secret=private-cookie"})
        assert response.status_code == 201
        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)
        denied = client.put("/u/private-filename", content=b"private-body")
        assert denied.status_code == 401
        assert denied.headers["x-request-id"] != request_id
        client.request("SECRET_METHOD", "/unknown-private-path")
        for _ in range(11):
            client.get("/unknown-private-path")
    records = request_records(caplog)
    assert records[0]["operation"] == "upload" and records[0]["status"] == 201
    assert records[1]["operation"] == "upload" and records[1]["status"] == 401
    assert records[2]["method"] == "OTHER"
    assert records[-1]["status"] == 429
    assert all(row["duration_ms"] >= 0 for row in records)
    log = json.dumps(records)
    for secret in ("private-", "SECRET_METHOD", CLI_PASSWORD, response.text.strip()):
        assert secret not in log


def test_unhandled_exception_is_generic_and_correlated(settings, database, caplog):
    caplog.set_level(logging.INFO, logger="junkyard.requests")
    app = create_app(settings)

    @app.get("/internal/failure")
    def failure():
        raise RuntimeError("private-filename password=secret-cookie https://private-link.test/")

    with TestClient(app) as client:
        response = client.get("/internal/failure")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    record = request_records(caplog)[0]
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["outcome"] == "failed"
    assert "private-" not in caplog.text


def test_formatter_never_formats_third_party_error_arguments_or_tracebacks():
    try:
        raise RuntimeError("private-exception-data")
    except RuntimeError:
        import sys
        record = logging.LogRecord("uvicorn.error", logging.ERROR, "private-file", 1,
                                   "request failed: %s", ("private-url",), sys.exc_info())
    result = json.loads(PrivateFormatter().format(record))
    assert result["event"] == "server_event"
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("cancelled", [False, True])
def test_stream_failure_is_counted_without_buffering_or_revealing_exception(caplog, cancelled):
    caplog.set_level(logging.INFO, logger="junkyard.requests")

    async def run():
        messages = []

        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"part", "more_body": True})
            raise asyncio.CancelledError if cancelled else RuntimeError("private-stream-error")

        async def send(message):
            messages.append(message)

        with pytest.raises(asyncio.CancelledError if cancelled else RequestFailed):
            await OperationLogging(application)({"type": "http", "path": "/AbCd_1", "method": "GET"}, None, send)
        assert messages[1]["body"] == b"part"

    asyncio.run(run())
    record = request_records(caplog)[0]
    assert record["outcome"] == ("cancelled" if cancelled else "failed")
    assert record["status"] == 200
    assert "private-stream-error" not in caplog.text


def test_no_automatic_http_redirects_behind_tls_proxy(settings, database):
    with TestClient(create_app(settings), follow_redirects=False) as client:
        for path in ("/admin/login/", "/auth/check/", "/NoSuch/"):
            response = client.get(path)
            assert response.status_code == 404
            assert "location" not in response.headers
        assert client.get("/admin").headers["location"] == "/admin/"
