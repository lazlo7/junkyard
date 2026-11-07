"""Optional real-browser smoke check, with an isolated project-local profile."""

import os
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest

from junkyard.file_options import FileOptions
from junkyard.files import FileStore


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
