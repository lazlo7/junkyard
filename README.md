# Junkyard

A personal, self-hosted, CLI-centric file server. The approved design is in
[docs/architecture.md](docs/architecture.md), with milestones in
[docs/implementation-plan.md](docs/implementation-plan.md).

Milestones 1–5 provide configuration, private SQLite storage, password
administration, Basic authentication, streaming CLI file operations, expiration
cleanup and recovery, HEAD/range downloads, request throttling, and a public
browser viewer, and secured browser administration. Deployment and release
checks remain milestone 6.

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

Existing milestone 1–4 databases require a schema upgrade. Stop the server and
back up the data directory, then run:

```sh
.venv/bin/uv run --locked junkyard migrate
```

The upgrade transactionally adds the missing tables and preserves existing
files and both credentials. Milestone 5 uses schema version 3 for browser sessions.
Running it again is harmless. New `init` commands create the current schema
directly; the server refuses an old schema until it is migrated.

Startup recovery now removes abandoned staging files and unreferenced blobs
after verifying all referenced content. It also processes expired files.

The runtime dependencies are FastAPI, Uvicorn, Jinja2, and `argon2-cffi`. Python's standard
library supplies configuration parsing and SQLite access. pytest and httpx are
development dependencies. Jinja2 supplies escaped, server-rendered HTML;
its MarkupSafe dependency provides HTML escaping. No frontend build is required.
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
so an old CLI password stops working after reset. Resetting the browser password
revokes every browser session in the same transaction. CLI-password changes do
not revoke browser sessions.

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
logging and Uvicorn's implicit proxy-header handling are disabled. The application
only trusts client-address headers from explicitly configured proxy peers (see
below). Public exposure requires the HTTPS reverse proxy described by the
architecture; complete deployment examples remain milestone 6.

The read-only `GET /auth/check` endpoint returns `204` for the fixed username
`admin` and the CLI password. It returns a generic `401` challenge for missing,
malformed, or incorrect credentials. The browser password and cookies do not
authorize it. A protected request returns `503` if the credential store is
unavailable or both password-verification slots are occupied. Responses disable
caching. Public setup and generated API documentation endpoints are disabled.

For a local smoke check with the default `trusted_proxies = []`, curl prompts for
the CLI password:

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
- `/{id}` negotiates a browser viewer or file bytes; `/{id}/raw` always returns
  bytes, with allowlisted media inline; `/{id}/download` always forces attachment.

Files that have expired are inaccessible and cannot be revived with an update.
New reads fail at the deadline; already-started downloads can finish. No public
listing is exposed. Short links are unlisted public links;
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
is retained and still charged to quota. Startup verifies every referenced blob
before deleting anything. Missing, wrong-sized, linked, or unexpectedly named
storage entries fail closed without discarding metadata. Valid startup recovery
removes abandoned staging files and unreferenced blobs, preserving committed data.

Every `cleanup_interval_seconds` (default 60), cleanup expires metadata and
reclaims unreferenced content. The same operation lock protects active uploads,
download pins, and quota reservations. Failed deletions stay charged and are
retried; cleanup errors are logged without file paths. Shutdown waits for any
running cleanup before releasing the process lock. Do not manually modify
storage while serving files.

## Public browser viewer

Open a shared link in a browser to see a preview, ID, filename, byte size,
upload/modification times, expiration, and download/share controls. Times are
shown in UTC; permanent files show “Never”. Copying uses the configured canonical
`public_url`, never the request's Host header. If clipboard access fails or
JavaScript is unavailable, the share URL remains selectable and the download
link still works.

`/{id}` selects HTML only when `Accept` explicitly includes `text/html` with a
positive quality at least as high as bytes, JSON, or plain text. Wildcards alone,
missing Accept, and normal curl/wget requests return bytes. User-Agent is not
used. Negotiated responses include `Vary: Accept`. Explicit `/raw` and `/download`
routes return bytes even when HTML is accepted.

Preview policy:

- UTF-8 text with common text/code extensions (or no extension) is displayed as
  escaped plain text, including Markdown. Reads are bounded to 256 KiB plus one
  truncation-detection byte; a notice identifies truncated previews. Invalid
  UTF-8 or binary control characters disable the preview. Full downloads are
  unchanged.
