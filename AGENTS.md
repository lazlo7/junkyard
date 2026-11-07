# Project

Personal, self-hosted, CLI-centric file server.

## Development principles

- Prefer simple, maintainable implementations.
- Follow the principle of least privilege.
- Treat all client input as untrusted.
- Avoid unnecessary dependencies.
- Prefer established standards and protocols.
- Keep security-sensitive logic explicit and testable.
- Maintain compatibility with headless Linux environments.

## Workflow

- Discuss significant architectural decisions before implementation.
- Make incremental, focused changes.
- Write automated tests for new functionality.
- Run relevant tests after modifications.
- Explain important design tradeoffs.
- Do not introduce dependencies without justification.
- Do not commit changes unless explicitly requested.

## Security

- Never commit secrets or credentials.
- Validate all file paths and prevent directory traversal.
- Consider filesystem permissions and concurrent access.
- Do not weaken authentication or authorization for convenience.

## Execution policy

- Explain commands that modify or delete files before running them.
- Request explicit authorization before destructive operations.
- Never request elevated privileges without explaining why.
- Never modify files outside the project directory.
- Never access personal files, credentials, or SSH keys.
- Do not install system packages without explicit authorization.
- Do not execute downloaded scripts without review.
- Keep changes small and reviewable.

