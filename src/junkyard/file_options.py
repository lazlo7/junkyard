"""Validation for file identifiers, display names, and terse curl parameters."""

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, unquote_to_bytes


class FileError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        self.status = status
        self.detail = detail
        self.headers = headers or {}
        super().__init__(detail)


def validate_id(value: str) -> None:
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]{6}|[A-Za-z0-9_-]{32})", value):
        raise FileError(404, "File not found")


def validate_name(value: str) -> str:
    if (
        not value or value in (".", "..") or not value.isprintable()
        or "/" in value or "\\" in value or len(value.encode("utf-8")) > 255
    ):
        raise FileError(400, "Filename must be a printable basename of at most 255 UTF-8 bytes")
    return value


def validate_encoding(raw: bytes) -> None:
    if re.search(rb"%(?![0-9a-fA-F]{2})", raw):
        raise FileError(400, "Invalid URL encoding")
    try:
        unquote_to_bytes(raw).decode("utf-8", errors="strict")
    except UnicodeError:
        raise FileError(400, "Invalid URL encoding") from None


@dataclass(frozen=True)
class FileOptions:
    name: str | None = None
    lifetime: int | None = None  # None preserves/defaults; zero means permanent.
    secure: bool = False


def parse_options(raw: bytes, *, creating: bool) -> FileOptions:
    if len(raw) > 8192:
        raise FileError(400, "Upload parameters are too long")
    validate_encoding(raw)
    try:
        pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, errors="strict")
    except UnicodeError:
        raise FileError(400, "Invalid URL encoding") from None
    values = dict(pairs)
    allowed = {"name", "e", "s"} if creating else {"name", "e"}
    if len(values) != len(pairs) or set(values) - allowed:
        raise FileError(400, "Unknown or repeated file parameter")
    name = validate_name(values["name"]) if "name" in values else None
    if "s" in values and values["s"] != "1":
        raise FileError(400, "s must be 1 when requesting a long link")
    lifetime = None
    if "e" in values:
        expiration = values["e"]
        if expiration == "0":
            lifetime = 0
        elif match := re.fullmatch(r"([1-9][0-9]{0,11})([hd])", expiration):
            lifetime = int(match[1]) * (3600 if match[2] == "h" else 86400)
        else:
            raise FileError(400, "e must be 0 or a positive duration such as 7d or 12h")
    return FileOptions(name=name, lifetime=lifetime, secure="s" in values)


def expiration_at(now: int, lifetime: int) -> int | None:
    if lifetime == 0:
        return None
    expires = now + lifetime
    if expires > 253402300799:  # Last representable UTC second in year 9999.
        raise FileError(400, "Expiration exceeds the supported timestamp range")
    return expires