- PNG, JPEG, GIF, and WebP images; MP3, WAV, FLAC, Ogg audio; MP4/M4V, WebM, and
  Ogg video; and M4A audio have previews when their extensions and bounded
  container signatures agree. `/raw` supplies the explicit media MIME type and
  inline disposition. Browser codec support determines actual playback; files
  are not transcoded or fully decoded by the server.
- HTML, SVG, PDF, other formats, and mismatched media signatures show a download
  message. Their raw/download responses use `application/octet-stream` and
  attachment disposition. An upload's supplied Content-Type cannot enable a
  preview.

Pages use autoescaped Jinja2 templates, a restrictive CSP, `nosniff`, no-store,
no-referrer, and fixed local CSS/JavaScript. Uploaded bytes have a sandbox CSP as
additional protection. No uploaded HTML, SVG, PDF, script, or stylesheet is
served as active content. Only the fixed application asset paths are exempt
from anonymous rate limits; viewer and media requests consume the usual budget.

Viewer GET/HEAD failures return HTML when requested, including missing links and
rate limits. Headless requests and explicit raw/download errors return JSON.
Status codes and useful headers such as `Retry-After`, `WWW-Authenticate`, and
`Content-Range` are preserved; HEAD never has a response body.

## Browser administration

Open `https://files.example.com/admin/` through the configured HTTPS origin and
sign in with the **browser** password. HTTP Basic credentials do not authorize
admin routes, and browser cookies do not authorize CLI mutations. `public_url`
must match the origin you use, including any non-default port. Access through a
different hostname or plain HTTP will not support login; Secure cookies and
Origin checks are always enabled.

The dashboard provides:

- A paginated list of live files, 25 per page, with viewer/download links and UTC
  timestamps. Expired files disappear immediately from the list.
- Storage usage using the same accounting as uploads: staging, obsolete pinned
  blobs, and files awaiting cleanup still count toward quota.
- Upload with optional filename, lifetime, and long link. “Edit or replace” lets
  you change metadata or select replacement contents. An omitted lifetime
  preserves the existing expiration; a supplied lifetime starts from the update.
- Confirmed deletion and sign-out.
- CLI-password rotation with the current browser password and confirmation of
  the new CLI password. The two passwords must remain different.

JavaScript is required for login and mutations. It sends raw file bodies to
`PUT /admin/files` or `PUT /admin/files/{id}` and uses PATCH/DELETE for other file
operations, reusing the CLI storage logic and limits. No multipart parser or
frontend build is needed. After a long upload, the session is rechecked before
publication; a session that expired or was revoked during transfer is rejected.
Operations already authorized and committing can finish during a concurrent
logout/reset, as can active downloads.

Sessions use independent random 256-bit bearer tokens; only SHA-256 hashes and
timestamps are stored in SQLite. Cookies are host-only, `Secure`, `HttpOnly`,
`SameSite=Strict`, and use the `__Host-` prefix. A session expires after one hour
without authenticated activity or twelve hours from login, whichever comes
first. Fixed assets do not extend idle time. Sessions survive server restarts;
logout revokes the current session and local browser-password reset revokes all.
At most 32 sessions are retained; a further successful login revokes the oldest.
Expired records are pruned on successful login, keeping persistent state bounded.

