"""Explicit, non-secret application configuration."""

from dataclasses import dataclass
from pathlib import Path
import tomllib
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """An actionable configuration error, safe to display to the operator."""


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    def __post_init__(self) -> None:
        if not isinstance(self.public_url, str):
            raise ConfigError("public_url must be an HTTPS origin")
        try:
            url = urlsplit(self.public_url)
            port = url.port
        except ValueError:
            raise ConfigError("public_url must be a valid HTTPS origin") from None
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or "?" in self.public_url
            or "#" in self.public_url
            or "\\" in self.public_url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.public_url)
            or port == 0
        ):
            raise ConfigError("public_url must be an HTTPS origin without credentials, path, query, or fragment")
        object.__setattr__(self, "public_url", self.public_url.rstrip("/"))
        if not isinstance(self.data_dir, Path) or not self.data_dir.is_absolute():
            raise ConfigError("data_dir must be an absolute filesystem path")
        if ".." in self.data_dir.parts:
            raise ConfigError("data_dir must not contain '..' components")
        if self.bind_host not in ("127.0.0.1", "::1"):
            raise ConfigError("bind_host must be 127.0.0.1 or ::1; use an HTTPS reverse proxy")
        if type(self.bind_port) is not int or not 1 <= self.bind_port <= 65535:
            raise ConfigError("bind_port must be an integer between 1 and 65535")


def load_settings(path: Path) -> Settings:
    try:
        with path.open("rb") as config:
            values = tomllib.load(config)
    except OSError:
        raise ConfigError("cannot read configuration file; specify --config PATH") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("configuration file is not valid TOML") from None
    if set(values) - {"public_url", "data_dir", "bind_host", "bind_port"}:
        raise ConfigError("configuration contains unsupported keys")
    if "public_url" not in values:
        raise ConfigError("public_url is required")
    data_dir = values.get("data_dir", "data")
    if not isinstance(data_dir, str) or not data_dir or "\x00" in data_dir or data_dir.startswith("~"):
        raise ConfigError("data_dir must be a nonempty path; home expansion is not supported")
    directory = Path(data_dir)
    if not directory.is_absolute():
        directory = path.absolute().parent / directory
    return Settings(
        public_url=values["public_url"],
        data_dir=directory,
        bind_host=values.get("bind_host", "127.0.0.1"),
        bind_port=values.get("bind_port", 8000),
    )

