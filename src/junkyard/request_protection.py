"""Authentication and client-IP limits before routes consume request bodies."""

import ipaddress

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from junkyard.config import Settings
from junkyard.database import StoreError
from junkyard.rate_limits import RateLimited, RequestLimits
from junkyard.security import CHALLENGE, AuthenticationBusy, CLIAuthenticator, decode_basic


def normalized_ip(value: str):
    if "%" in value:  # Scoped IPv6 addresses are not external client identities.
        raise ValueError("scoped address")
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


class RequestProtection:
    def __init__(self, app, *, settings: Settings, authenticator: CLIAuthenticator, limits: RequestLimits):
        self.app = app
        self.authenticator = authenticator
        self.limits = limits
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
        response = JSONResponse({"detail": detail}, status_code=status,
                                headers={"Cache-Control": "no-store", **(headers or {})})
        if request.method == "HEAD":
            response = Response(status_code=status, headers=dict(response.headers))
        await response(request.scope, receive, send)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        state = scope.setdefault("state", {})
        state["cli_authenticated"] = False
        try:
            client_ip = self.client_ip(request)
        except ValueError:
            await self._respond(request, receive, send, 400, "Invalid proxy client address")
            return
        state["client_ip"] = client_ip
        required = request.method in ("PUT", "PATCH", "DELETE") or scope["path"] == "/auth/check"
        authorization = request.headers.getlist("authorization")
        try:
            if required or authorization:
                ticket = self.limits.authentication.take(client_ip, reserve=True)
                refund = True
                failure = None
                try:
                    credentials = decode_basic(authorization)
                    valid = credentials is not None and await run_in_threadpool(self.authenticator.verify, *credentials)
                    if valid:
                        state["cli_authenticated"] = True
                    else:
                        refund = False
                        failure = (401, "Invalid CLI credentials", CHALLENGE)
                except AuthenticationBusy:
                    failure = (503, "Authentication busy", {"Retry-After": "1"})
                except StoreError:
                    failure = (503, "Authentication unavailable", {})
                finally:
                    self.limits.authentication.settle(ticket, refund=refund)
                if failure is not None:
                    await self._respond(request, receive, send, *failure)
                    return
            if request.method in ("GET", "HEAD") and not state["cli_authenticated"]:
                # There are no static assets yet. All anonymous reads, including
                # nonexistent paths and HEAD probes, consume the same IP budget.
                self.limits.anonymous.take(client_ip)
        except RateLimited as error:
            await self._respond(request, receive, send, 429, "Too many requests",
                                {"Retry-After": str(error.retry_after)})
            return
        await self.app(scope, receive, send)
