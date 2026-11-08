"""Release checks use only synthetic data, unprivileged processes and loopback."""

import base64
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
import pytest

from conftest import BROWSER_PASSWORD, CLI_PASSWORD
from junkyard.browser_auth import BrowserAuth, SESSION_COOKIE
from junkyard.config import load_settings
from junkyard.database import Database, StoreError
from junkyard.file_options import FileOptions
from junkyard.files import FileStore
from junkyard.security import CLIAuthenticator

ROOT = Path(__file__).resolve().parents[1]


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(condition, message, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


@contextmanager
def server(config_file, tmp_path):
    settings = load_settings(config_file)
    log_path = tmp_path / "application.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "junkyard", "--config", str(config_file), "serve"],
                                   stdout=log, stderr=log)
        try:
            origin = f"http://127.0.0.1:{settings.bind_port}"
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                def ready():
                    assert process.poll() is None, "application exited during startup"
                    try:
                        return client.get(origin + "/auth/check").status_code in (400, 401)
                    except httpx.TransportError:
                        return False
                wait_for(ready, "application did not start")
            # Both processes in release checks run under the ordinary test user.
            assert os.getuid() != 0, "release integration checks must run without root"
            status = Path(f"/proc/{process.pid}/status").read_text()
            assert re.search(rf"^Uid:\s+{os.getuid()}\s+{os.getuid()}\s", status, re.MULTILINE)
            yield process, origin, log_path
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def save(store, contents, name, lifetime=0, file_id=None):
    upload = store.begin(FileOptions(name=name, lifetime=lifetime), file_id)
    try:
        store.write(upload, contents)
        return store.finish(upload)
    finally:
        store.abort(upload)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def test_offline_backup_restores_metadata_blobs_credentials_and_expiration(database, settings, tmp_path, clock, hashes):
    original = FileStore(database, settings, clock=clock)
    original.start()
    kept = save(original, b"contents at backup time", "original.txt")
    expired = save(original, b"short lived", "expires.txt", lifetime=1)
    auth = BrowserAuth(database, CLIAuthenticator(database), clock=clock)
    session_token = auth.login(BROWSER_PASSWORD)
    original.close()  # The documented procedure first stops all writers.
    backup = tmp_path / "backup"
    shutil.copytree(database.directory, backup, copy_function=shutil.copy2)
    manifest = {path.relative_to(backup): digest(path) for path in backup.rglob("*") if path.is_file()}
    original.start()
    try:
        save(original, b"later replacement", "changed.txt", file_id=kept.id)
        original.delete(expired.id)
        database.replace_password("cli", hashes[1])
    finally:
        original.close()
    restored_dir = tmp_path / "restored"
    shutil.copytree(backup, restored_dir, copy_function=shutil.copy2)
    assert {path: digest(restored_dir / path) for path in manifest} == manifest
    restored_db = Database(restored_dir)
    with restored_db.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored_db.password_hash("cli") == hashes[0]
    # Restoring also restores old sessions: reset the browser password before
    # exposing the recovered service, as required by the recovery instructions.
    restored_auth = BrowserAuth(restored_db, CLIAuthenticator(restored_db), clock=clock)
    assert restored_auth.resolve(session_token) is not None
    restored_db.replace_password("browser", hashes[1])
    assert restored_auth.resolve(session_token) is None
    clock.advance(2)
    recovered = FileStore(restored_db, replace(settings, data_dir=restored_dir), clock=clock)
    recovered.start()
    try:
        download = recovered.open_download(kept.id)
        try:
            assert download.record.name == "original.txt"
            assert download.file.read() == b"contents at backup time"
        finally:
            recovered.close_download(download)
        assert recovered.list_files(1)["total"] == 1
        assert not (recovered.blobs / expired.blob).exists()
    finally:
        recovered.close()


def test_incomplete_backup_is_rejected_before_metadata_is_deleted(database, settings, tmp_path):
    store = FileStore(database, settings)
    store.start()
    record = save(store, b"must exist", "kept.txt")
    store.close()
    restored = tmp_path / "incomplete"
    shutil.copytree(database.directory, restored)
    (restored / "blobs" / record.blob).unlink()  # Damage only this synthetic restore.
    restored_db = Database(restored)
    with pytest.raises(StoreError, match="referenced blob"):
        FileStore(restored_db, replace(settings, data_dir=restored)).start()
    with restored_db.connect() as connection:
        assert connection.execute("SELECT id FROM files").fetchone()[0] == record.id


