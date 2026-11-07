# Junkyard

A personal, self-hosted, CLI-centric file server. The approved design is in
[docs/architecture.md](docs/architecture.md), with milestones in
[docs/implementation-plan.md](docs/implementation-plan.md).

Milestone 1 provides configuration, a private SQLite credential database, local
password administration, and an HTTP Basic authentication check. File operations,
browser pages, sessions, and request throttling belong to later milestones.

## Development setup

Use Python 3.12 or newer. This project is developed with Python 3.12.3 and the
existing `.venv`, with uv installed inside it. No newer Python is required.

```sh
.venv/bin/uv sync --locked --no-python-downloads
cp junkyard.example.toml junkyard.toml
```

Edit `junkyard.toml` to set the external HTTPS origin. It contains no passwords.
Relative `data_dir` paths are relative to this file. Its parent directory must
already exist; the data directory itself will be created privately by `init`.
Unknown configuration keys, invalid types, non-HTTPS public origins, URLs with
credentials or paths, and non-loopback bind addresses are rejected.

The runtime dependencies are FastAPI, Uvicorn, and `argon2-cffi`. Python's standard
library supplies configuration parsing and SQLite access. pytest and httpx are
development dependencies. Jinja2 will be added with the viewer milestone.
`uv.lock` records the resolved versions; use `uv sync --locked` to reproduce them.

## Initialize and recover credentials

```sh
.venv/bin/uv run --locked junkyard init
.venv/bin/uv run --locked junkyard passwd cli
.venv/bin/uv run --locked junkyard passwd browser
```

All password commands require an interactive terminal and hidden, confirmed
prompts. They refuse to fall back to echoed input. Passwords must be distinct for
CLI and browser access, contain at least 12 printable characters, and fit within
1024 UTF-8 bytes. Spaces, Unicode, and colons are supported. There are no default
passwords, password arguments, password environment variables, or automatic
expiration. The reset commands do not require the old password; local ownership
of the private database is the recovery authority.

Initialization publishes both Argon2id hashes together and refuses to overwrite
an existing database. The data directory must be owned by the running user with
mode `0700`; `metadata.sqlite3` must have mode `0600`. Symlinks and hard-linked
database files are rejected. Existing permissions are checked, never silently
changed. The configured directory and its parents must be controlled by the
operator; protection against a hostile process running as the same OS user is
outside this boundary.

Each reset updates only the selected credential and increments its version.
The running server reads the current CLI hash on every authentication attempt,
so an old CLI password stops working after reset. Browser session revocation will
use the browser credential version when sessions are implemented in milestone 5.

For an alternative configuration file, put the global option before the command:

```sh
.venv/bin/uv run --locked junkyard --config /path/to/junkyard.toml init
```

## Run and check authentication

```sh
.venv/bin/uv run --locked junkyard serve
```

The server refuses to start without both initialized credentials. It runs one
worker and binds to `127.0.0.1:8000` by default; `::1` is also supported. HTTP access
logging and proxy-header trust are disabled. Public exposure requires the HTTPS
reverse proxy described by the architecture; deployment configuration and trusted
proxy integration are planned for milestone 6.

The read-only `GET /auth/check` endpoint returns `204` for the fixed username
`admin` and the CLI password. It returns a generic `401` challenge for missing,
malformed, or incorrect credentials. The browser password and cookies do not
authorize it. A protected request returns `503` if the credential store is
unavailable or both password-verification slots are occupied. Responses disable
caching. Public setup and generated API documentation endpoints are disabled.

For a local smoke check, curl prompts for the CLI password:

```sh
curl -q -i -u admin http://127.0.0.1:8000/auth/check
```

Use HTTPS for requests over a network. For unattended access, create a private
curl configuration file with mode `0600` using an editor; do not put its password
in a shell command. Its contents follow curl's config syntax:

```text
user = "admin:YOUR_CLI_PASSWORD"
```

Then invoke it without exposing the password in arguments or shell history:

```sh
curl -q -i --config /path/to/private-curl.conf https://files.example.com/auth/check
```

`-q` suppresses curl's implicit user configuration. Keep credential files outside
version control and use them only with the intended service. The application
does not accept HTTP Basic credentials through query parameters.

## Tests and packaging

```sh
mkdir -p .cache/tmp
TMPDIR="$PWD/.cache/tmp" UV_CACHE_DIR="$PWD/.cache/uv" \
  .venv/bin/uv run --locked --offline pytest --basetemp=.cache/pytest
.venv/bin/uv build --offline
```

The tests use synthetic credentials and project-local temporary directories.
They cover configuration failures, private filesystem permissions, initialization
failure, password reset, credential separation, malformed Basic headers,
verification concurrency, real hidden terminal prompts, and a real Uvicorn/curl
exchange using a password file. Integration tests require Linux pseudo-terminals,
loopback sockets, and curl. A restrictive sandbox may need to permit those
capabilities to run the tests.

Local configuration, the default data directory, virtual environments, caches,
and build artifacts are ignored by Git. Keep any custom data directory and
credential-file location out of version control as well.

