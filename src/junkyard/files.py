"""Single-process file operations with immutable blobs and explicit publication.

All methods perform synchronous I/O and must run outside the async event loop.
A short process-local lock coordinates quota, revisions, and open downloads.
The filesystem lock prevents another worker from bypassing that coordination.
"""

from dataclasses import dataclass, replace
import fcntl
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import BinaryIO, Callable

from junkyard.config import Settings
from junkyard.database import Database, StoreError
from junkyard.file_options import FileError, FileOptions, expiration_at, validate_id, validate_name

CHUNK_SIZE = 64 * 1024
ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRecord:
    id: str
    blob: str
    name: str
    size: int
    created_at: int
    modified_at: int
    expires_at: int | None
    revision: int


@dataclass
class Upload:
    file: BinaryIO
    path: Path
    blob: str
    options: FileOptions
    previous: FileRecord | None
    size: int = 0
    published: bool = False
    released: bool = False


@dataclass
class Download:
    file: BinaryIO
    record: FileRecord
    released: bool = False


def _check_regular(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
    ):
        raise StoreError("storage entries must be private regular files with permissions 0600 and no hard links")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileStore:
    def __init__(self, database: Database, settings: Settings, *, clock: Callable[[], float] = time.time):
        self.database = database
        self.settings = settings
        self._clock = clock
        self.blobs = database.directory / "blobs"
        self.staging = database.directory / "staging"
        self._lock = threading.RLock()
        self._process_lock: int | None = None
        self._pins: dict[str, int] = {}
        self._pending: set[str] = set()
        self._uploads: dict[str, Upload] = {}
        self._transfers = 0
        self.used_bytes = 0

    def start(self) -> None:
        self.database.require_file_schema()
        descriptor = os.open(
            self.database.directory / ".files.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
        )
        try:
            _check_regular(os.fstat(descriptor))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("file storage is already in use; run only one server worker") from None
            for directory in (self.blobs, self.staging):
                if directory.is_symlink():
                    raise StoreError("storage directories must not be symlinks")
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise StoreError("storage directories must be private with permissions 0700")
            # Persist newly created directory entries before any committed file
            # can depend on them surviving a power loss.
            _sync_directory(self.database.directory)
            # Reconcile only while holding the exclusive process lock. Validation
            # precedes all deletion so a damaged reference cannot be silently lost.
            self.cleanup()
            self._process_lock = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self._process_lock is not None:
            os.close(self._process_lock)
            self._process_lock = None

    def _inventory(self, directory: Path) -> dict[str, int]:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StoreError("storage directories must be private with permissions 0700")
        entries = {}
        for path in directory.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", path.name):
                raise StoreError("unexpected entry in managed file storage")
            info = path.lstat()
            _check_regular(info)
            entries[path.name] = info.st_size
        return entries

    def cleanup(self) -> None:
        """Validate, expire metadata, and reclaim unreferenced content atomically
        relative to uploads and downloads. Failed unlinks stay charged for retry.
        """
        with self._lock:
            blobs = self._inventory(self.blobs)
            staging = self._inventory(self.staging)
            with self.database.connect() as connection:
                records = connection.execute("SELECT blob, size FROM files").fetchall()
            for record in records:
                self._blob_path(record["blob"])
                if blobs.get(record["blob"]) != record["size"]:
                    raise StoreError("referenced blob is missing or has the wrong size; restore storage before serving")
            # No filesystem changes or metadata deletion occur before validation.
            with self.database.connect() as connection, connection:
                connection.execute("DELETE FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?", (int(self._clock()),))
                referenced = {row[0] for row in connection.execute("SELECT blob FROM files")}
            active_blobs = {upload.blob for upload in self._uploads.values() if upload.published}
            protected = referenced | set(self._pins) | active_blobs
            self._pending.difference_update(referenced)
            self._pending.update(set(blobs) - referenced - active_blobs)
            # Preserve reservations after a partial write until its upload exits.
            reserved = sum(max(0, upload.size - staging.get(upload.blob, 0))
                           for upload in self._uploads.values() if not upload.published)
            self.used_bytes = sum(blobs.values()) + sum(staging.values()) + reserved
            for directory, entries, keep in (
                (self.blobs, blobs, protected),
                (self.staging, staging, set(self._uploads)),
            ):
                changed = False
                for name, size in entries.items():
                    if name in keep:
                        continue
                    try:
                        (directory / name).unlink()
                    except OSError:
                        logger.warning("File cleanup deferred; storage remains charged")
                    else:
                        self.used_bytes -= size
                        if directory == self.blobs:
                            self._pending.discard(name)
                        changed = True
                if changed:
                    _sync_directory(directory)

    def _take_slot(self) -> None:
        if self._transfers >= self.settings.max_transfers:
            raise FileError(503, "Transfer capacity reached; retry later")
        self._transfers += 1

    def list_files(self, page: int, page_size: int = 25) -> dict:
        """A bounded, consistent snapshot using the same quota lock as transfers."""
        with self._lock, self.database.connect() as connection:
            now = int(self._clock())
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM files WHERE expires_at IS NULL OR expires_at > ?", (now,)).fetchone()[0]
            rows = connection.execute("""SELECT * FROM files WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""", (now, page_size, (page - 1) * page_size)).fetchall()
            return {"files": [FileRecord(**dict(row)) for row in rows], "page": page,
                    "pages": max(1, (total + page_size - 1) // page_size), "total": total,
                    "used_bytes": self.used_bytes, "quota_bytes": self.settings.storage_quota_bytes}

    def _blob_path(self, blob: str) -> Path:
        if not isinstance(blob, str) or not re.fullmatch(r"[0-9a-f]{32}", blob):
            raise StoreError("invalid blob reference in metadata")
        return self.blobs / blob

    def _find(self, file_id: str) -> FileRecord:
        validate_id(file_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None or (row["expires_at"] is not None and row["expires_at"] <= int(self._clock())):
            raise FileError(404, "File not found")
        return FileRecord(**dict(row))

    def begin(self, options: FileOptions, file_id: str | None = None) -> Upload:
        if options.name is not None:
            validate_name(options.name)
        if options.lifetime is not None:
            expiration_at(int(self._clock()), options.lifetime)
        with self._lock:
            previous = self._find(file_id) if file_id is not None else None
            self._take_slot()
            try:
                for _ in range(16):
                    blob = secrets.token_hex(16)
                    path = self.staging / blob
                    if self._blob_path(blob).exists():
                        continue
                    try:
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    except FileExistsError:
                        continue
                    upload = Upload(os.fdopen(descriptor, "wb", buffering=0), path, blob, options, previous)
                    self._uploads[blob] = upload
                    return upload
                raise FileError(503, "Could not allocate upload storage")
            except BaseException:
                self._transfers -= 1
                raise

    def write(self, upload: Upload, chunk: bytes | memoryview) -> None:
        with self._lock:
            if upload.released or upload.published:
                raise StoreError("upload is no longer writable")
            if upload.size + len(chunk) > self.settings.max_file_bytes:
                raise FileError(413, "File exceeds the configured size limit")
            if self.used_bytes + len(chunk) > self.settings.storage_quota_bytes:
                raise FileError(507, "Storage quota exceeded")
            # Reserve before writing; partial/failed writes remain charged until abort.
            upload.size += len(chunk)
            self.used_bytes += len(chunk)
            view = memoryview(chunk)
            while view:
                written = upload.file.write(view)
                if not written:
                    raise OSError("short write to upload storage")
                view = view[written:]

    def _new_id(self, secure: bool) -> str:
        # Slicing token_urlsafe(4) would provide only 32 random bits, not 36.
        return "".join(secrets.choice(ID_ALPHABET) for _ in range(32 if secure else 6))

    def _allocate_id(self, secure: bool) -> str:
        with self.database.connect() as connection:
            for _ in range(16):
                candidate = self._new_id(secure)
                if connection.execute("SELECT 1 FROM files WHERE id = ?", (candidate,)).fetchone() is None:
                    return candidate
        raise FileError(503, "Could not allocate a unique file link")

    def _publish(self, upload: Upload) -> None:
        os.fsync(upload.file.fileno())
        upload.file.close()
        destination = self._blob_path(upload.blob)
        # The process lock and exclusive staging name reserve this generated blob
        # name. Check again under the operation lock before the atomic rename.
        if destination.exists() or destination.is_symlink():
            raise StoreError("blob destination already exists")
        os.rename(upload.path, destination)
        upload.published = True
        _sync_directory(self.blobs)
        _sync_directory(self.staging)

    def _commit(self, record: FileRecord, previous: FileRecord | None) -> None:
        with self.database.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if previous is None:
                connection.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.id, record.blob, record.name, record.size, record.created_at,
                     record.modified_at, record.expires_at, record.revision),
                )
            else:
                result = connection.execute(
                    "UPDATE files SET blob = ?, name = ?, size = ?, modified_at = ?, expires_at = ?, revision = ? "
                    "WHERE id = ? AND revision = ? AND blob = ? AND (expires_at IS NULL OR expires_at > ?)",
                    (record.blob, record.name, record.size, record.modified_at, record.expires_at,
                     record.revision, record.id, previous.revision, previous.blob, int(self._clock())),
                )
                if result.rowcount != 1:
                    raise FileError(409, "File changed or disappeared during the update")

    def finish(self, upload: Upload) -> FileRecord:
        with self._lock:
            if upload.released or upload.published:
                raise StoreError("upload is no longer writable")
            previous = upload.previous
            if previous is not None:
                try:
                    current = self._find(previous.id)
                except FileError:
                    raise FileError(409, "File changed or disappeared during the update") from None
                if current.revision != previous.revision or current.blob != previous.blob:
                    raise FileError(409, "File changed or disappeared during the update")
            file_id = previous.id if previous else self._allocate_id(upload.options.secure)
            # Publish bytes durably before referring to them in SQLite.
            self._publish(upload)
            now = int(self._clock())
            lifetime = upload.options.lifetime
            expiration = (
                previous.expires_at if previous and lifetime is None
                else expiration_at(now, 30 * 86400 if lifetime is None else lifetime)
            )
            record = FileRecord(
                id=file_id, blob=upload.blob,
                name=upload.options.name if upload.options.name is not None else previous.name if previous else file_id,
                size=upload.size, created_at=previous.created_at if previous else now,
                modified_at=now, expires_at=expiration, revision=previous.revision + 1 if previous else 1,
            )
            self._commit(record, previous)
            if previous is not None:
                self._discard(previous.blob)
            return record

    def abort(self, upload: Upload) -> None:
        """Release the upload on every exit, including success and cancellation."""
        with self._lock:
            if upload.released:
                return
            try:
                upload.file.close()
                if not upload.published:
                    try:
                        upload.path.unlink()
                    except OSError:
                        logger.warning("Could not remove abandoned upload; storage remains charged")
                    else:
                        self.used_bytes -= upload.size
                # Once published, an ambiguous commit must never cause blob deletion.
                # Unreferenced blobs remain charged until a cleanup pass.
            finally:
                upload.released = True
                self._uploads.pop(upload.blob, None)
                self._transfers -= 1

    def patch(self, file_id: str, options: FileOptions) -> FileRecord:
        if options.name is None and options.lifetime is None:
            raise FileError(400, "Specify a filename or expiration to update")
        if options.name is not None:
            validate_name(options.name)
        with self._lock:
            previous = self._find(file_id)
            now = int(self._clock())
            record = replace(
                previous, name=previous.name if options.name is None else options.name,
                expires_at=previous.expires_at if options.lifetime is None else expiration_at(now, options.lifetime),
                modified_at=now, revision=previous.revision + 1,
            )
            self._commit(record, previous)
            return record

    def delete(self, file_id: str) -> None:
        with self._lock:
            previous = self._find(file_id)
            with self.database.connect() as connection, connection:
                connection.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._discard(previous.blob)

    def _discard(self, blob: str) -> None:
        self._pending.add(blob)
        if self._pins.get(blob, 0):
            return
        try:
            path = self._blob_path(blob)
            info = path.lstat()
            _check_regular(info)
            path.unlink()
            self.used_bytes -= info.st_size
            self._pending.discard(blob)
        except (OSError, StoreError):
            # The metadata commit already succeeded. Cleanup failure must not turn
            # a committed update into an apparent failure or release quota early.
            logger.warning("Blob cleanup deferred; storage remains charged")

    def open_download(self, file_id: str) -> Download:
        with self._lock:
            record = self._find(file_id)
            self._take_slot()
            try:
                descriptor = os.open(self._blob_path(record.blob), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(descriptor)
                    _check_regular(info)
                    if info.st_size != record.size:
                        raise StoreError("file metadata and blob size disagree")
                    file = os.fdopen(descriptor, "rb", buffering=0)
                except BaseException:
                    os.close(descriptor)
                    raise
                self._pins[record.blob] = self._pins.get(record.blob, 0) + 1
                return Download(file, record)
            except BaseException:
                self._transfers -= 1
                raise

    def close_download(self, download: Download) -> None:
        with self._lock:
            if download.released:
                return
            try:
                download.file.close()
            finally:
                download.released = True
                self._transfers -= 1
                blob = download.record.blob
                self._pins[blob] -= 1
                if self._pins[blob] == 0:
                    del self._pins[blob]
                    if blob in self._pending:
                        self._discard(blob)
