import pytest

from junkyard.config import ConfigError, Settings, load_settings


@pytest.mark.parametrize("url", [
    "http://files.example.com", "https://user:secret@files.example.com",
    "https://files.example.com/path", "https://files.example.com?key=secret",
    "https://files.example.com#fragment", "https://", "https://[broken",
    "https://files.example.com:99999", "https://files.example.com:0",
    "https://files.example.com/\n", "https://files.example.com\\evil",
])
def test_invalid_public_urls_are_rejected_without_echoing_values(tmp_path, url):
    with pytest.raises(ConfigError) as error:
        Settings(public_url=url, data_dir=tmp_path / "data")
    assert url not in str(error.value)


@pytest.mark.parametrize("extra", [
    'bind_host = "0.0.0.0"', 'bind_host = "localhost"', 'bind_port = 0',
    'bind_port = 65536', 'bind_port = true', 'bind_port = "8000"',
    'password = "do-not-repeat-this-secret"', 'data_dir = 42',
    'data_dir = "../elsewhere"', 'data_dir = "~/personal"',
    'max_file_bytes = 0', 'max_file_bytes = true', 'storage_quota_bytes = -1',
    'max_transfers = 0', 'storage_quota_bytes = 9223372036854775808',
    'cleanup_interval_seconds = 0', 'anonymous_rate = 0', 'anonymous_rate = true',
    'anonymous_rate = nan', 'anonymous_rate = inf', 'anonymous_rate = 1e-320',
    'anonymous_burst = -1', 'auth_failures_per_minute = 0', 'rate_limit_max_ips = 0',
    'trusted_proxies = "*"', 'trusted_proxies = ["not-a-network"]',
])
def test_invalid_settings_fail_clearly(tmp_path, extra):
    config = tmp_path / "invalid.toml"
    config.write_text('public_url = "https://files.example.com"\n' + extra)
    with pytest.raises(ConfigError) as error:
        load_settings(config)
    assert "do-not-repeat-this-secret" not in str(error.value)


def test_configuration_is_relative_to_file_not_cwd(config_file, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = load_settings(config_file)
    assert settings.data_dir == config_file.parent / "data"
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8000
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("text", ["", "broken = [", 'public_url = 123'])
def test_missing_or_malformed_configuration(tmp_path, text):
    config = tmp_path / "invalid.toml"
    config.write_text(text)
    with pytest.raises(ConfigError):
        load_settings(config)
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(tmp_path / "missing.toml")

