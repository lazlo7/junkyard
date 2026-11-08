# Junkyard architecture

Status: approved by the project owner on 2026-11-07. Milestones 1–5 are complete. Milestone 6 is implemented and awaiting review; no live deployment has been performed.

This design builds on [requirements.md](requirements.md). Development milestones and acceptance criteria are in [implementation-plan.md](implementation-plan.md).

## Agreed behavior

- One administrator; publicly readable files accessible by possession of their links.
- Python with FastAPI, running on one Linux host behind an HTTPS reverse proxy.
- Local file storage with SQLite metadata.
- Separate passwords for browser administration and curl.
- HTTP Basic authentication for curl, supporting password prompts and protected configuration files.
- Random URL-safe IDs: **6 characters by default**, **32 characters when requested**.
- Files expire after 30 days by default; explicit lifetimes and permanent files are supported.
- Replacement preserves the link, filename, and expiration unless metadata changes are explicitly requested.
- Expiration blocks new requests immediately; active downloads may finish.
- Interrupted uploads restart. Downloads support byte ranges.
- Shared links automatically show a viewer in browsers and return bytes to curl.
- Anonymous requests are limited per IP to one per second, with a burst of ten.

Six-character links are **unlisted public links, not a confidentiality guarantee**. They provide 36 bits of randomness. Long links provide 192 bits and stronger resistance to guessing, but anyone possessing either kind can read the file. Neither kind appears in a public directory.

## Application architecture

```mermaid
flowchart LR
    C[Browser or curl] --> P[HTTPS reverse proxy]
    P --> A[FastAPI / Uvicorn]
    A --> D[(SQLite metadata)]
    A --> F[Private file storage]
    A --> T[Server-rendered pages]
```

### Runtime and dependencies

