"""Opaque browser sessions, login challenges, and explicit CSRF validation."""

from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import Request

from junkyard.database import Database, StoreError
from junkyard.file_options import FileError
from junkyard.security import CLIAuthenticator, PASSWORD_HASHER, hash_password

SESSION_COOKIE = "__Host-junkyard-session"
LOGIN_COOKIE = "__Host-junkyard-login"
IDLE_SECONDS = 3600
ABSOLUTE_SECONDS = 12 * 3600
LOGIN_SECONDS = 600
MAX_SESSIONS = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_token(token: str) -> str:
    # A domain-separated token bound to this bearer secret, never the cookie
    # itself. A leaked CSRF token cannot be used as a session credential.
    return hmac.new(token.encode("ascii"), b"junkyard-csrf-v1", hashlib.sha256).hexdigest()


def cookie(request: Request, name: str) -> str | None:
    values = []
    for header in request.headers.getlist("cookie"):
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if key == name and separator:
                values.append(value)
    return values[0] if len(values) == 1 else None


def set_cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True, samesite="strict", path="/")


def clear_cookie(response, name: str) -> None:
    response.delete_cookie(name, secure=True, httponly=True, samesite="strict", path="/")


def check_origin(request: Request, public_url: str) -> None:
    origins = request.headers.getlist("origin")
    try:
        origin = urlsplit(origins[0]) if len(origins) == 1 else None
        target = urlsplit(public_url)
        valid = (origin is not None and origin.scheme == "https" and origin.hostname is not None
                 and origin.username is None and origin.password is None
                 and not origin.path and not origin.query and not origin.fragment
                 and not any(char.isspace() or ord(char) < 32 for char in origins[0])
                 and "?" not in origins[0] and "#" not in origins[0] and "\\" not in origins[0]
                 and (origin.hostname, 443 if origin.port is None else origin.port)
                 == (target.hostname, 443 if target.port is None else target.port))
    except ValueError:
        valid = False
    if not valid:
        raise FileError(403, "Invalid request origin")


def check_csrf(request: Request, expected: str) -> None:
    values = request.headers.getlist("x-csrf-token")
    if len(values) != 1 or not re.fullmatch(r"[0-9a-f]{64}", values[0]) or not secrets.compare_digest(values[0], expected):
        raise FileError(403, "Invalid CSRF token; reload the page")


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str
    credential_version: int
    expires_at: int


