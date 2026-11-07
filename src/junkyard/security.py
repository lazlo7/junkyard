"""Password hashing and strict HTTP Basic decoding; never log credentials."""

import base64
import binascii
import secrets
import threading

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request

from junkyard.database import Database, StoreError

MAX_PASSWORD_BYTES = 1024
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID)
CHALLENGE = {"WWW-Authenticate": 'Basic realm="junkyard-cli", charset="UTF-8"', "Cache-Control": "no-store"}


def require_cli(request: Request) -> None:
    if not getattr(request.state, "cli_authenticated", False):
        raise HTTPException(status_code=401, detail="Invalid CLI credentials", headers=CHALLENGE)


class PasswordError(ValueError):
    pass


class AuthenticationBusy(RuntimeError):
    pass


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordError("password must contain at least 12 characters and at most 1024 UTF-8 bytes")
    if not password.isprintable() or not password.strip():
        raise PasswordError("password must contain printable characters and must not be all spaces")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def decode_basic(headers: list[str]) -> tuple[str, str] | None:
    if len(headers) != 1 or len(headers[0]) > 4096:
        return None
    scheme, separator, encoded = headers[0].partition(" ")
    if not separator or scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES or not password.isprintable():
            return None
        return username, password
    except (ValueError, binascii.Error, UnicodeError):
        return None


class CLIAuthenticator:
    def __init__(self, database: Database):
        self.database = database
        # Bound the memory cost of simultaneous Argon2 verifications (~128 MiB).
        self._slots = threading.BoundedSemaphore(2)

    def verify(self, username: str, password: str) -> bool:
        if not self._slots.acquire(blocking=False):
            raise AuthenticationBusy
        try:
            stored_hash = self.database.password_hash("cli")
            try:
                correct_password = PASSWORD_HASHER.verify(stored_hash, password)
            except VerifyMismatchError:
                return False
            except (VerificationError, InvalidHashError):
                raise StoreError("CLI credential hash is invalid; reset it locally") from None
            correct_username = secrets.compare_digest(username.encode("utf-8"), b"admin")
            # A reset that committed during verification invalidates the old hash.
            return correct_password and correct_username and secrets.compare_digest(
                stored_hash, self.database.password_hash("cli")
            )
        finally:
            self._slots.release()

