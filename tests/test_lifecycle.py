import asyncio
from dataclasses import replace
import errno
from pathlib import Path
import threading

import anyio
import pytest

from junkyard.database import StoreError
from junkyard.file_options import FileError, FileOptions
from junkyard.files import FileStore
from junkyard.lifecycle import cleanup_loop
from test_files import read, save


@pytest.fixture
def store(database, settings, clock):
    files = FileStore(database, replace(settings, max_file_bytes=100, storage_quota_bytes=200), clock=clock)
    files.start()
    try:
        yield files
    finally:
        files.close()


def test_expiration_defaults_updates_and_permanent_files(store, clock):
    record = save(store)
    assert record.expires_at == int(clock()) + 30 * 86400
    clock.advance(3600)
    updated = save(store, b"new", record.id, FileOptions())
    assert updated.created_at == record.created_at
    assert updated.modified_at == int(clock())
    assert updated.expires_at == record.expires_at
    updated = store.patch(record.id, FileOptions(lifetime=7200))
    assert updated.expires_at == int(clock()) + 7200
    permanent = save(store, b"forever", options=FileOptions(lifetime=0))
    clock.advance(7200)
    with pytest.raises(FileError) as error:
        read(store, record.id)
    assert error.value.status == 404
    store.cleanup()
    assert read(store, permanent.id) == b"forever"
    assert store.used_bytes == len(b"forever")


def test_expiration_blocks_new_reads_and_active_read_finishes(store, clock):
    record = save(store, options=FileOptions(lifetime=10))
    download = store.open_download(record.id)
    clock.advance(10)
    with pytest.raises(FileError) as error:
        store.open_download(record.id)
    assert error.value.status == 404
    store.cleanup()
    with store.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert store.used_bytes == len(b"original")
    assert download.file.read() == b"original"
    store.close_download(download)
    assert store.used_bytes == 0
    assert not list(store.blobs.iterdir())


def test_expired_replacement_cannot_resurrect_file(store, clock):
    record = save(store, options=FileOptions(lifetime=10))
    upload = store.begin(FileOptions(lifetime=0), record.id)
    store.write(upload, b"replacement")
    clock.advance(10)
    store.cleanup()
    assert upload.path.exists()
    try:
        with pytest.raises(FileError) as error:
            store.finish(upload)
        assert error.value.status == 409
    finally:
        store.abort(upload)
    assert store.used_bytes == 0


def test_cleanup_protects_staging_and_published_upload_until_commit(store, monkeypatch):
    upload = store.begin(FileOptions())
    store.write(upload, b"new bytes")
    store.cleanup()
    assert upload.path.exists()
    commit = store._commit

    def cleanup_during_publication(record, previous):
        store.cleanup()
        assert (store.blobs / record.blob).exists()
        commit(record, previous)

    monkeypatch.setattr(store, "_commit", cleanup_during_publication)
    try:
        record = store.finish(upload)
    finally:
        store.abort(upload)
    assert read(store, record.id) == b"new bytes"
    assert (store.blobs / record.blob).exists()  # Closing a read must not discard it.


def test_cleanup_keeps_partial_write_reservation_until_abort(store, monkeypatch):
    upload = store.begin(FileOptions())
    write = upload.file.write

    def partial(chunk):
        write(chunk[:2])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(upload.file, "write", partial)
    with pytest.raises(OSError):
        store.write(upload, b"1234567890")
    store.cleanup()
    assert store.used_bytes == 10
    store.abort(upload)
    assert store.used_bytes == 0


def test_restart_removes_abandoned_uploads_and_unreferenced_blobs(store, clock):
    live = save(store)
    interrupted = store.begin(FileOptions())
    store.write(interrupted, b"partial")
    interrupted.file.close()  # Simulate process death before the request's finally.
    published = store.begin(FileOptions())
    store.write(published, b"uncommitted")
    store._publish(published)
    store.close()
    restarted = FileStore(store.database, store.settings, clock=clock)
    restarted.start()
    try:
        assert read(restarted, live.id) == b"original"
        assert restarted.used_bytes == len(b"original")
        assert not list(restarted.staging.iterdir())
        assert [path.name for path in restarted.blobs.iterdir()] == [live.blob]
    finally:
        restarted.close()


@pytest.mark.parametrize("damage", ["missing", "size", "symlink", "unknown", "invalid_reference"])
def test_recovery_validates_before_deleting_anything(store, clock, damage, tmp_path):
    record = save(store)
    orphan = store.begin(FileOptions())
    store.write(orphan, b"uncommitted")
    store._publish(orphan)
    store.abort(orphan)
    referenced = store.blobs / record.blob
    if damage == "missing":
        referenced.unlink()
    elif damage == "size":
        referenced.write_bytes(b"wrong length")
    elif damage == "symlink":
        referenced.unlink()
        referenced.symlink_to(tmp_path / "outside")
    elif damage == "unknown":
        (store.staging / "unexpected-file").touch(mode=0o600)
    else:
        with store.database.connect() as connection, connection:
            connection.execute("UPDATE files SET blob = ?", (b"invalid-reference",))
    store.close()
    restarted = FileStore(store.database, store.settings, clock=clock)
    with pytest.raises((StoreError, OSError)):
        restarted.start()
    assert (store.blobs / orphan.blob).exists()
    with store.database.connect() as connection:
        assert connection.execute("SELECT id FROM files").fetchone()[0] == record.id
    assert restarted._process_lock is None


def test_startup_refuses_unsynced_storage_directories(database, settings, monkeypatch):
    def fail_sync(path):
        assert path == database.directory
        raise OSError(errno.EIO, "directory sync failed")

    monkeypatch.setattr("junkyard.files._sync_directory", fail_sync)
    files = FileStore(database, settings)
    with pytest.raises(OSError):
        files.start()
    assert files._process_lock is None
    database.check_initialized()


def test_failed_unlink_stays_charged_then_is_retried(store, clock, monkeypatch):
    record = save(store, options=FileOptions(lifetime=1))
    path = store.blobs / record.blob
    unlink = Path.unlink

    def fail(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError(errno.EIO, "injected failure")
        return unlink(candidate, *args, **kwargs)

    clock.advance(1)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        store.cleanup()
        assert store.used_bytes == 8
        assert path.exists()
    store.cleanup()
    assert store.used_bytes == 0
    assert not path.exists()


def test_periodic_cleanup_retries_without_logging_paths(caplog):
    async def exercise():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Files:
            calls = 0

            def cleanup(self):
                self.calls += 1
                if self.calls == 1:
                    raise StoreError("secret-path-must-not-be-logged")
                loop.call_soon_threadsafe(stop.set)

        files = Files()
        await asyncio.wait_for(cleanup_loop(files, 0.001, stop), timeout=5)
        assert files.calls == 2

    anyio.run(exercise)
    assert "will be retried" in caplog.text
    assert "secret-path" not in caplog.text


def test_cleanup_shutdown_waits_for_running_work():
    async def exercise():
        stop = asyncio.Event()
        started, release = threading.Event(), threading.Event()

        class Files:
            def cleanup(self):
                started.set()
                assert release.wait(5)

        task = asyncio.create_task(cleanup_loop(Files(), 0.001, stop))
        try:
            assert await anyio.to_thread.run_sync(started.wait, 5)
            stop.set()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=5)

    anyio.run(exercise)
