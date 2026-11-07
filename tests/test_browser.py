"""Optional real-browser smoke check, with an isolated project-local profile."""

import os
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest

from junkyard.file_options import FileOptions
from junkyard.files import FileStore
from conftest import BROWSER_PASSWORD, CLI_PASSWORD


def test_chrome_renders_escaped_text_and_loads_share_script(database, settings, config_file, tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if chrome is None:
        pytest.skip("Chrome/Chromium is optional for the browser smoke test")
    store = FileStore(database, settings)
    store.start()
    try:
        upload = store.begin(FileOptions(name="Release notes.txt", lifetime=0))
        try:
            store.write(upload, b"Release notes\n\nThis is a shared text file.\n<script>document.body.textContent='INJECTED'</script>\n")
            record = store.finish(upload)
        finally:
            store.abort(upload)
    finally:
        store.close()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with config_file.open("a") as config:
        config.write(f"bind_port = {port}\n")
    server = subprocess.Popen(
        [sys.executable, "-m", "junkyard", "--config", str(config_file), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/{record.id}"
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            deadline = time.monotonic() + 10
            while True:
                assert server.poll() is None, "server exited before becoming ready"
                try:
                    assert client.head(url).status_code == 200
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
        result = subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
             "--disable-background-networking", "--disable-extensions", "--disable-sync",
             "--no-first-run", "--no-default-browser-check", "--disable-breakpad", "--no-proxy-server",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
             f"--user-data-dir={tmp_path / 'chrome-profile'}", "--window-size=1200,1000",
             f"--screenshot={tmp_path / 'viewer.png'}", "--dump-dom", "--virtual-time-budget=2000", url],
            env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "chrome-config"),
                 "XDG_CACHE_HOME": str(tmp_path / "chrome-cache"),
                 "XDG_DATA_HOME": str(tmp_path / "chrome-data")},
            capture_output=True, text=True, timeout=30, check=True,
        )
        assert '<h1>Release notes.txt</h1>' in result.stdout
        assert '&lt;script&gt;' in result.stdout
        # The only executable script is our fixed asset, which reveals this button.
        assert '<button id="copy-link" type="button">Copy share link</button>' in result.stdout
        assert f'value="https://files.example.com/{record.id}"' in result.stdout
        assert (tmp_path / "viewer.png").stat().st_size > 1000
    finally:
        server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)


def test_browser_admin_workflow_over_https(database, config_file, tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    node = shutil.which("node")
    openssl = shutil.which("openssl")
    if not chrome or not node or not openssl:
        pytest.skip("Chrome, Node 22+, and OpenSSL are optional for the browser-admin test")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout
    if int(version.lstrip("v").split(".")[0]) < 22:
        pytest.skip("Node 22+ is needed for its built-in WebSocket client")
    key = tmp_path / "test-key.pem"
    certificate = tmp_path / "test-cert.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-noenc", "-keyout", str(key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   capture_output=True, check=True, timeout=10)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    config_file.write_text(f'public_url = "{origin}"\ndata_dir = "data"\nbind_port = {port}\n')
    source = tmp_path / "browser-upload.txt"
    source.write_text("original browser bytes")
    replacement = tmp_path / "browser-replacement.txt"
    replacement.write_text("replacement browser bytes")
    script = (
        "import sys; from pathlib import Path; import uvicorn; "
        "from junkyard.app import create_app; from junkyard.config import load_settings; "
        "settings=load_settings(Path(sys.argv[1])); "
        "uvicorn.run(create_app(settings), host='127.0.0.1', port=settings.bind_port, "
        "ssl_keyfile=sys.argv[2], ssl_certfile=sys.argv[3], proxy_headers=False, access_log=False)"
    )
    server = subprocess.Popen([sys.executable, "-c", script, str(config_file), str(key), str(certificate)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    new_password = "browser rotated CLI passphrase"
    try:
        with httpx.Client(verify=False, trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 10
            while True:
                assert server.poll() is None, "HTTPS test server exited early"
                try:
                    assert client.head(origin + "/admin/login").status_code == 200
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            result = subprocess.run([node, str(Path(__file__).with_name("browser_admin.mjs"))],
                input=json.dumps({"chrome": chrome, "directory": str(tmp_path), "origin": origin,
                    "source": str(source), "replacement": str(replacement),
                    "browserPassword": BROWSER_PASSWORD, "newPassword": new_password}),
                capture_output=True, text=True, timeout=100)
            assert result.returncode == 0, result.stderr
            assert "logout passed" in result.stdout
            assert client.get(origin + "/auth/check", auth=("admin", CLI_PASSWORD)).status_code == 401
            assert client.get(origin + "/auth/check", auth=("admin", new_password)).status_code == 204
            assert (tmp_path / "admin.png").stat().st_size > 1000
    finally:
        server.terminate()
        try:
            stdout, stderr = server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            stdout, stderr = server.communicate(timeout=5)
    assert BROWSER_PASSWORD.encode() not in stdout + stderr
    assert new_password.encode() not in stdout + stderr