All browser mutations require the configured Origin and a session-bound CSRF
header before reading the body. Login uses a separate signed, ten-minute
challenge and HttpOnly cookie; restarting the server invalidates open login
pages, so reload them. Authentication JSON bodies are limited to 8 KiB. Passwords
are never accepted in query parameters. Login and CLI-password rotation share
the per-IP authentication budget and Argon2 concurrency limit with CLI Basic
checks. CSRF protection follows the
[OWASP guidance on custom headers and Origin checks](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## HEAD and resumable downloads

All three file routes support HEAD; their byte representations support single ranges:

```sh
curl -I https://files.example.com/ID
curl --range 0-1023 https://files.example.com/ID -o first-kib.bin
curl -C - https://files.example.com/ID -o partially-downloaded.bin
```

HEAD returns the same headers as a full GET, without its body. A satisfiable
single range returns `206` with `Content-Range`; malformed or unsatisfiable byte
ranges return `416` with the full size. Open-ended and suffix ranges work.
Multi-range requests and unknown range units fall back to a full `200` response;
Range is ignored on HEAD. This follows the selective range support permitted by
[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-range).

Byte responses include a strong `ETag` and `Last-Modified`. Send the saved ETag as
`If-Range` when resuming a potentially replaced file: a matching tag allows the
range; a changed tag returns the complete new file. Weak tags and date-based
`If-Range` values also return the full file because multiple changes can occur
within one second. `curl -C -` alone does not check that a file is unchanged.
Responses remain `Cache-Control: no-store`.
Negotiated HTML ignores Range and has no blob ETag. Its HEAD response matches
the complete viewer's headers, including the rendered page's Content-Length.

## Request limits and trusted proxies

Anonymous GET/HEAD requests share a per-IP token bucket, including missing-file
and invalid-path probes. `anonymous_rate` defaults to one request per second and
`anonymous_burst` to ten. Verified CLI authentication or a live browser session
bypasses this read limit;
merely supplying an Authorization header does not. Incorrect supplied credentials
return `401`, even on otherwise public reads.

Authentication has a separate bucket shared by CLI checks, browser login, and
CLI-password rotation. `auth_failures_per_minute` defaults to
five and controls both burst size and sustained refill. Each check reserves a
token before password hashing; success or backend failure refunds it, while
missing, malformed, or incorrect credentials consume it. After five failures,
the next token arrives in 12 seconds. Concurrent checks cannot exceed the
available budget; Argon2 verification retains its separate two-operation limit.

Throttled requests return `429`, `Retry-After` in whole seconds, and `no-store`.
Each bucket map tracks at most `rate_limit_max_ips` addresses (default 10000).
Idle entries expire only after their budget can fully refill; active blocked
entries are not evicted to admit new addresses. At capacity, new addresses are
temporarily rejected. Rate-limit state resets on restart; stored sessions retain
their original deadlines.

By default, client identity comes from the socket peer and all forwarded headers
are ignored. Behind your single reverse proxy, set `trusted_proxies` to its exact
IP addresses or trusted networks, for example `["127.0.0.1", "::1"]`. The proxy
must **overwrite** `X-Forwarded-For` with exactly one client IP; for Nginx:

```nginx
proxy_set_header X-Forwarded-For $remote_addr;
```

Requests from a trusted proxy with missing, invalid, repeated, or chained values
are rejected with `400`. `Forwarded` and `X-Real-IP` are not used. IPv4-mapped IPv6
client addresses are normalized to the same IPv4 identity. With proxy trust
enabled, perform smoke checks through the HTTPS proxy; a direct request from a
trusted loopback peer also needs its required client-address header. Continue to
use `junkyard serve`, which disables Uvicorn's separate proxy-header processing.

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
and failures around publication/commit. Fake-clock checks cover expiration and
rate limits; recovery checks cover referenced blobs, active transfers, and failed
cleanup. A real Uvicorn/curl exchange covers the file lifecycle, HEAD, ranges,
resuming a download, and a chunked stdin upload using a password file. A process
crash test verifies cleanup after an interrupted upload. Viewer checks cover
escaping, bounded text reads, MIME spoofing, media ranges, negotiation, asset
throttle exemptions, and error responses. If Chrome/Chromium is installed, an
optional headless smoke test renders the viewer with an isolated project-local
profile and checks that its share script loads. It is skipped otherwise.
With Chrome/Chromium, Node 22+, and OpenSSL installed, a second optional browser
test exercises login, upload, metadata editing, replacement, CLI-password
rotation, deletion, and logout over loopback HTTPS. It uses a synthetic test
certificate, isolated profile, and Node's built-in WebSocket; no npm packages
are needed. Session tests cover idle/absolute expiry, restart persistence,
reset races, CSRF rejection before body reads, bounded state, and migration
rollback. These optional tools are test tools, not application dependencies.
Integration tests require Linux pseudo-terminals,
loopback sockets, and curl. A restrictive sandbox may need to permit those
capabilities to run the tests.

Local configuration, the default data directory, virtual environments, caches,
and build artifacts are ignored by Git. Keep any custom data directory and
credential-file location out of version control as well.

