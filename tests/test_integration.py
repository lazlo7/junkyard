"""Real CLI/terminal/HTTP checks using only temporary project-local credentials."""

import os
import pty
import select
import shutil
import socket
import subprocess
import sys
import time

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


def test_uvicorn_and_curl_use_password_file_without_logging_secrets(database, config_file, tmp_path):
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
