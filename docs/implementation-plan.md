# Junkyard implementation plan

Status: approved by the project owner on 2026-11-07. Implementation has not started and remains paused until explicitly requested.

The behavior, interfaces, security policies, and operational defaults are defined in [architecture.md](architecture.md), based on [requirements.md](requirements.md).

## Development approach

- Make incremental, focused changes, with automated tests for new functionality.
- Keep each milestone independently reviewable and run relevant tests after modifications.
- Use real HTTP tests against Uvicorn for streaming behavior and curl compatibility, in addition to unit and application-level tests.
- Use controlled clocks for expiration tests and injected failures for persistence recovery tests.
- Discuss significant changes to the approved architecture before implementing them.
- Do not commit changes unless explicitly requested.

## Milestone 1: Foundation and credentials

Deliver project packaging, configuration, schema initialization, local credential commands, and Basic authentication.

Acceptance criteria:

- Invalid configuration fails clearly.
- No default credentials or public setup endpoint exist.
- Passwords are never stored or logged in plaintext.
- CLI and browser credentials remain separate.
- Local initialization and recovery use hidden password prompts.

## Milestone 2: Durable CLI file operations

Deliver streaming creation, read, replacement, metadata edits, deletion, short/long IDs, and quotas.

Acceptance criteria:

- End-to-end curl tests cover creation, download, replacement, metadata edits, and deletion.
- Filename and directory-traversal attacks cannot escape private storage or inject response headers.
- Forced public-ID collisions are handled without overwriting an existing file.
- Interrupted uploads and disk-full failures do not publish incomplete files or damage previous contents.
- Concurrent replacement and deletion obey the conflict policy.
- Injected failures around blob publication and database commit leave recoverable state.
- File-size and storage limits are enforced for streamed bodies.

## Milestone 3: Lifecycle and HTTP behavior

Deliver expiration, cleanup, recovery, HEAD, range downloads, and throttling.

Acceptance criteria:

- Fake-clock tests cover default expiration, explicit lifetimes, permanent files, and deadline-preserving replacements.
- Expiration denies new reads immediately while active downloads finish.
- Restart recovery and orphan cleanup preserve referenced and actively used blobs.
- Concurrent operations cannot bypass storage quotas.
- HEAD and range responses are correct, including invalid ranges.
- Forged proxy headers cannot bypass client-IP handling or rate limits.
- Anonymous throttling, authentication throttling, and `Retry-After` behavior are tested.

## Milestone 4: Public viewer

Deliver negotiated HTML, safe previews, metadata display, and download/share controls.

Acceptance criteria:

- Browsers and curl receive the correct representations; explicit raw/download routes remain predictable.
- Uploaded HTML and SVG remain non-executable.
- Filenames and displayed text are escaped.
- Supported media previews and video seeking work.
- Text previews are bounded and show truncation clearly.
- Unsupported formats provide a download path.
- Error pages and headless errors have appropriate status codes and formats.

## Milestone 5: Browser administration

Deliver sessions, CSRF protection, file management, storage display, and CLI-password rotation.

Acceptance criteria:

- Session expiration, logout, and browser-password-change revocation work.
- CSRF and cross-origin mutation attempts are rejected.
- Browser cookies cannot authorize CLI mutation routes.
- CLI-password rotation invalidates the old CLI password without changing browser credentials.
- A complete browser upload, replacement, metadata-edit, and deletion workflow works.
- The admin list is paginated and storage information reflects the quota policy.

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
