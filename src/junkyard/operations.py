"""Request accounting without logging client-controlled strings or tracebacks."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import secrets
import time

from starlette.requests import Request

from junkyard.presentation import ASSETS, error_response

logger = logging.getLogger("junkyard.requests")
FILE_PATH = re.compile(r"/[A-Za-z0-9_-]{6}(?:[A-Za-z0-9_-]{26})?(?:/(raw|download))?")
METHODS = frozenset(("GET", "HEAD", "PUT", "PATCH", "DELETE", "POST", "OPTIONS"))


def operation(scope) -> str:
    """Return only fixed labels, including for failures before route matching."""
    path, method = scope["path"], scope["method"]
    if path in ASSETS:
        return "asset"
    if path == "/auth/check":
        return "cli_auth"
    if path in ("/admin", "/admin/"):
        return "admin_list"
    if path == "/admin/login":
        return "browser_login"
    if path == "/admin/logout":
        return "browser_logout"
    if path == "/admin/cli-password":
        return "cli_password_change"
    if method == "PUT" and (path in ("/u", "/admin/files") or path.startswith("/u/")):
        return "upload"
    file_path = path.removeprefix("/admin/files") if path.startswith("/admin/files/") else path
    if FILE_PATH.fullmatch(file_path):
        return {"GET": "read", "HEAD": "inspect", "PUT": "replace", "PATCH": "metadata", "DELETE": "delete"}.get(method, "unknown")
    return "unknown"


class RequestFailed(RuntimeError):
    """Safe replacement for an exception raised after streaming has started."""


class OperationLogging:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(16)  # Never reuse a client-supplied ID.
        started_at = time.monotonic()
        status = 500
        started = complete = False
        outcome = "complete"

        async def measured_send(message):
            nonlocal status, started, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
                started = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive, measured_send)
        except asyncio.CancelledError:
            outcome = "cancelled"
            if not started:
                status = 499
            raise
        except Exception:
            outcome = "failed"
            if started:
                # Do not let Uvicorn print a request-derived exception message.
                raise RequestFailed("Response failed after headers were sent") from None
            response = error_response(Request(scope), 500, "Internal server error")
            await response(scope, receive, measured_send)
        finally:
            if outcome == "complete" and not complete:
                outcome = "incomplete"
            logger.log(logging.ERROR if outcome == "failed" else logging.INFO, "request", extra={
                "operation_fields": {
                    "event": "request", "request_id": request_id, "operation": operation(scope),
                    "method": scope["method"] if scope["method"] in METHODS else "OTHER",
                    "status": status, "outcome": outcome,
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
                },
            })


class PrivateFormatter(logging.Formatter):
    """Allowlist operational fields; never format exception text or arguments."""
    def format(self, record):
        result = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  "level": record.levelname.lower()}
        if record.name == "junkyard.requests" and hasattr(record, "operation_fields"):
            result.update(record.operation_fields)
        else:
            # Existing cleanup messages are fixed strings. Unknown server errors
            # remain observable without trusting third-party exception content.
            event = {
                "Blob cleanup deferred; storage remains charged": "blob_cleanup_deferred",
                "File cleanup deferred; storage remains charged": "file_cleanup_deferred",
                "File cleanup failed; it will be retried at the next interval": "cleanup_failed",
                "Waiting for application startup.": "starting",
                "Application startup complete.": "ready",
                "Shutting down": "stopping",
                "Application shutdown complete.": "stopped",
            }.get(record.msg if isinstance(record.msg, str) else "", "server_event")
            result.update(event=event)
        return json.dumps(result, separators=(",", ":"), ensure_ascii=True)


LOG_CONFIG = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"private": {"()": "junkyard.operations.PrivateFormatter"}},
    "handlers": {"private": {"class": "logging.StreamHandler", "formatter": "private", "stream": "ext://sys.stderr"}},
    "loggers": {
        "junkyard": {"handlers": ["private"], "level": "INFO", "propagate": False},
        "uvicorn": {"handlers": ["private"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["private"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
    },
}
