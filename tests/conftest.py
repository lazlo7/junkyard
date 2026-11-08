import pytest

from junkyard.config import Settings
from junkyard.database import Database
from junkyard.security import hash_password

CLI_PASSWORD = "test CLI passphrase: alpha"
BROWSER_PASSWORD = "test browser passphrase beta"


def representation_headers(response):
    """Per-request correlation IDs are intentionally different on GET and HEAD."""
    return {key: value for key, value in response.headers.items() if key != "x-request-id"}


class FakeClock:
    def __init__(self, now=1700000000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(scope="session")
def hashes():
    return hash_password(CLI_PASSWORD), hash_password(BROWSER_PASSWORD)


@pytest.fixture
def settings(tmp_path):
    return Settings(public_url="https://files.example.com", data_dir=tmp_path / "data")


@pytest.fixture
def database(settings, hashes):
    database = Database(settings.data_dir)
    database.initialize(*hashes)
    return database


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "junkyard.toml"
    path.write_text('public_url = "https://files.example.com"\ndata_dir = "data"\n')
    return path

