# Junkyard implementation plan

Status: approved by the project owner on 2026-11-07. Milestones 1–4 are complete. Milestone 5 is implemented and awaiting review; milestone 6 has not started.

The behavior, interfaces, security policies, and operational defaults are defined in [architecture.md](architecture.md), based on [requirements.md](requirements.md).

## Development approach

- Make incremental, focused changes, with automated tests for new functionality.
- Keep each milestone independently reviewable and run relevant tests after modifications.
- Use real HTTP tests against Uvicorn for streaming behavior and curl compatibility, in addition to unit and application-level tests.
- Use controlled clocks for expiration tests and injected failures for persistence recovery tests.
- Discuss significant changes to the approved architecture before implementing them.
- Do not commit changes unless explicitly requested.

## Milestone 1: Foundation and credentials

Status: implemented and committed by the project owner; acceptance tests pass.

Deliver project packaging, configuration, schema initialization, local credential commands, and Basic authentication.

Acceptance criteria:

- Invalid configuration fails clearly.
- No default credentials or public setup endpoint exist.
- Passwords are never stored or logged in plaintext.
- CLI and browser credentials remain separate.
- Local initialization and recovery use hidden password prompts.

Implementation notes:

- Python 3.12.3, the existing `.venv`, and uv project management with `uv.lock`.
- Strict TOML configuration and a versioned SQLite schema containing the two
  credential records; future tables will be added with their respective milestones.
- `junkyard init`, `junkyard passwd cli|browser`, and `junkyard serve` commands.
- A read-only `/auth/check` endpoint tests Basic authentication independently of
  future file operations. Expensive hash checks run outside the event loop with
  at most two concurrent verifications; rate-based throttling is added in milestone 3.
- Passwords require hidden confirmation, at least 12 printable characters, and
  at most 1024 UTF-8 bytes. CLI and browser passwords must differ.
- Automated checks include real terminal echo suppression and Uvicorn/curl
  authentication using a protected password file.

## Milestone 2: Durable CLI file operations

Status: complete; 126 tests passed at this milestone, including Uvicorn/curl integration tests.
Accepted for continuation to milestone 3. The existing Starlette/httpx test-client
deprecation warning remains; runtime dependencies are unchanged.

Deliver streaming creation, read, replacement, metadata edits, deletion, short/long IDs, and quotas.

Acceptance criteria:

- End-to-end curl tests cover creation, download, replacement, metadata edits, and deletion.
- Filename and directory-traversal attacks cannot escape private storage or inject response headers.
- Forced public-ID collisions are handled without overwriting an existing file.
- Interrupted uploads and disk-full failures do not publish incomplete files or damage previous contents.
- Concurrent replacement and deletion obey the conflict policy.
- Injected failures around blob publication and database commit leave recoverable state.
- File-size and storage limits are enforced for streamed bodies.

Implementation notes:

- Schema version 2 adds file metadata. `junkyard migrate` transactionally upgrades
  existing milestone 1 databases without changing credentials.
- The single-worker storage lock coordinates quota, upload publication, revision
  conflicts, and pinned downloads. Staging and retained blobs count toward quota.
- File operations require CLI authentication before reading upload bodies; public
  downloads currently serve attachments with caching and MIME sniffing disabled.
- Interrupted requests remove staging files during normal operation. Uncertain
  commits retain published blobs for recovery rather than risk deleting live data.
- Expiration is stored and checked on access; periodic cleanup, full restart
  reconciliation, HEAD, ranges, and request throttling are added in milestone 3.
- Integration tests verify curl's filename behavior: query-bearing upload URLs
  need an explicit path filename or `name=` to preserve the original basename.

## Milestone 3: Lifecycle and HTTP behavior

Status: complete and accepted for continuation to milestone 4; all 184 tests passed, including Uvicorn/curl
integration and process-crash recovery tests. The existing Starlette/httpx
deprecation warning remains.

Deliver expiration, cleanup, recovery, HEAD, range downloads, and throttling.

Acceptance criteria:

- Fake-clock tests cover default expiration, explicit lifetimes, permanent files, and deadline-preserving replacements.
- Expiration denies new reads immediately while active downloads finish.
- Restart recovery and orphan cleanup preserve referenced and actively used blobs.
- Concurrent operations cannot bypass storage quotas.
- HEAD and range responses are correct, including invalid ranges.
- Forged proxy headers cannot bypass client-IP handling or rate limits.
- Anonymous throttling, authentication throttling, and `Retry-After` behavior are tested.

Implementation notes:

- Startup validates referenced content before deleting expired metadata or crash
  leftovers. Periodic cleanup shares the storage lock and protects active uploads,
  pinned downloads, and quota reservations; failed unlinks are retried.
- Cleanup defaults to 60 seconds and finishes any running work before shutdown.
  Injected clocks test exact expiration boundaries without waiting for real deadlines.
- HEAD and single byte ranges work on all download routes. Strong ETag-based
  `If-Range` prevents mixing versions during a resumed download; multi-ranges and
  weak/date validators fall back to a full response.
- Anonymous reads default to 1 request/second, burst 10; failed CLI authentication
  defaults to 5/minute, burst 5. Bounded per-IP maps and pre-hash reservations
  prevent state growth and concurrent failure-budget bypass.
