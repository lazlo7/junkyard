"""Real CLI/terminal/HTTP checks using only temporary project-local credentials."""

import os
import base64
import pty
import select
import shutil
import socket
import subprocess
import sys
import time
import json
from urllib.parse import quote, urlsplit

import httpx

from junkyard.config import load_settings
from junkyard.database import Database
from junkyard.security import PASSWORD_HASHER
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


def test_real_terminal_passwords_are_not_echoed(config_file):
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "junkyard", "--config", str(config_file), "init"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    try:
        for label, password in (
            (b"New CLI password:", CLI_PASSWORD),
            (b"Confirm CLI password:", CLI_PASSWORD),
            (b"New browser password:", BROWSER_PASSWORD),
            (b"Confirm browser password:", BROWSER_PASSWORD),
        ):
            deadline = time.monotonic() + 10
            while label not in output:
                assert time.monotonic() < deadline, "timed out waiting for hidden prompt"
                assert process.poll() is None, "credential command exited early"
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    output.extend(os.read(master, 4096))
            os.write(master, password.encode() + b"\n")
        assert process.wait(timeout=10) == 0
        while select.select([master], [], [], 0)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break  # Linux PTYs return EIO when the slave closes.
            if not chunk:
                break
            output.extend(chunk)
        assert CLI_PASSWORD.encode() not in output
        assert BROWSER_PASSWORD.encode() not in output
        database = Database(load_settings(config_file).data_dir)
        assert PASSWORD_HASHER.verify(database.password_hash("cli"), CLI_PASSWORD)
        assert PASSWORD_HASHER.verify(database.password_hash("browser"), BROWSER_PASSWORD)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)


