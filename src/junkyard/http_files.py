"""HTTP metadata and bounded single-byte-range selection (RFC 9110)."""

from email.utils import formatdate
import hashlib
import re
from urllib.parse import quote

from starlette.requests import Request

from junkyard.file_options import FileError
from junkyard.files import FileRecord

PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def download_headers(record: FileRecord) -> dict[str, str]:
    fallback = record.name if record.name.isascii() else "download"
    fallback = fallback.replace('"', '\\"')
    # Immutable blob identity plus metadata revision makes a strong validator
    # without exposing the private storage filename or reading the entire file.
    identity = f"{record.blob}:{record.revision}".encode("ascii")
    return {
        **PRIVATE_HEADERS,
        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        "Content-Length": str(record.size),
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(record.name, safe="")}',
        "Accept-Ranges": "bytes",
        "ETag": '"' + hashlib.sha256(identity).hexdigest() + '"',
        "Last-Modified": formatdate(record.modified_at, usegmt=True),
    }


def select_range(request: Request, size: int, etag: str) -> tuple[int, int, int]:
    """Return start, byte count, and status. Unsupported multi-ranges get 200."""
    complete = (0, size, 200)
    ranges = request.headers.getlist("range")
    if request.method != "GET" or not ranges:
        return complete
    validators = request.headers.getlist("if-range")
    if validators and (len(validators) != 1 or validators[0] != etag):
        # Dates have only second precision; replacements can occur within one
        # second, so our Last-Modified value is not a strong If-Range validator.
        return complete

    def unsatisfiable():
        raise FileError(416, "Invalid or unsatisfiable byte range",
                        {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})

    if len(ranges) != 1:
        unsatisfiable()
    unit, separator, value = ranges[0].partition("=")
    if unit.lower() != "bytes":
        return complete
    if not separator or len(ranges[0]) > 256:
        unsatisfiable()
    if "," in value:
        return complete
    match = re.fullmatch(r"([0-9]{0,19})-([0-9]{0,19})", value.strip())
    if match is None or not any(match.groups()) or size == 0:
        unsatisfiable()
    first, last = match.groups()
    if not first:
        length = min(int(last), size)
        if length == 0:
            unsatisfiable()
        return size - length, length, 206
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        unsatisfiable()
    return start, end - start + 1, 206