class BrowserAuth:
    def __init__(self, database: Database, authenticator: CLIAuthenticator, *, clock=time.time):
        self.database = database
        self.authenticator = authenticator
        self._clock = clock
        self._login_secret = secrets.token_bytes(32)

    def login_challenge(self) -> str:
        value = f"{secrets.token_urlsafe(32)}.{int(self._clock())}"
        signature = hmac.new(self._login_secret, value.encode("ascii"), hashlib.sha256).hexdigest()
        return value + "." + signature

    def check_login(self, request: Request) -> None:
        value = cookie(request, LOGIN_COOKIE)
        if value is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[0-9]{1,12}\.[0-9a-f]{64}", value):
            raise FileError(403, "Login page expired; reload it")
        unsigned, signature = value.rsplit(".", 1)
        expected = hmac.new(self._login_secret, unsigned.encode("ascii"), hashlib.sha256).hexdigest()
        age = int(self._clock()) - int(unsigned.rsplit(".", 1)[1])
        if not secrets.compare_digest(signature, expected) or not 0 <= age < LOGIN_SECONDS:
            raise FileError(403, "Login page expired; reload it")
        check_csrf(request, csrf_token(value))

    def _prune(self, connection, now: int) -> None:
        connection.execute("""DELETE FROM browser_sessions WHERE expires_at <= ? OR last_seen <= ?
            OR credential_version != (SELECT version FROM credentials WHERE kind = 'browser')""",
            (now, now - IDLE_SECONDS))

    def _valid(self, connection, digest: str, now: int):
        return connection.execute("""SELECT s.* FROM browser_sessions s JOIN credentials c
            ON c.kind = 'browser' AND c.version = s.credential_version
            WHERE s.token_hash = ? AND s.expires_at > ? AND s.last_seen > ?""",
            (digest, now, now - IDLE_SECONDS)).fetchone()

    def resolve(self, token: str | None, *, touch: bool = True) -> Session | None:
        if token is None or not TOKEN_PATTERN.fullmatch(token):
            return None
        digest = token_hash(token)
        now = int(self._clock())
        with self.database.connect() as connection, connection:
            # Invalid bearer guesses must not acquire SQLite's writer lock.
            if self._valid(connection, digest, now) is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            row = self._valid(connection, digest, now)
            if row is None:
                return None
            if touch:
                connection.execute("UPDATE browser_sessions SET last_seen = MAX(last_seen, ?) WHERE token_hash = ?", (now, digest))
        return Session(digest, csrf_token(token), row["credential_version"], row["expires_at"])

    def require_live(self, session: Session) -> None:
        with self.database.connect() as connection:
            if self._valid(connection, session.token_hash, int(self._clock())) is None:
                raise FileError(401, "Browser session expired; sign in again")

    def _browser_credential(self, password: str):
        with self.database.connect() as connection:
            row = connection.execute("SELECT password_hash, version FROM credentials WHERE kind = 'browser'").fetchone()
        if row is None:
            raise StoreError("browser credential is missing")
        try:
            PASSWORD_HASHER.verify(row["password_hash"], password)
        except VerifyMismatchError:
            raise FileError(401, "Invalid browser credentials") from None
        except (VerificationError, InvalidHashError):
            raise StoreError("browser credential is invalid; reset it locally") from None
        return row

    def login(self, password: str, previous: str | None = None) -> str:
        with self.authenticator.password_work():
            credential = self._browser_credential(password)
            token = secrets.token_urlsafe(32)
            now = int(self._clock())
            with self.database.connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("SELECT version FROM credentials WHERE kind = 'browser'").fetchone()[0]
                if version != credential["version"]:
                    raise FileError(401, "Invalid browser credentials")
                self._prune(connection, now)
                if previous and TOKEN_PATTERN.fullmatch(previous):
                    connection.execute("DELETE FROM browser_sessions WHERE token_hash = ?", (token_hash(previous),))
                # Bound persistent state, even after repeated successful logins.
                connection.execute("""DELETE FROM browser_sessions WHERE token_hash IN
                    (SELECT token_hash FROM browser_sessions ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)""",
                    (MAX_SESSIONS - 1,))
                connection.execute("INSERT INTO browser_sessions VALUES (?, ?, ?, ?, ?)",
                                   (token_hash(token), version, now, now, now + ABSOLUTE_SECONDS))
            return token

    def logout(self, session: Session) -> None:
        with self.database.connect() as connection, connection:
            connection.execute("DELETE FROM browser_sessions WHERE token_hash = ?", (session.token_hash,))

    def rotate_cli(self, session: Session, browser_password: str, new_password: str) -> None:
        with self.authenticator.password_work():
            credential = self._browser_credential(browser_password)
            if browser_password == new_password:
                raise FileError(400, "CLI and browser passwords must be different")
            with self.database.connect() as connection:
                cli_version = connection.execute("SELECT version FROM credentials WHERE kind = 'cli'").fetchone()[0]
            new_hash = hash_password(new_password)
            now = int(self._clock())
            with self.database.connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                live = self._valid(connection, session.token_hash, now)
                if live is None or live["credential_version"] != credential["version"]:
                    raise FileError(401, "Browser session expired; sign in again")
                result = connection.execute("""UPDATE credentials SET password_hash = ?, version = version + 1,
                    updated_at = ? WHERE kind = 'cli' AND version = ?""", (new_hash, now, cli_version))
                if result.rowcount != 1:
                    raise FileError(409, "CLI credential changed; retry the operation")