def test_uvicorn_and_curl_authentication_and_file_lifecycle(database, config_file, tmp_path):
    assert shutil.which("curl"), "curl is required for the HTTP integration test"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with config_file.open("a") as config:
        config.write(f"bind_port = {port}\n")
    process = subprocess.Popen(
        [sys.executable, "-m", "junkyard", "--config", str(config_file), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "server exited before becoming ready"
                try:
                    response = client.get(base_url + "/auth/check")
                    assert response.status_code == 401
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline, "server did not start"
                    time.sleep(0.05)

        curl_config = tmp_path / "curl.conf"
        curl_config.touch(mode=0o600)
        for password, expected_status in ((CLI_PASSWORD, "204"), (BROWSER_PASSWORD, "401")):
            curl_config.write_text(f'user = "admin:{password}"\n')
            result = subprocess.run(
                ["curl", "-q", "--noproxy", "*", "--silent", "--show-error", "--max-time", "5",
                 "--config", str(curl_config), "--output", os.devnull, "--write-out", "%{http_code}",
                 base_url + "/auth/check?private=test-url-marker"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            assert result.stdout == expected_status
            assert not result.stderr

        curl_config.write_text(f'user = "admin:{CLI_PASSWORD}"\n')

        def curl(*arguments, body=None):
            result = subprocess.run(
                ["curl", "-q", "--noproxy", "*", "--silent", "--show-error", "--fail", "--max-time", "10",
                 "--config", str(curl_config), *arguments],
                input=body, capture_output=True, timeout=15, check=True,
            )
            assert not result.stderr
            return result.stdout

        source = tmp_path / "upload sample.bin"
        contents = bytes(range(256)) * 4096
        source.write_bytes(contents)
        short = json.loads(curl("-H", "Accept: application/json", "-T", str(source), base_url + "/u/"))
        assert len(short["id"]) == 6
        assert short["filename"] == source.name
        assert curl("-X", "DELETE", base_url + "/" + short["id"]) == b""
        unnamed = json.loads(curl("-H", "Accept: application/json", "-T", str(source), base_url + "/u/?s=1&e=7d"))
        assert len(unnamed["id"]) == 32
        assert unnamed["filename"] == unnamed["id"]
        assert curl("-X", "DELETE", base_url + "/" + unnamed["id"]) == b""
        uploaded = json.loads(curl("-H", "Accept: application/json", "-T", str(source),
                                  base_url + "/u/" + quote(source.name, safe="") + "?s=1&e=7d"))
        assert len(uploaded["id"]) == 32
        assert uploaded["filename"] == source.name
        assert uploaded["size"] == len(contents)
        file_url = base_url + urlsplit(uploaded["url"]).path
        assert curl(file_url) == contents
        viewer = curl("-H", "Accept: text/html", file_url)
        assert b"<!doctype html>" in viewer and b"No browser preview" in viewer
        assert uploaded["id"].encode() in viewer
        assert curl("-H", "Accept: text/html", file_url + "/download") == contents
        assert curl("-H", "Accept: text/html", file_url + "/raw") == contents
        head = curl("--head", file_url)
        assert b"accept-ranges: bytes" in head.lower()
        assert f"content-length: {len(contents)}".encode() in head.lower()
        etag = next(line.split(b":", 1)[1].strip().decode() for line in head.splitlines() if line.lower().startswith(b"etag:"))
        assert curl("--range", "10-29", file_url) == contents[10:30]
        assert curl("--range", "10-29", "-H", "If-Range: " + etag, file_url) == contents[10:30]
        partial = tmp_path / "resumed.bin"
        partial.write_bytes(contents[:12345])
        assert curl("--continue-at", "-", "--output", str(partial), file_url) == b""
        assert partial.read_bytes() == contents
        replacement = b"replacement\x00" * 12000
        # Reading stdin makes curl stream an HTTP/1.1 chunked request.
        assert curl("--http1.1", "-T", "-", file_url, body=replacement) == (uploaded["url"] + "\n").encode()
        assert curl(file_url) == replacement
        assert curl("--range", "10-29", "-H", "If-Range: " + etag, file_url) == replacement
        changed = json.loads(curl("-H", "Accept: application/json", "-X", "PATCH", file_url + "?name=renamed.bin&e=0"))
        assert changed["filename"] == "renamed.bin"
        assert changed["expires_at"] is None
        assert curl("-X", "DELETE", file_url) == b""
        with httpx.Client(trust_env=False) as client:
            assert client.get(file_url).status_code == 404
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    assert CLI_PASSWORD not in stdout + stderr
    assert BROWSER_PASSWORD not in stdout + stderr
    assert "test-url-marker" not in stdout + stderr


def test_process_crash_recovery_keeps_committed_file_and_removes_partial_upload(database, config_file):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with config_file.open("a") as config:
        config.write(f"bind_port = {port}\n")
    base_url = f"http://127.0.0.1:{port}"

    def start():
        process = subprocess.Popen(
            [sys.executable, "-m", "junkyard", "--config", str(config_file), "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while True:
                    assert process.poll() is None, "server failed during startup recovery"
                    try:
                        assert client.get(base_url + "/auth/check").status_code == 401
                        return process
                    except httpx.TransportError:
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
        except BaseException:
            process.kill()
            process.communicate(timeout=5)
            raise

    process = start()
    connection = None
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            saved = client.put(base_url + "/u/kept", content=b"committed contents", auth=("admin", CLI_PASSWORD))
            assert saved.status_code == 201
            path = urlsplit(saved.text.strip()).path
        connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        credentials = base64.b64encode(f"admin:{CLI_PASSWORD}".encode()).decode()
        connection.sendall((f"PUT /u/interrupted HTTP/1.1\r\nHost: localhost\r\n"
                            f"Authorization: Basic {credentials}\r\nContent-Length: 1000000\r\n\r\n").encode() + b"x" * 65536)
        deadline = time.monotonic() + 10
        while not any(file.stat().st_size for file in (database.directory / "staging").iterdir()):
            assert time.monotonic() < deadline, "upload did not reach staging storage"
            time.sleep(0.05)
        process.kill()  # An actual process crash, bypassing request cleanup.
        process.communicate(timeout=5)
        connection.close()
        connection = None
        assert list((database.directory / "staging").iterdir())
        process = start()
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(base_url + path).content == b"committed contents"
        assert not list((database.directory / "staging").iterdir())
        assert len(list((database.directory / "blobs").iterdir())) == 1
    finally:
        if connection is not None:
            connection.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
