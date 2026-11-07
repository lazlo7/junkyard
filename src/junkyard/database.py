"""Small, explicit SQLite operations; call these outside the async event loop."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from typing import Iterator, Literal

CredentialKind = Literal["cli", "browser"]
SCHEMA_VERSION = 3


def create_session_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE browser_sessions (
            token_hash TEXT PRIMARY KEY,
            credential_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    connection.execute("CREATE INDEX sessions_expiration ON browser_sessions(expires_at)")


def create_file_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE files (
            id TEXT PRIMARY KEY,
            blob TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size >= 0),
            created_at INTEGER NOT NULL,
            modified_at INTEGER NOT NULL,
            expires_at INTEGER,
            revision INTEGER NOT NULL CHECK (revision > 0)
        )
    """)
    connection.execute("CREATE INDEX files_expiration ON files(expires_at)")


class StoreError(RuntimeError):
    """A storage error with a safe operator-facing message."""


def _check_kind(kind: str) -> None:
    if kind not in ("cli", "browser"):
        raise StoreError("credential kind must be cli or browser")


class Database:
    def __init__(self, data_dir: Path):
        self.directory = data_dir
        self.path = data_dir / "metadata.sqlite3"

    def _reject_symlinks(self) -> None:
        # A private, owner-controlled directory is the SQLite pathname trust boundary.
        # Never resolve symlinks before checking them.
        for path in (*reversed(self.directory.parents), self.directory):
            if path.is_symlink():
                raise StoreError("data_dir and its parents must not be symlinks")

    def _check_directory(self) -> None:
        self._reject_symlinks()
        try:
            info = self.directory.lstat()
        except OSError:
            raise StoreError("data directory is missing; run junkyard init") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StoreError("data directory must be owned by this user with permissions 0700")

    def _check_file(self) -> None:
        self._check_directory()
        try:
            info = self.path.lstat()
        except OSError:
            raise StoreError("database is missing; run junkyard init") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise StoreError("database must be a private regular file owned by this user with permissions 0600 and no hard links")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._check_file()
        try:
            # mode=rw prevents accidental creation if the initialized file disappears.
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                if connection.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, SCHEMA_VERSION):
                    raise StoreError("unsupported database schema version")
                yield connection
            finally:
                connection.close()
        except sqlite3.Error:
            raise StoreError("database operation failed; check storage availability and integrity") from None

    def check_initialized(self) -> None:
        with self.connect() as connection:
            rows = connection.execute("SELECT kind FROM credentials").fetchall()
            if {row["kind"] for row in rows} != {"cli", "browser"}:
                raise StoreError("both CLI and browser credentials must be initialized")

    def require_file_schema(self) -> None:
        self.check_initialized()
        with self.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise StoreError("database upgrade required; stop the server and run junkyard migrate")

    def migrate(self) -> None:
        self.check_initialized()
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                create_file_schema(connection)
            if version < 3:
                create_session_schema(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def initialize(self, cli_hash: str, browser_hash: str) -> None:
        try:
            # Parent directories must already exist; do not silently create a tree.
            self._reject_symlinks()
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self._check_directory()
            if self.path.exists() or self.path.is_symlink():
                raise StoreError("database already exists; use junkyard passwd to reset a credential")
            descriptor, filename = tempfile.mkstemp(prefix=".init-", suffix=".sqlite3", dir=self.directory)
            os.close(descriptor)
            temporary = Path(filename)
            try:
                connection = sqlite3.connect(temporary)
                try:
                    with connection:
                        connection.execute("""
                            CREATE TABLE credentials (
                                kind TEXT PRIMARY KEY CHECK (kind IN ('cli', 'browser')),
                                password_hash TEXT NOT NULL,
                                version INTEGER NOT NULL CHECK (version > 0),
                                updated_at INTEGER NOT NULL
                            )
                        """)
                        connection.executemany(
                            "INSERT INTO credentials VALUES (?, ?, 1, ?)",
                            [("cli", cli_hash, int(time.time())), ("browser", browser_hash, int(time.time()))],
                        )
                        create_file_schema(connection)
                        create_session_schema(connection)
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                finally:
                    connection.close()
                # Atomic publication without overwriting a competing initializer.
                os.link(temporary, self.path)
            finally:
                temporary.unlink()
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, sqlite3.Error):
            raise StoreError("cannot initialize database; check permissions, free space, and whether it already exists") from None

    def password_hash(self, kind: CredentialKind) -> str:
        _check_kind(kind)
        with self.connect() as connection:
            row = connection.execute("SELECT password_hash FROM credentials WHERE kind = ?", (kind,)).fetchone()
            if row is None:
                raise StoreError("credential is not initialized")
            return row["password_hash"]

    def replace_password(self, kind: CredentialKind, password_hash: str) -> None:
        _check_kind(kind)
        with self.connect() as connection, connection:
            result = connection.execute(
                "UPDATE credentials SET password_hash = ?, version = version + 1, updated_at = ? WHERE kind = ?",
                (password_hash, int(time.time()), kind),
            )
            if result.rowcount != 1:
                raise StoreError("credential is not initialized")
            if kind == "browser" and connection.execute("PRAGMA user_version").fetchone()[0] >= 3:
                connection.execute("DELETE FROM browser_sessions")

