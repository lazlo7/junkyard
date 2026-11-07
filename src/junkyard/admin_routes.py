"""Browser-only routes; session/origin/CSRF checks precede all body reads."""

from contextlib import asynccontextmanager
import json
import re

from fastapi import Depends, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse

from junkyard.browser_auth import (
    ABSOLUTE_SECONDS, LOGIN_COOKIE, LOGIN_SECONDS, SESSION_COOKIE,
    check_csrf, check_origin, clear_cookie, cookie, csrf_token, set_cookie,
)
from junkyard.database import StoreError
from junkyard.file_options import FileError, parse_options, validate_encoding
from junkyard.file_routes import expected_length, mutation_response, upload_file
from junkyard.http_files import PRIVATE_HEADERS
from junkyard.presentation import admin_response
from junkyard.rate_limits import RateLimited
from junkyard.security import AuthenticationBusy, PasswordError


def require_browser(request: Request):
    session = request.state.browser_session
    if session is None:
        raise FileError(401, "Browser session expired; sign in again")
    check_origin(request, request.app.state.files.settings.public_url)
    check_csrf(request, session.csrf)
    return session


def no_body(request: Request) -> None:
    if expected_length(request) not in (None, 0) or "transfer-encoding" in request.headers:
        raise FileError(400, "This operation does not accept a request body")


async def password_fields(request: Request, names: set[str]) -> dict[str, str]:
    if request.scope["query_string"]:
        raise FileError(400, "Passwords must be sent in the request body")
    types = request.headers.getlist("content-type")
    if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise FileError(415, "Expected a JSON request")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise FileError(415, "Encoded bodies are not supported")
    length = expected_length(request)
    if length is not None and length > 8192:
        raise FileError(413, "Authentication request is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise FileError(413, "Authentication request is too large")
        body.extend(chunk)
    if length is not None and length != len(body):
        raise FileError(400, "Invalid request length")

    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate fields")
        return result

    try:
        values = json.loads(body.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise FileError(400, "Invalid authentication request") from None
    if not isinstance(values, dict) or set(values) != names:
        raise FileError(400, "Invalid authentication fields")
    for value in values.values():
        if (not isinstance(value, str) or not value.isprintable() or len(value) > 1024
            or len(value.encode("utf-8")) > 1024):
            raise FileError(400, "Passwords must be printable and at most 1024 UTF-8 bytes")
    return values


@asynccontextmanager
async def credential_attempt(request: Request):
    limits = request.app.state.limits.authentication
    try:
        ticket = limits.take(request.state.client_ip, reserve=True)
    except RateLimited as error:
        raise FileError(429, "Too many requests", {"Retry-After": str(error.retry_after)}) from None
    refund = False
    try:
        yield
        refund = True
    except AuthenticationBusy:
        refund = True
        raise FileError(503, "Authentication busy", {"Retry-After": "1"}) from None
    except StoreError:
        refund = True
        raise FileError(503, "Authentication unavailable", {"Retry-After": "1"}) from None
    except PasswordError as error:
        raise FileError(400, str(error)) from None
    finally:
        limits.settle(ticket, refund=refund)


def install_admin_routes(app) -> None:
    @app.api_route("/admin", methods=["GET", "HEAD"])
    async def admin_redirect():
        return RedirectResponse("/admin/", status_code=303, headers=PRIVATE_HEADERS)

    @app.api_route("/admin/login", methods=["GET", "HEAD"])
    async def login_page(request: Request):
        if request.state.browser_session is not None:
            return RedirectResponse("/admin/", status_code=303, headers=PRIVATE_HEADERS)
        challenge = request.app.state.browser_auth.login_challenge()
        response = admin_response(request, "login", csrf=csrf_token(challenge))
        set_cookie(response, LOGIN_COOKIE, challenge, LOGIN_SECONDS)
        return response

    @app.post("/admin/login")
    async def login(request: Request):
        auth = request.app.state.browser_auth
        check_origin(request, request.app.state.files.settings.public_url)
        auth.check_login(request)
        async with credential_attempt(request):
            fields = await password_fields(request, {"password"})
            token = await run_in_threadpool(auth.login, fields["password"], cookie(request, SESSION_COOKIE))
        response = JSONResponse({"redirect": "/admin/"}, headers=PRIVATE_HEADERS)
        set_cookie(response, SESSION_COOKIE, token, ABSOLUTE_SECONDS)
        clear_cookie(response, LOGIN_COOKIE)
        return response

    @app.api_route("/admin/", methods=["GET", "HEAD"])
    async def dashboard(request: Request):
        if request.state.browser_session is None:
            return RedirectResponse("/admin/login", status_code=303, headers=PRIVATE_HEADERS)
        validate_encoding(request.scope["query_string"])
        params = list(request.query_params.multi_items())
        if params and (len(params) != 1 or params[0][0] != "page" or not re.fullmatch(r"[1-9][0-9]{0,5}", params[0][1])):
            raise FileError(400, "Invalid page number")
        page = int(params[0][1]) if params else 1
        snapshot = await run_in_threadpool(request.app.state.files.list_files, page)
        return await run_in_threadpool(admin_response, request, "admin", csrf=request.state.browser_session.csrf,
                                      max_file_bytes=request.app.state.files.settings.max_file_bytes, **snapshot)

    @app.post("/admin/logout")
    async def logout(request: Request, session=Depends(require_browser)):
        no_body(request)
        if request.scope["query_string"]:
            raise FileError(400, "Logout does not accept parameters")
        await run_in_threadpool(request.app.state.browser_auth.logout, session)
        response = Response(status_code=204, headers=PRIVATE_HEADERS)
        clear_cookie(response, SESSION_COOKIE)
        clear_cookie(response, LOGIN_COOKIE)
        return response

    @app.put("/admin/files", dependencies=[Depends(require_browser)])
    async def upload(request: Request):
        return await upload_file(request)

    @app.put("/admin/files/{file_id}", dependencies=[Depends(require_browser)])
    async def replace_file(request: Request, file_id: str):
        return await upload_file(request, file_id=file_id)

    @app.patch("/admin/files/{file_id}", dependencies=[Depends(require_browser)])
    async def edit_file(request: Request, file_id: str):
        no_body(request)
        validate_encoding(request.scope["raw_path"])
        options = parse_options(request.scope["query_string"], creating=False)
        record = await run_in_threadpool(request.app.state.files.patch, file_id, options)
        return mutation_response(request, record, 200)

    @app.delete("/admin/files/{file_id}", dependencies=[Depends(require_browser)])
    async def delete_file(request: Request, file_id: str):
        no_body(request)
        validate_encoding(request.scope["raw_path"])
        if request.scope["query_string"]:
            raise FileError(400, "Delete does not accept parameters")
        await run_in_threadpool(request.app.state.files.delete, file_id)
        return Response(status_code=204, headers=PRIVATE_HEADERS)

    @app.post("/admin/cli-password")
    async def rotate_cli(request: Request, session=Depends(require_browser)):
        async with credential_attempt(request):
            fields = await password_fields(request, {"browser_password", "new_password", "confirmation"})
            if fields["new_password"] != fields["confirmation"]:
                raise FileError(400, "Passwords do not match")
            await run_in_threadpool(request.app.state.browser_auth.rotate_cli, session,
                                   fields["browser_password"], fields["new_password"])
        return Response(status_code=204, headers=PRIVATE_HEADERS)
