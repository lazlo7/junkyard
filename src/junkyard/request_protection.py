"""Authentication and client-IP limits before routes consume request bodies."""

import ipaddress

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from junkyard.config import Settings
from junkyard.browser_auth import BrowserAuth, SESSION_COOKIE, cookie
from junkyard.database import StoreError
from junkyard.file_options import FILE_ID_PATTERN
from junkyard.presentation import ASSETS, error_response
from junkyard.rate_limits import RateLimited, RequestLimits
from junkyard.security import (
    CHALLENGE, FILE_CHALLENGE, TOKEN_PATTERN, AuthenticationBusy, CLIAuthenticator, decode_basic, token_hash,
)


def normalized_ip(value: str):
    if "%" in value:  # Scoped IPv6 addresses are not external client identities.
        raise ValueError("scoped address")
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


class RequestProtection:
    def __init__(self, app, *, settings: Settings, authenticator: CLIAuthenticator, limits: RequestLimits,
                 browser_auth: BrowserAuth):
        self.app = app
        self.authenticator = authenticator
        self.limits = limits
        self.browser_auth = browser_auth
        self.trusted = tuple(ipaddress.ip_network(value) for value in settings.trusted_proxies)

    def client_ip(self, request: Request) -> str:
        try:
            peer = normalized_ip(request.client.host) if request.client else None
        except ValueError:
            peer = None
        if peer is None:
            return "unknown"
        if any(peer in network for network in self.trusted):
            forwarded = request.headers.getlist("x-forwarded-for")
            # The single reverse proxy must overwrite this header with exactly one
            # IP. Never trust arbitrary chains or client-supplied leftmost values.
            if len(forwarded) != 1:
                raise ValueError("missing or duplicate forwarded address")
            return str(normalized_ip(forwarded[0].strip()))
        return str(peer)

    async def _respond(self, request: Request, receive, send, status: int, detail: str, headers=None) -> None:
        response = error_response(request, status, detail, headers)
        await response(request.scope, receive, send)

    async def _authenticate(self, request: Request, authorization: list[str]) -> tuple[int, str, dict] | None:
        file_id = request.scope["path"].removeprefix("/")
        if request.method == "PUT" and FILE_ID_PATTERN.fullmatch(file_id) and len(authorization) == 1:
            scheme, _, token = authorization[0].partition(" ")
            if scheme.lower() == "bearer":
                if TOKEN_PATTERN.fullmatch(token):
                    digest = token_hash(token)
                    if await run_in_threadpool(request.app.state.files.verify_file_token, file_id, digest):
                        request.state.file_token_digest = digest
                        return None
                return (401, "Invalid file token", FILE_CHALLENGE)
        credentials = decode_basic(authorization)
        if credentials is not None and await run_in_threadpool(self.authenticator.verify, *credentials):
            request.state.cli_authenticated = True
            return None
        return (401, "Invalid CLI credentials", CHALLENGE)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        state = scope.setdefault("state", {})
        state["cli_authenticated"] = False
        state["file_token_digest"] = None
        state["browser_session"] = None
        try:
            client_ip = self.client_ip(request)
        except ValueError:
            await self._respond(request, receive, send, 400, "Invalid proxy client address")
            return
        state["client_ip"] = client_ip
        admin = scope["path"] == "/admin" or scope["path"].startswith("/admin/")
        required = not admin and (request.method in ("PUT", "PATCH", "DELETE") or scope["path"] == "/auth/check")
        authorization = request.headers.getlist("authorization")
        if admin and authorization:
            await self._respond(request, receive, send, 401, "Use the browser login for administration")
            return
        try:
            if required or authorization:
                ticket = self.limits.authentication.take(client_ip, reserve=True)
                refund = True
                failure = None
                try:
                    failure = await self._authenticate(request, authorization)
                    refund = failure is None
                except AuthenticationBusy:
                    failure = (503, "Authentication busy", {"Retry-After": "1"})
                except StoreError:
                    failure = (503, "Authentication unavailable", {})
                finally:
                    self.limits.authentication.settle(ticket, refund=refund)
                if failure is not None:
                    await self._respond(request, receive, send, *failure)
                    return
            if (admin or request.method in ("GET", "HEAD")) and scope["path"] not in ASSETS and not state["cli_authenticated"]:
                try:
                    state["browser_session"] = await run_in_threadpool(self.browser_auth.resolve, cookie(request, SESSION_COOKIE))
                except StoreError:
                    await self._respond(request, receive, send, 503, "Authentication unavailable", {"Retry-After": "1"})
                    return
            if (request.method in ("GET", "HEAD") and not state["cli_authenticated"]
                and state["browser_session"] is None and scope["path"] not in ASSETS):
                # Only fixed application assets are exempt; arbitrary paths and
                # file/media requests continue consuming the same IP budget.
                self.limits.anonymous.take(client_ip)
        except RateLimited as error:
            await self._respond(request, receive, send, 429, "Too many requests",
                                {"Retry-After": str(error.retry_after)})
            return
        await self.app(scope, receive, send)