Use one Uvicorn worker, with bounded concurrent transfers. Run synchronous database and filesystem operations outside the async event loop. FastAPI supports the required [streaming responses](https://fastapi.tiangolo.com/advanced/custom-response/) and [Jinja2 templates](https://fastapi.tiangolo.com/advanced/templates/).

Direct runtime dependencies are FastAPI, Uvicorn, Jinja2, and `argon2-cffi`. Use Python's `sqlite3` interface and explicit SQL, without an ORM. Use small amounts of browser JavaScript for file uploads, CSRF-protected requests, and copying share links. No frontend build system.

Organize code around HTTP handlers, authentication, file operations, persistence, and templates. Both browser and CLI handlers call the same file-operation functions. Avoid generic storage backends, plugin systems, or repository interfaces.

These dependencies provide established HTTP, templating, and password-hashing implementations. SQLite avoids custom metadata indexing and transaction logic; local blob storage keeps file transfers simple. A separate SPA or distributed service architecture is unnecessary for the intended workload.

### Persistence and concurrent access

SQLite stores file metadata, credential hashes, sessions, and a schema version. File records include the public ID, internal blob reference, display filename, size, timestamps, expiration, and revision.

- Generate internal storage names independently of supplied filenames and public IDs.
- Stream uploads into private temporary files on the same filesystem as final storage.
- Publish complete, immutable blobs before committing their metadata.
- Replace files by switching the database reference, never overwriting an active blob.
- Reject an update with `409 Conflict` if the target changed or disappeared during its upload.
- Pin blobs during downloads so replacement and cleanup cannot break an active response.
- Use short database transactions; never hold a transaction throughout a transfer.

A cleanup task runs every minute by default. It removes expired records and
unreferenced blobs, deferring blobs used by active downloads or uploads. Startup
reconciliation validates all referenced content before removing abandoned
uploads or orphans. Missing or damaged referenced blobs produce an operational
error, not silent metadata deletion. Failed unlinks stay charged to quota and
are retried. Shutdown waits for running cleanup before releasing the storage lock.

Milestone 2 implements immutable publication, revision checks, download pins,
immediate best-effort removal of unreferenced blobs after normal operations, and
quota accounting for all staged and stored content. Milestone 3 adds coordinated
startup and periodic reconciliation. A filesystem lock enforces the single-worker
boundary. Existing credential-only databases upgrade through the explicit local
`junkyard migrate` command. Milestone 5 adds hashed browser sessions in schema
version 3; both schema 1 and 2 upgrade explicitly without changing files or
credentials. The server refuses earlier schemas until migration.

A filesystem operation and a database transaction do not commit atomically. Publishing immutable blobs before metadata, followed by reconciliation, gives this boundary an explicit failure model. The design accepts temporary extra disk usage to make interrupted uploads and replacements safe.

### Deployment

Provide a documented Nginx configuration and systemd service example. Run the application as an unprivileged account on a loopback address; only the proxy accepts public traffic.

Use a configured canonical public URL when generating links. Trust forwarded headers only from the configured proxy. The proxy must never serve the private storage directory directly.

Milestone 6 supplies a dedicated [Nginx configuration](../deploy/nginx.conf),
[systemd unit](../deploy/junkyard.service), and [production settings](../deploy/junkyard.toml).
The proxy overwrites the client IP, streams requests/responses without buffering
or caching, uses the same byte limit as the application, and disables retries.
The service has a dedicated unprivileged account, read-only code/configuration,
private state storage, and no capabilities. TLS keys belong only to the proxy.
See the [deployment guide](deployment.md) for installation and host validation.

Request logging records only generated correlation IDs, fixed operation labels,
normalized methods, status, duration, and completion outcome. Responses return
the application ID as `X-Request-ID`. Uvicorn uses a private formatter that omits
arbitrary messages and exception tracebacks. Nginx records only time, IDs, status,
and duration; its unredacted error log is suppressed because it can contain URLs.
This sacrifices detailed production exception/proxy text to preserve link and
credential privacy. Debugging uses safe events and reproduction with synthetic
data. Automatic slash redirects are disabled to prevent HTTP redirects behind
TLS termination while forwarded-scheme parsing remains disabled.

Backups use a quiescent, complete copy of the data directory, with checksums and
configuration/version information retained alongside it. Restore occurs into a
fresh directory, with integrity checks and a loopback verification instance
before cutover. Restored sessions are revoked through browser-password reset;
CLI credentials may also need resetting if a snapshot predates their rotation.
See [backup and restore](backup-restore.md). No online backup service, dependency,
or schema change is introduced in milestone 6.

## HTTP interfaces

Milestone 1 adds a read-only `GET /auth/check` endpoint for verifying CLI Basic
authentication before file operations exist. It returns `204` on success and a
generic `401` challenge on authentication failure. This endpoint accepts only the
CLI credential; it does not create a browser session. Milestone 2 implements file
creation, replacement, metadata edits, deletion, and basic attachment downloads.
Milestone 3 adds HEAD and single byte ranges to all download routes. The table
below includes milestone 4's HTML negotiation and previews.

| Operation | Interface | Behavior |
|---|---|---|
| Create | `PUT /u/{filename}` | Store request body and generate a new link |
| Create without filename | `PUT /u` | Support stdin; use the ID as the fallback filename |
| Read | `GET /{id}` | HTML viewer when explicitly accepted; otherwise bytes |
| Inspect headers | `HEAD /{id}` | Match the selected GET representation without its body |
| Raw content | `GET /{id}/raw` | Always bytes; safe media may render inline |
| Download | `GET /{id}/download` | Always an attachment with a sanitized filename |
| Replace | `PUT /{id}` | Atomically replace contents |
| Edit metadata | `PATCH /{id}` | Change filename or expiration without re-uploading |
| Delete | `DELETE /{id}` | Immediately deny subsequent reads and schedule cleanup |

Downloads support closed, open-ended, and suffix byte ranges with `206` and
`Content-Range`; invalid or unsatisfiable byte ranges return `416`. Multi-ranges
and unknown units receive a full `200`, and HEAD ignores Range. A strong ETag
derived from immutable blob identity and revision supports `If-Range` without
exposing the storage filename. Date-based or weak validators receive a full
response because multiple modifications may occur within one second.

All CLI mutations require Basic authentication. Browser administration uses separate `/admin/` routes with session and CSRF checks; cookies do not authorize CLI mutation routes.

Parameters:

- `s=1`: generate a long link, creation only.
- `e=30d`: lifetime; accept positive integer durations with `h` or `d`.
- `e=0`: never expire.
- `name=...`: explicit display filename on creation or modification.

Creation defaults to 30 days from successful publication. An explicit expiration update counts from the successful update; omission preserves the existing deadline. Expired files cannot be revived through modification.

Creation returns `201 Created`, a `Location` header, and the shared URL followed by a newline. Successful updates return the same URL; deletion returns `204`. Clients requesting JSON receive metadata instead of the plain URL. Errors use structured JSON for headless clients and HTML for viewer requests.

Example commands:

```sh
curl -u admin -T photo.jpg https://example.com/u/
curl -u admin -T photo.jpg 'https://example.com/u/photo.jpg?s=1&e=7d'
curl -u admin -T replacement.jpg https://example.com/ID
curl -u admin -X PATCH 'https://example.com/ID?e=0'
curl -u admin -X DELETE https://example.com/ID
```

The curl integration test confirms that curl appends the local filename when the
upload URL ends with `/` and has no query string. With query parameters, provide
the filename explicitly in the path or through `name=`; otherwise the generated
ID becomes the display name. The supplied name is display metadata, never a
storage path. A protected, service-specific curl configuration file supports
unattended use. See the [curl documentation](https://curl.se/docs/manpage.html).

The proposed secret-header-name authentication is replaced by Basic authentication. curl treats `-H 'secretkey:'` as removing that header, header names are case-insensitive, and secret header names may escape credential redaction in logs.

## Viewer and admin pages

The viewer displays the ID, filename, size, creation/modification timestamps, expiration, preview, download button, and share button.

Preview escaped text, common raster images, and browser-supported audio/video. Limit text previews to 256 KiB with a truncation notice. HTML, SVG, PDF, and unsupported formats download as attachments. Never trust a supplied content type to enable active content.

The admin panel provides a paginated file list, storage usage, upload/replacement, filename and expiration editing, deletion, and CLI-password rotation. Changing a file's link type requires creating a new upload and deleting the old one; v1 has no aliases or multiple links per file.

Automatic browser-versus-curl responses use `Accept` negotiation, not user-agent detection. Explicit raw and download routes provide predictable alternatives. Restricted previews avoid executing uploaded active content on the admin application's origin.

Milestone 4 implements autoescaped Jinja2 templates and two packaged assets,
without a frontend build. HTML must be explicitly accepted with a positive
quality at least as high as bytes, JSON, or plain text; wildcard-only requests
keep byte behavior. HTML HEAD renders the same bounded page to match its headers;
HTML ignores byte ranges and does not reuse the blob's validator.

Preview inspection uses the pinned immutable blob outside the event loop.
Common text/code extensions and extensionless UTF-8 files render as escaped plain
text, with a 256 KiB limit and a truncation notice. Media requires an allowlisted
extension and matching bounded container signature: PNG/JPEG/GIF/WebP,
MP3/WAV/FLAC/Ogg, MP4/M4V/M4A, and WebM. This identifies containers rather than
validating codecs; browser support determines playback. No upload Content-Type
is trusted. Unsupported and active document formats remain octet-stream
attachments, including on `/raw`. Byte responses add a sandbox CSP, while viewer
pages allow only local scripts, styles, images, and media and prohibit framing.
Canonical share links remain selectable if JavaScript or clipboard access fails.

Milestone 5 implements `/admin/login` and a paginated `/admin/` dashboard with
25 live files per page. Its raw-body upload and replacement routes call the same
streaming handler as CLI uploads; metadata edits and deletion share FileStore
operations. The storage summary takes a snapshot under the quota lock and counts
all charged content. Forms use one additional fixed JavaScript asset, with
`connect-src 'self'` allowed only on admin pages. No new dependency is introduced.

Browser sessions use 256-bit random tokens, SHA-256 token hashes in SQLite,
credential versions, idle timestamps, and fixed absolute deadlines. They survive
restart. Logout deletes the current record; local browser-password reset changes
the version and deletes all sessions transactionally. Login always issues a new
token. Successful login prunes stale records and limits retained sessions to 32,
revoking the oldest if needed. Assets do not renew idle time. Long browser uploads
recheck session validity before publication; already authorized operations may
finish during concurrent logout/reset.

Every browser mutation checks the configured canonical Origin and a CSRF header
derived with HMAC from the session secret before reading its body. The secret is
never embedded in HTML. Pre-login requests use a separate ten-minute signed
challenge cookie and bound CSRF value; this challenge is invalidated on restart.
Both cookies use `__Host-`, Secure, HttpOnly, SameSite=Strict, and Path=/ without
a Domain attribute. Browser credentials cannot authorize CLI routes, and Basic
credentials cannot authorize admin routes. Password bodies are bounded JSON.
CLI-password rotation verifies the browser password again and checks the session
and credential versions in the update transaction, preventing a reset during
verification from issuing a session or overwriting newer CLI credentials.

## Security and operational defaults

The following are approved defaults, configurable where appropriate:

- **Limits:** 250 MiB per file, 100 GiB storage quota, and four simultaneous transfers. Count temporary uploads and obsolete blobs toward disk usage; reject excess work cleanly.
- **Credentials:** salted Argon2id hashes; passwords never expire automatically. Local administrative commands initialize and reset credentials through hidden prompts. No default password or public setup endpoint. See [OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
- **Sessions:** random opaque tokens, hashed in SQLite; `Secure`, `HttpOnly`, host-only cookies with `SameSite=Strict`. One-hour idle timeout and 12-hour absolute lifetime. Browser-password changes revoke all browser sessions; CLI-password changes invalidate only the old CLI credential.
- **CSRF:** tokens and origin checks for browser mutations, including login protection. Require the browser password again before rotating the CLI password.
- **Authentication throttling:** separate limits for failed login/Basic authentication, plus bounded password-verification concurrency. Anonymous throttling applies to file/viewer requests, including HEAD; fixed application assets are exempt. Return `429` with `Retry-After`.
- **Path safety:** validate ID syntax; reject traversal, separators, control characters, and malformed filename encoding. Treat filenames strictly as metadata. Keep storage private and refuse symlink-based access.
- **Content safety:** escaped templates, restrictive CSP, `nosniff`, and no third-party assets. Disable cross-origin API access.
- **Privacy:** `Cache-Control: no-store`, `Vary: Accept` on negotiated responses, and `Referrer-Policy: no-referrer`. Unknown, expired, and deleted links return the same public `404`.
- **Logs:** record operation, status, duration, and internal correlation identifiers; omit passwords, cookies, filenames, and shared URLs from application and proxy access logs.
- **Recovery:** provide an offline backup/restore procedure covering both SQLite and blobs. Deletion and expiration do not erase existing backups or previously downloaded copies.

Milestone 3 uses two bounded per-IP token-bucket maps. Anonymous GET/HEAD requests
default to one request per second with a burst of ten, including unknown-path
probes. Valid CLI authentication bypasses that read limit. Failed CLI
authentication defaults to five attempts per minute with a burst of five;
checks reserve a token before hashing and refund it on success or backend error.
Throttle responses use `429` with `Retry-After`. Milestone 5 shares this
authentication budget and the two-operation Argon2 semaphore with browser login
and CLI-password rotation, preventing bypass by switching authentication paths.
Live browser sessions also bypass anonymous read limits. Only the three exact
packaged asset paths are exempt without authentication; anonymous viewer and
media requests remain limited.

Each limiter tracks up to 10000 IPs by default. Only idle, replenished entries
can be evicted; at capacity, new IPs are temporarily rejected. Limits use a
monotonic clock and reset on restart. Client IPs come from the socket peer unless
it matches an explicit trusted proxy address/network. The single trusted proxy
must overwrite `X-Forwarded-For` with exactly one valid IP; missing, repeated,
invalid, or chained values from trusted peers are rejected. Other forwarded
headers are ignored. Uvicorn's separate proxy-header parsing remains disabled.

This is an explicit single-worker design; multiple application workers would
require revisiting coordination. The burst allowance accommodates page loads
and media requests, but per-IP throttling does not prevent distributed guessing
and may affect users sharing an IP.

## V1 boundaries

V1 excludes resumable uploads, multiple administrators, reader accounts, version history, deduplication, transcoding, public search, and distributed storage. All originally requested browser functionality remains included.
