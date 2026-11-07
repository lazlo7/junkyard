"""HTML negotiation, escaped pages, and a fixed set of packaged assets."""

from datetime import datetime, timezone
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from junkyard.http_files import PRIVATE_HEADERS

PAGE_HEADERS = {
    **PRIVATE_HEADERS,
    "Vary": "Accept",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; media-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
_environment = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                           autoescape=True, undefined=StrictUndefined, auto_reload=False)
# Compile fixed templates once, keeping filesystem I/O out of request handling.
_templates = {name: _environment.get_template(name + ".html") for name in ("viewer", "error")}
ASSETS = {
    "/assets/viewer.css": ((Path(__file__).parent / "static/viewer.css").read_bytes(), "text/css"),
    "/assets/viewer.js": ((Path(__file__).parent / "static/viewer.js").read_bytes(), "text/javascript"),
}


def wants_html(request: Request) -> bool:
    """Require explicit HTML acceptance; wildcards alone keep CLI byte behavior.

    Compare HTML with the octet-stream representation using the most specific
    matching range. JSON/plain-text preferences also keep errors headless.
    """
    accepted = {}
    for item in ",".join(request.headers.getlist("accept")).split(","):
        parts = [part.strip().lower() for part in item.split(";")]
        qualities = [part[2:] for part in parts[1:] if part.startswith("q=")]
        quality = qualities[0] if len(qualities) == 1 else "1" if not qualities else "invalid"
        if not re.fullmatch(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", quality):
            continue
        # Unknown media parameters cannot describe our representation.
        if any(not part.startswith("q=") and part not in ("charset=utf-8", 'charset="utf-8"') for part in parts[1:]):
            continue
        accepted[parts[0]] = max(accepted.get(parts[0], 0), float(quality))
    html = accepted.get("text/html", 0)
    binary = accepted.get("application/octet-stream", accepted.get("application/*", accepted.get("*/*", 0)))
    return html > 0 and html >= max(binary, accepted.get("application/json", 0), accepted.get("text/plain", 0))


def without_head_body(request: Request, response: Response) -> Response:
    if request.method == "HEAD":
        return Response(status_code=response.status_code, headers=dict(response.headers))
    return response


def error_response(request: Request, status: int, detail: str, headers=None) -> Response:
    headers = {**PAGE_HEADERS, **(headers or {})}
    if request.method in ("GET", "HEAD") and not request.url.path.endswith(("/raw", "/download")) and wants_html(request):
        body = _templates["error"].render(status=status, detail=detail, retry_after=headers.get("Retry-After"))
        response = HTMLResponse(body, status_code=status, headers=headers)
    else:
        response = JSONResponse({"detail": detail}, status_code=status, headers=headers)
    return without_head_body(request, response)


def timestamp(value: int | None) -> str:
    if value is None:
        return "Never"
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        # Explicit lifetimes can exceed the platform's calendar range.
        return f"Unix timestamp {value}"


def viewer_response(request: Request, record, preview, public_url: str) -> Response:
    body = _templates["viewer"].render(
        file=record, preview=preview, share_url=public_url + "/" + record.id,
        created=timestamp(record.created_at), modified=timestamp(record.modified_at),
        expires=timestamp(record.expires_at),
    )
    return without_head_body(request, HTMLResponse(body, headers=PAGE_HEADERS))


def install_assets(app) -> None:
    async def asset(request: Request):
        body, media_type = ASSETS[request.url.path]
        return without_head_body(request, Response(body, media_type=media_type, headers=PRIVATE_HEADERS))

    for path in ASSETS:
        app.add_api_route(path, asset, methods=["GET", "HEAD"], include_in_schema=False)
