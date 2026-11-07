# Junkyard

A personal, self-hosted, CLI-centric file server. The approved design is in
[docs/architecture.md](docs/architecture.md), with milestones in
[docs/implementation-plan.md](docs/implementation-plan.md).

Milestones 1–2 provide configuration, private SQLite storage, password
administration, Basic authentication, and streaming CLI file operations.
Scheduled cleanup/recovery, HEAD/range handling, request throttling, browser
pages, and sessions belong to later milestones.

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

Existing milestone 1 databases require a schema upgrade. Stop the server and
back up the data directory, then run:

```sh
.venv/bin/uv run --locked junkyard migrate
```

The upgrade adds file metadata in a transaction and preserves both credentials.
Running it again is harmless. New `init` commands create the current schema
directly; the server refuses an old schema until it is migrated.

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
worker, enforced by a storage lock, and binds to `127.0.0.1:8000` by default; `::1` is also supported. HTTP access
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

## Manage files with curl

These examples use password prompting; the protected curl configuration above
can be used instead. Uploads are raw bytes, not multipart forms.

```sh
# Default short link, original basename, and a 30-day lifetime.
curl -u admin -T photo.jpg https://files.example.com/u/

# Long link with a seven-day lifetime and an explicit filename.
curl -u admin -T photo.jpg 'https://files.example.com/u/photo.jpg?s=1&e=7d'

# Download, replace contents, edit metadata, and delete using the returned ID.
curl https://files.example.com/ID -o photo.jpg
curl -u admin -T replacement.jpg https://files.example.com/ID
curl -u admin -X PATCH 'https://files.example.com/ID?name=renamed.jpg&e=0'
curl -u admin -X DELETE https://files.example.com/ID
```

curl appends the local filename to `/u/` **only when the URL has no query string**.
When adding options, include the filename in the path as above or supply `name=`.
`/u` and `/u/` also accept unnamed uploads and stdin (`-T -`); their display name
defaults to the generated ID. Query strings and explicit path names must be URL
encoded. Names are validated display metadata and never become storage paths.

Creation returns `201`, a `Location` header, and the shared URL on its own line.
Replacement and metadata edits return `200` with the same URL; deletion returns
`204`. Add `-H 'Accept: application/json'` to mutations for `id`, `url`, `filename`,
`size`, `created_at`, `modified_at`, `expires_at`, and `revision`. Timestamps are
UTC Unix seconds, and `expires_at` is `null` for permanent files. Errors return
JSON with a `detail` message.

- `s=1` requests a 32-character link at creation; the default is six characters.
- `e=7d` or `e=12h` sets a lifetime from successful creation/update; `e=0` makes
  the file permanent. Expirations beyond year 9999 are rejected.
- `name=` sets the display filename. Omission during replacement preserves the
  existing name; omission of `e` preserves the existing deadline.
- `PATCH` changes metadata only and accepts no request body. Unknown or repeated
  parameters are rejected rather than ignored.
- `/{id}`, `/{id}/raw`, and `/{id}/download` currently serve binary attachments.
  Inline previews and browser-versus-curl negotiation arrive in milestone 4.

Files that have expired are inaccessible and cannot be revived with an update.
Scheduled expiration deletion and recovery of crash leftovers arrive in
milestone 3. No public listing is exposed. Short links are unlisted public links;
use explicit long links for stronger resistance to guessing.

## Storage and concurrent transfers

Configure `max_file_bytes` (default 250 MiB), `storage_quota_bytes` (default
100 GiB), and `max_transfers` (default four) in TOML. Transfers include uploads
and downloads. The quota counts logical content bytes in both `staging/` and
`blobs/`, including obsolete blobs and crash leftovers; SQLite and filesystem
overhead are excluded. Physical disk exhaustion is handled independently.

Uploads stream in bounded chunks and are published only when complete. A
replacement needs quota for both the previous and incoming contents. Metadata
then switches to an immutable new blob; an active download retains its original
bytes even if the file is replaced or deleted. Old blobs are removed when no
download holds them. Deletion denies new reads immediately.

Conflicting replacement or metadata changes return `409` rather than silently
overwriting another update. Oversized files return `413`, exhausted quota or
disk space returns `507`, and exhausted transfer capacity returns `503` with
`Retry-After`. Interrupted uploads restart from scratch and release their staging
space during normal operation.

Publication syncs the file and containing directories before the database commit.
If publication succeeds but a commit fails or its result is uncertain, the blob
is retained and still charged to quota. Startup counts these retained files but
does not delete them yet; reconciliation is milestone 3. Missing or damaged
referenced blobs fail closed. Do not manually modify storage while serving files.

## Tests and packaging

```sh
mkdir -p .cache/tmp
TMPDIR="$PWD/.cache/tmp" UV_CACHE_DIR="$PWD/.cache/uv" \
  .venv/bin/uv run --locked --offline pytest --basetemp=.cache/pytest
.venv/bin/uv build --offline
```

The tests use synthetic credentials and project-local temporary directories.
They cover configuration failures, private filesystem permissions, initialization
and migration failures, credential separation, malformed Basic headers, hidden
terminal prompts, file CRUD, traversal/header injection attempts, identifier
collisions, concurrent updates, quota races, cancellation, partial disk writes,
and failures around publication/commit. A real Uvicorn/curl exchange covers the
file lifecycle, filename handling, and a chunked stdin upload using a password
file. Integration tests require Linux pseudo-terminals,
loopback sockets, and curl. A restrictive sandbox may need to permit those
capabilities to run the tests.

Local configuration, the default data directory, virtual environments, caches,
and build artifacts are ignored by Git. Keep any custom data directory and
credential-file location out of version control as well.

