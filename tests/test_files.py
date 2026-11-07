"""Persistence, quota, and concurrent-operation invariants."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import sqlite3

import pytest

from junkyard.database import Database, StoreError
from junkyard.file_options import FileError, FileOptions
from junkyard.files import FileStore


@pytest.fixture
def store(database, settings):
    files = FileStore(database, replace(settings, max_file_bytes=100, storage_quota_bytes=200))
    files.start()
    try:
        yield files
    finally:
        files.close()


def save(store, contents=b"original", file_id=None, options=FileOptions(name="sample.txt")):
    upload = store.begin(options, file_id)
    try:
        store.write(upload, contents)
        return store.finish(upload)
    finally:
        store.abort(upload)


def read(store, file_id):
    download = store.open_download(file_id)
    try:
        return download.file.read()
    finally:
        store.close_download(download)


def test_migration_preserves_credentials_and_is_idempotent(database, hashes):
    with database.connect() as connection, connection:
        connection.execute("DROP TABLE browser_sessions")
        connection.execute("DROP TABLE files")
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(StoreError, match="migrate"):
        database.require_file_schema()
    database.migrate()
    database.migrate()
    database.require_file_schema()
    assert database.password_hash("cli") == hashes[0]
    assert database.password_hash("browser") == hashes[1]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_failed_migration_rolls_back(database, monkeypatch):
    with database.connect() as connection, connection:
        connection.execute("DROP TABLE browser_sessions")
        connection.execute("DROP TABLE files")
        connection.execute("PRAGMA user_version = 1")

    def fail(connection):
        connection.execute("CREATE TABLE partial (id INTEGER)")
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr("junkyard.database.create_file_schema", fail)
    with pytest.raises(StoreError):
        database.migrate()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'partial'").fetchone() is None


def test_second_worker_is_refused(store):
    second = FileStore(store.database, store.settings)
    with pytest.raises(StoreError, match="one server worker"):
        second.start()
    assert read(store, save(store).id) == b"original"


def test_collision_retry_preserves_existing_file(store, monkeypatch):
    original = save(store)
    candidates = iter([original.id, "AbC_12"])
    monkeypatch.setattr(store, "_new_id", lambda secure: next(candidates))
    new = save(store, b"second")
    assert new.id == "AbC_12"
    assert read(store, original.id) == b"original"
    assert read(store, new.id) == b"second"
    monkeypatch.setattr(store, "_new_id", lambda secure: original.id)
    with pytest.raises(FileError) as error:
        save(store, b"failed")
    assert error.value.status == 503
    assert not list(store.staging.iterdir())
    assert store.used_bytes == len(b"originalsecond")


def test_active_read_survives_replacement_and_deletion(store):
    original = save(store, b"old bytes")
    active = store.open_download(original.id)
    updated = save(store, b"new bytes", original.id, FileOptions())
    assert updated.id == original.id
    assert updated.name == original.name
    assert updated.created_at == original.created_at
    assert updated.expires_at == original.expires_at
    assert store.used_bytes == 18
    assert active.file.read() == b"old bytes"
    newer = store.open_download(original.id)
    store.delete(original.id)
    with pytest.raises(FileError) as error:
        store.open_download(original.id)
    assert error.value.status == 404
    assert newer.file.read() == b"new bytes"
    store.close_download(active)
    assert store.used_bytes == 9
    store.close_download(newer)
    assert store.used_bytes == 0
    assert not list(store.blobs.iterdir())


def test_concurrent_replacements_have_one_winner(store):
    original = save(store)
    uploads = [store.begin(FileOptions(), original.id) for _ in range(2)]
    for upload, content in zip(uploads, (b"first", b"second")):
        store.write(upload, content)

    def finish(upload):
        try:
            return store.finish(upload)
        except FileError as error:
            return error.status
        finally:
            store.abort(upload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, uploads))
    assert sum(result == 409 for result in results) == 1
    assert read(store, original.id) in (b"first", b"second")
    assert len(list(store.blobs.iterdir())) == 1


@pytest.mark.parametrize("interference", ["delete", "patch"])
def test_stale_upload_cannot_overwrite_a_change(store, interference):
    original = save(store)
    upload = store.begin(FileOptions(), original.id)
    store.write(upload, b"replacement")
    if interference == "delete":
        store.delete(original.id)
    else:
        store.patch(original.id, FileOptions(name="renamed.txt"))
    try:
        with pytest.raises(FileError) as error:
            store.finish(upload)
        assert error.value.status == 409
    finally:
        store.abort(upload)
    assert not list(store.staging.iterdir())
    if interference == "patch":
        assert read(store, original.id) == b"original"
        assert store._find(original.id).name == "renamed.txt"


def test_concurrent_staging_bytes_cannot_bypass_quota(store):
    store.settings = replace(store.settings, storage_quota_bytes=10)
    uploads = [store.begin(FileOptions()) for _ in range(2)]

    def write(upload):
        try:
            store.write(upload, b"123456")
            return 200
        except FileError as error:
            return error.status

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(write, uploads)) == [200, 507]
        assert store.used_bytes == 6
    finally:
        for upload in uploads:
            store.abort(upload)
    assert store.used_bytes == 0


def test_quota_requires_space_for_both_versions(store):
    original = save(store, b"12345678")
    store.settings = replace(store.settings, storage_quota_bytes=10)
    with pytest.raises(FileError) as error:
        save(store, b"replacement", original.id)
    assert error.value.status == 507
    assert read(store, original.id) == b"12345678"
    assert store.used_bytes == 8


def test_partial_disk_write_failure_keeps_old_file(store, monkeypatch):
    original = save(store)
    upload = store.begin(FileOptions(), original.id)
    real_write = upload.file.write

    def no_space(chunk):
        real_write(chunk[:2])
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(upload.file, "write", no_space)
    try:
        with pytest.raises(OSError):
            store.write(upload, b"replacement")
    finally:
        store.abort(upload)
    assert read(store, original.id) == b"original"
    assert store.used_bytes == len(b"original")
    assert not list(store.staging.iterdir())


@pytest.mark.parametrize("boundary", ["before_publish", "after_publish", "before_commit", "after_commit"])
def test_publication_failure_never_deletes_a_referenced_blob(store, monkeypatch, boundary):
    original = save(store)
    publish, commit = store._publish, store._commit

    def fail_publish(upload):
        if boundary == "after_publish":
            publish(upload)
        raise OSError(errno.EIO, "injected publication failure")

    def fail_commit(record, previous):
        if boundary == "after_commit":
            commit(record, previous)
        raise StoreError("injected commit failure")

    monkeypatch.setattr(store, "_publish" if "publish" in boundary else "_commit",
                        fail_publish if "publish" in boundary else fail_commit)
    with pytest.raises((StoreError, OSError)):
        save(store, b"new", original.id)
    assert read(store, original.id) == (b"new" if boundary == "after_commit" else b"original")
    assert not list(store.staging.iterdir())
    assert store.used_bytes == (8 if boundary == "before_publish" else 11)
    # Recovery removes orphans while preserving the outcome of the DB commit.
    store.close()
    reopened = FileStore(store.database, store.settings)
    reopened.start()
    try:
        assert reopened.used_bytes == (3 if boundary == "after_commit" else 8)
        assert read(reopened, original.id) == (b"new" if boundary == "after_commit" else b"original")
    finally:
        reopened.close()


def test_actual_database_rollback_keeps_previous_contents(store):
    original = save(store)
    with store.database.connect() as connection, connection:
        connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON files BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(StoreError):
        save(store, b"replacement", original.id)
    assert read(store, original.id) == b"original"
    assert store.used_bytes == len(b"originalreplacement")


def test_failed_cleanup_does_not_report_a_committed_update_as_failed(store, monkeypatch):
    original = save(store)
    old_path = store.blobs / original.blob
    real_unlink = type(old_path).unlink

    def fail(path, *args, **kwargs):
        if path == old_path:
            raise OSError(errno.EIO, "injected cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(old_path), "unlink", fail)
    updated = save(store, b"new", original.id)
    assert read(store, updated.id) == b"new"
    assert store.used_bytes == 11


def test_symlink_blob_cannot_be_read(store, tmp_path):
    record = save(store)
    outside = tmp_path / "unrelated"
    outside.write_bytes(b"must not be served")
    path = store.blobs / record.blob
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        store.open_download(record.id)
    assert store._transfers == 0


def test_transfer_limit_releases_slots_after_abort(store):
    store.settings = replace(store.settings, max_transfers=1)
    upload = store.begin(FileOptions())
    with pytest.raises(FileError) as error:
        store.begin(FileOptions())
    assert error.value.status == 503
    store.abort(upload)
    assert save(store, b"works").size == 5