- Trusted-proxy addresses are explicit; only a single overwritten client IP is
  accepted. Forged headers from other peers cannot change rate-limit identity.
- Real HTTP tests cover curl HEAD, range, and resume behavior. A controlled
  process crash verifies partial-upload cleanup and preservation of committed data.
- No dependencies or database schema changes are required beyond milestone 2.

## Milestone 4: Public viewer

Status: complete and accepted for continuation to milestone 5; all 232 tests passed, including real
Uvicorn/curl negotiation and the isolated Chrome smoke test. Wheel and source
distribution contents were verified, including templates and assets. The
existing Starlette/httpx deprecation warning remains.

Deliver negotiated HTML, safe previews, metadata display, and download/share controls.

Acceptance criteria:

- Browsers and curl receive the correct representations; explicit raw/download routes remain predictable.
- Uploaded HTML and SVG remain non-executable.
- Filenames and displayed text are escaped.
- Supported media previews and video seeking work.
- Text previews are bounded and show truncation clearly.
- Unsupported formats provide a download path.
- Error pages and headless errors have appropriate status codes and formats.

Implementation notes:

- Explicit Accept negotiation selects server-rendered HTML on shared links;
  raw/download routes retain predictable byte responses. HEAD matches the
  selected representation and HTML ignores byte ranges.
- Autoescaped Jinja2 templates show metadata, bounded plain-text previews, and
  allowlisted image/audio/video previews. Media extensions must match bounded
  container signatures; uploaded MIME headers never grant inline treatment.
- HTML, SVG, PDF, and unsupported formats remain attachments. Restrictive CSP,
  nosniff, no-store, and no-referrer apply to viewer responses; bytes add sandbox
  CSP. Preview reads retain download pins and transfer limits.
- Two fixed local assets supply responsive CSS and the copy-link button; they
  are exempt from anonymous read throttling. The canonical link and download
  remain usable without JavaScript. No frontend build is introduced.
- Errors negotiate HTML/JSON while retaining status and relevant headers.
  Real curl tests cover viewer negotiation; an optional isolated Chrome smoke
  test checks rendered escaping and loading of the share script.
- Jinja2 (and its MarkupSafe dependency) implements the approved templating
  choice. No database schema or configuration change is required.

## Milestone 5: Browser administration

Status: implemented and awaiting review; all 308 tests pass, including the full
admin workflow in Chrome over loopback HTTPS. Wheel and source distribution
contents were verified. The existing Starlette/httpx deprecation warning remains.

Deliver sessions, CSRF protection, file management, storage display, and CLI-password rotation.

Acceptance criteria:

- Session expiration, logout, and browser-password-change revocation work.
- CSRF and cross-origin mutation attempts are rejected.
- Browser cookies cannot authorize CLI mutation routes.
- CLI-password rotation invalidates the old CLI password without changing browser credentials.
- A complete browser upload, replacement, metadata-edit, and deletion workflow works.
- The admin list is paginated and storage information reflects the quota policy.

Implementation notes:

- Schema 3 adds hashed session records. Explicit transactional migrations from
  schema 1 or 2 preserve credentials and files; initialization creates schema 3.
- Opaque 256-bit session tokens use host-only Secure/HttpOnly/SameSite=Strict
  cookies, one-hour idle and twelve-hour absolute deadlines, and a 32-session
  cap. Logout revokes one session; browser-password reset revokes all. Successful
  login prunes expired records and always issues a fresh token.
- Origin and session-bound CSRF checks precede mutation body reads. Login uses
  a separate signed, ten-minute challenge, invalidated on server restart.
  Authentication bodies are bounded JSON; all Argon2 work and failure budgets
  are shared with CLI checks.
- The dashboard lists 25 live files per page and reports quota usage under the
  storage lock. Raw uploads, replacement, metadata edits, and deletion reuse
  existing file-operation code. Long uploads recheck session validity before
  publishing. Browser sessions never authorize CLI mutation routes.
- Password rotation re-verifies the browser password and validates session and
  credential versions transactionally. Concurrent resets cannot issue stale
  sessions or overwrite newer CLI credentials.
- Templates and one new fixed script provide the admin interface without a
  frontend build or new dependencies. JavaScript is required for mutations.
- Tests cover migration rollback, expiration, revocation, concurrent reset,
  CSRF/origin rejection before body consumption, pagination, quota accounting,
  and the full admin workflow in an isolated Chrome session over test HTTPS.

## Milestone 6: Deployment and release checks

Deliver proxy/service examples, a curl usage guide, backup/restore instructions, and operational logging.

Acceptance criteria:

- HTTPS integration and trusted-proxy configuration work as documented.
- Application and proxy upload limits agree.
- Four simultaneous transfers work with bounded memory usage.
- Restarting the process during an upload produces recoverable state.
- A backup can restore both metadata and file contents consistently.
- Logs omit credentials, session tokens, filenames, and shared URLs.
- Deployment runs without elevated application privileges and does not expose the blob directory directly.

## Scope and completion

All milestones target the single-host, single-administrator design. V1 includes both the public viewer and browser admin panel. Resumable uploads, reader accounts, version history, distributed storage, and the other exclusions in the architecture are outside this plan.

The release is complete when all milestone acceptance criteria pass, deployment and recovery instructions have been exercised, and the resulting implementation has been reviewed against the approved architecture.