def rss(pid):
    return int(re.search(r"^VmRSS:\s+(\d+) kB$", Path(f"/proc/{pid}/status").read_text(), re.MULTILINE)[1]) * 1024


def test_four_simultaneous_uploads_and_downloads_have_bounded_memory(database, config_file, tmp_path):
    port = unused_port()
    with config_file.open("a") as config:
        config.write(f"bind_port = {port}\n")
    size = 32 * 1024 * 1024
    chunk = b"streamed test content\n".ljust(64 * 1024, b"x")
    expected_hash = hashlib.sha256()
    for _ in range(size // len(chunk)):
        expected_hash.update(chunk)
    authorization = base64.b64encode(f"admin:{CLI_PASSWORD}".encode()).decode()
    sockets = []
    with server(config_file, tmp_path) as (process, origin, _):
        try:
            for index in range(4):
                connection = socket.create_connection(("127.0.0.1", port), timeout=10)
                connection.settimeout(30)
                sockets.append(connection)
                connection.sendall((f"PUT /u/file-{index} HTTP/1.1\r\nHost: localhost\r\n"
                                    f"Authorization: Basic {authorization}\r\nContent-Length: {size}\r\n"
                                    "Connection: close\r\n\r\n").encode() + chunk)
                # Sequence authentication only; the four file transfers overlap.
                wait_for(lambda: len(list((database.directory / "staging").iterdir())) == index + 1, "upload did not begin")
            with httpx.Client(trust_env=False) as client:
                denied = client.put(origin + "/u/fifth", content=b"", auth=("admin", CLI_PASSWORD))
                assert denied.status_code == 503 and denied.headers["retry-after"] == "1"
            baseline = rss(process.pid)
            peak = baseline
            for offset in range(len(chunk), size, len(chunk)):
                for connection in sockets:
                    connection.sendall(chunk)
                if offset % (1024 * 1024) == 0:
                    peak = max(peak, rss(process.pid))
            paths = []
            for connection in sockets:
                response = http.client.HTTPResponse(connection)
                response.begin()
                assert response.status == 201
                paths.append(urlsplit(response.read().decode().strip()).path)
                connection.close()
            sockets.clear()
            assert peak - baseline < 32 * 1024 * 1024
            upload_memory = {"baseline": baseline, "peak": peak}
            readers = []
            baseline = rss(process.pid)
            peak = baseline
            for path in paths:
                connection = socket.socket()
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
                connection.settimeout(30)
                connection.connect(("127.0.0.1", port))
                sockets.append(connection)
                connection.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                assert response.status == 200
                readers.append(response)
                peak = max(peak, rss(process.pid))
            with httpx.Client(trust_env=False) as client:
                assert client.get(origin + paths[0]).status_code == 503
            peak = max(peak, rss(process.pid))
            hashes = [hashlib.sha256() for _ in readers]
            for offset in range(0, size, len(chunk)):
                for response, checksum in zip(readers, hashes):
                    block = response.read(len(chunk))
                    assert len(block) == len(chunk)
                    checksum.update(block)
                if offset % (1024 * 1024) == 0:
                    peak = max(peak, rss(process.pid))
            assert all(checksum.hexdigest() == expected_hash.hexdigest() for checksum in hashes)
            assert peak - baseline < 32 * 1024 * 1024
            (tmp_path / "transfer-memory.json").write_text(json.dumps({
                "bytes_per_file": size, "concurrent_transfers": 4,
                "upload_rss": upload_memory, "download_rss": {"baseline": baseline, "peak": peak},
            }))
        finally:
            for connection in sockets:
                connection.close()


def test_deployment_defaults_agree_and_systemd_unit_is_valid(tmp_path):
    settings = load_settings(ROOT / "deploy/junkyard.toml")
    nginx = (ROOT / "deploy/nginx.conf").read_text()
    assert int(re.search(r"client_max_body_size (\d+);", nginx)[1]) == settings.max_file_bytes
    assert settings.bind_host == "127.0.0.1" and settings.trusted_proxies == ("127.0.0.1/32",)
    assert "proxy_request_buffering off;" in nginx and "proxy_buffering off;" in nginx
    assert "proxy_next_upstream off;" in nginx
    service = (ROOT / "deploy/junkyard.service").read_text()
    assert "User=junkyard\n" in service and "UMask=0077\n" in service
    assert "ProtectSystem=strict\n" in service and "StateDirectoryMode=0700\n" in service
    verifier = shutil.which("systemd-analyze")
    if verifier is None:
        pytest.skip("systemd-analyze is required to verify the service example")
    # Use an existing executable for validation; do not install/start a unit.
    unit = tmp_path / "junkyard.service"
    unit.write_text(service.replace("/opt/junkyard/.venv/bin/junkyard", str(ROOT / ".venv/bin/junkyard")))
    result = subprocess.run([verifier, "verify", "--man=no", str(unit)], capture_output=True, text=True, timeout=15,
                            env={**os.environ, "SYSTEMD_UNIT_PATH": str(tmp_path) + ":/usr/lib/systemd/system"})
    assert result.returncode == 0, result.stderr


def test_nginx_https_streaming_limits_proxy_identity_and_log_privacy(database, config_file, tmp_path):
    nginx = shutil.which("nginx")
    openssl = shutil.which("openssl")
    if nginx is None or openssl is None:
        pytest.skip("Nginx and OpenSSL are required for the proxy release check")
    backend_port, tls_port, plain_port = unused_port(), unused_port(), unused_port()
    origin = f"https://127.0.0.1:{tls_port}"
    limit = 64 * 1024
    config_file.write_text(f'public_url = "{origin}"\ndata_dir = "data"\nbind_port = {backend_port}\n'
                           f'trusted_proxies = ["127.0.0.1"]\nmax_file_bytes = {limit}\n'
                           'anonymous_burst = 2\nanonymous_rate = 0.001\n')
    certificate, key = tmp_path / "proxy-cert.pem", tmp_path / "proxy-key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-noenc",
                    "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], capture_output=True, check=True, timeout=10)
    context = ssl.create_default_context(cafile=certificate)
    proxy_log = tmp_path / "proxy.log"
    configuration = (ROOT / "deploy/nginx.conf").read_text()
    configuration = configuration.replace("user www-data;", "").replace("worker_processes auto;", "worker_processes 1;")
    for old, new in {
        "/run/nginx.pid": str(tmp_path / "nginx.pid"), "/var/log/nginx/junkyard-access.log": str(proxy_log),
        "/etc/ssl/junkyard/fullchain.pem": str(certificate), "/etc/ssl/junkyard/privkey.pem": str(key),
        "listen 80 default_server": f"listen 127.0.0.1:{plain_port} default_server",
        "listen 443 ssl default_server": f"listen 127.0.0.1:{tls_port} ssl default_server",
        "files.example.com": "127.0.0.1", "127.0.0.1:8000": f"127.0.0.1:{backend_port}",
        "client_max_body_size 262144000;": f"client_max_body_size {limit};",
    }.items():
        configuration = configuration.replace(old, new)
    # Nginx may initialize unused module temp directories on startup. Keep all
    # of them inside this test prefix rather than distribution defaults in /var.
    temporary_paths = "\n".join(f"{module}_temp_path {tmp_path / module};" for module in
                                 ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    configuration = configuration.replace("http {", "http {\n" + temporary_paths)
    proxy_config = tmp_path / "nginx.conf"
    proxy_config.write_text(configuration)
    arguments = [nginx, "-p", str(tmp_path) + "/", "-c", str(proxy_config)]
    checked = subprocess.run([*arguments, "-t"], capture_output=True, text=True, timeout=10)
    assert checked.returncode == 0, checked.stderr
    secrets = [CLI_PASSWORD, BROWSER_PASSWORD, "private-filename", "private-query", "private-correlation", "private-referer"]
    with server(config_file, tmp_path) as (application, backend_origin, application_log):
        with (tmp_path / "proxy-process.log").open("wb") as output:
            proxy = subprocess.Popen([*arguments, "-g", "daemon off;"], stdout=output, stderr=output)
            try:
                with httpx.Client(verify=context, trust_env=False, timeout=10) as client:
                    def ready():
                        assert proxy.poll() is None, "Nginx exited during startup"
                        try:
                            return client.get(origin + "/auth/check").status_code == 401
                        except httpx.TransportError:
                            return False
                    wait_for(ready, "HTTPS proxy did not start")
                    assert client.get(f"http://127.0.0.1:{plain_port}/").status_code == 400
                    assert client.get(origin + "/", headers={"Host": "other.example.com"}).status_code == 421
                    page = client.get(origin + "/admin/login", headers={"X-Forwarded-For": "forged, chain"})
                    assert page.status_code == 200
                    csrf = re.search(r'<meta name="csrf-token" content="([0-9a-f]{64})">', page.text)[1]
                    response = client.post(origin + "/admin/login", json={"password": BROWSER_PASSWORD},
                                           headers={"Origin": origin, "X-CSRF-Token": csrf})
                    assert response.status_code == 200
                    secrets.extend((csrf, client.cookies[SESSION_COOKIE]))
                    uploaded = client.put(origin + "/u/private-filename?name=private-query", content=b"x" * limit,
                                          auth=("admin", CLI_PASSWORD), headers={"X-Request-ID": "private-correlation",
                                                                               "Referer": "https://private-referer/"})
                    assert uploaded.status_code == 201
                    path = urlsplit(uploaded.text.strip()).path
                    secrets.extend((uploaded.text.strip(), path.lstrip("/")))
                    assert re.fullmatch(r"[0-9a-f]{32}", uploaded.headers["x-request-id"])
                    assert "max-age=" in uploaded.headers["strict-transport-security"]
                    assert client.get(origin + path).content == b"x" * limit
                    assert client.get(origin + path, headers={"Range": "bytes=10-29"}).content == b"x" * 20
                    assert client.put(origin + "/u/too-big", content=b"x" * (limit + 1), auth=("admin", CLI_PASSWORD)).status_code == 413
                    assert client.put(backend_origin + "/u/too-big", content=b"x" * (limit + 1), auth=("admin", CLI_PASSWORD),
                                      headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 413
                    # Streaming reaches staging before the proxy has the complete body.
                    credentials = base64.b64encode(f"admin:{CLI_PASSWORD}".encode()).decode()
                    with context.wrap_socket(socket.create_connection(("127.0.0.1", tls_port), timeout=10), server_hostname="127.0.0.1") as connection:
                        connection.sendall((f"PUT /u/streamed HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Basic {credentials}\r\n"
                                            f"Content-Length: {limit}\r\nConnection: close\r\n\r\n").encode() + b"a" * 16384)
                        wait_for(lambda: any(p.stat().st_size for p in (database.directory / "staging").iterdir()), "proxy buffered upload")
                        connection.sendall(b"a" * (limit - 16384))
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        assert response.status == 201
                        response.read()
                    chunked = client.put(origin + "/u/too-big", content=iter((b"x" * limit, b"x")), auth=("admin", CLI_PASSWORD))
                    assert chunked.status_code == 413
                    wait_for(lambda: not list((database.directory / "staging").iterdir()), "aborted proxy upload retained staging")
                    blob = next((database.directory / "blobs").iterdir()).name
                    for private_path in ("/metadata.sqlite3", "/data/metadata.sqlite3", "/blobs/" + blob, "/.files.lock"):
                        assert client.get(origin + private_path).status_code == 404
                    assert client.get(origin + "/admin/login/").status_code == 404
                    assert client.request("PRIVATE_METHOD", origin + "/private-query").status_code == 405
                    secrets.append("PRIVATE_METHOD")
                    client.cookies.clear()
                    # Only one anonymous token remains after the login page.
                    assert client.get(origin + "/NoSuch", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 404
                    limited = client.get(origin + "/NoSuch", headers={"X-Forwarded-For": "192.0.2.2"})
                    assert limited.status_code == 429 and "retry-after" in limited.headers
                    application.terminate()
                    application.wait(timeout=40)
                    assert client.get(origin + "/private-query").status_code == 502
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)
    logs = application_log.read_text() + proxy_log.read_text() + (tmp_path / "proxy-process.log").read_text()
    assert all(secret not in logs for secret in secrets)
    app_records = [json.loads(line) for line in application_log.read_text().splitlines()]
    proxy_records = [json.loads(line) for line in proxy_log.read_text().splitlines()]
    assert any(record.get("operation") == "upload" and record.get("status") == 201 for record in app_records)
    assert any(record["status"] == 502 for record in proxy_records)
    assert any(record["app_request_id"] == uploaded.headers["x-request-id"] for record in proxy_records)
