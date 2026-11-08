# Deploying Junkyard

These examples target one Linux host, one administrator, one Uvicorn worker,
and a dedicated Nginx HTTPS proxy. They are installation instructions for an
operator; development tests do not install services, create system accounts,
or alter host configuration.

## Layout and prerequisites

| Path | Owner / permissions | Purpose |
|---|---|---|
| `/opt/junkyard` | Deployment owner; service user cannot write | Reviewed source and locked `.venv` |
| `/etc/junkyard` | root:junkyard, `0750` | Configuration directory |
| `/etc/junkyard/junkyard.toml` | root:junkyard, `0640` | Non-secret settings |
| `/var/lib/junkyard` | junkyard:junkyard, `0700` | SQLite, blobs, staging and process lock |
| `/etc/ssl/junkyard` | TLS administrator, private | Certificate chain and private key for Nginx |

Use the existing Python 3.12 runtime and uv to install the reviewed source from
`uv.lock`. Build the virtual environment in its final location; do not copy a
development `.venv`, whose entry points contain absolute paths. From the reviewed
deployment checkout, run the installed uv executable:

```sh
uv sync --project /opt/junkyard --locked --no-dev --no-python-downloads --python python3.12
```

Use an installation account that can write `/opt/junkyard`, then ensure the
runtime account cannot modify that tree. No package manager runs in the service.
Nginx, systemd, and valid TLS certificates must already be provisioned by the
host administrator. The application needs neither root nor access to the TLS key.

Create a dedicated `junkyard` system user/group with no interactive login if it
does not already exist. On systems with the usual Linux `useradd` tool:

```sh
sudo useradd --system --user-group --home-dir /var/lib/junkyard --no-create-home --shell /usr/sbin/nologin junkyard
sudo install -d -o root -g junkyard -m 0750 /etc/junkyard
sudo install -d -o junkyard -g junkyard -m 0700 /var/lib/junkyard
```

Adapt account/tool paths for your distribution. The data directory and all its
parents must be real directories, not symlinks. It must remain on a local
filesystem that supports SQLite, atomic rename, fsync, and flock.

## Configure and initialize

Review [deploy/junkyard.toml](../deploy/junkyard.toml), set `public_url` to the exact
external HTTPS origin, and install the edited copy at
`/etc/junkyard/junkyard.toml` with the ownership above. It uses:

- Loopback-only `127.0.0.1:8000`.
- An explicit trusted proxy of `127.0.0.1`.
- 262144000 bytes per file (250 MiB), 100 GiB total content quota, four transfers.

Initialize a new installation from a real terminal so password entry is hidden:

```sh
sudo -u junkyard /opt/junkyard/.venv/bin/junkyard --config /etc/junkyard/junkyard.toml init
```

For an existing installation, stop the server, take an [offline backup](backup-restore.md),
and run `migrate` instead of `init`. Milestone 6 keeps schema version 3; databases
already migrated for milestone 5 need no further upgrade. Password recovery uses
`passwd cli` or `passwd browser` with the same executable and `--config` prefix.

## Run with systemd

Install the reviewed [service unit](../deploy/junkyard.service) as
`/etc/systemd/system/junkyard.service`, owned by root with mode `0644`, then run:

```sh
sudo systemd-analyze verify /etc/systemd/system/junkyard.service
sudo systemctl daemon-reload
sudo systemctl enable --now junkyard.service
sudo systemctl status junkyard.service
```

The unit uses `User=junkyard`, no capabilities, no privilege escalation, a
read-only system filesystem, private temporary files/devices, and a private
`StateDirectory`. systemd permits writes to that state directory despite
`ProtectSystem=strict`. The code/configuration stay read-only to the application.
See the [systemd execution documentation](https://www.freedesktop.org/software/systemd/man/258/systemd.exec.html).

Use the supplied `junkyard serve` entry point, not a separate `uvicorn` command:
it enforces one worker, disables implicit proxy parsing and raw access logs,
and installs the private log formatter. Shutdown gives transfers up to 30 seconds
to finish; systemd allows 90 seconds for the whole shutdown before killing the
process group. Interrupted uploads are not resumable and must be retried after
checking whether publication succeeded. Startup recovery handles abandoned data.

## Configure Nginx

[deploy/nginx.conf](../deploy/nginx.conf) is a **complete dedicated configuration**,
not a fragment to paste into an existing `server` block. Adapt its worker account
(`www-data` in the example), hostname, certificate paths, and log location. For
an existing multi-site Nginx installation, merge the relevant blocks deliberately
and inspect inherited/default-server logging; do not overwrite unrelated sites.

Provide a valid certificate chain and private key at the configured paths using
your existing certificate-management process. Provisioning/renewal is external
to Junkyard; arrange Nginx reload after renewal. The example rejects HTTP on port
80, so certificate methods that need HTTP validation require a separate, narrow
validation configuration or a different challenge method.

After installing the reviewed configuration:

```sh
sudo nginx -t
sudo systemctl reload nginx
```

The proxy exposes HTTPS on port 443 and rejects noncanonical Host values. Port 80
rejects requests instead of redirecting credentials or upload bodies. HSTS is
enabled for this hostname only; do not enable it until HTTPS is correctly
provisioned. Expose only the proxy through the host firewall. Keep port 8000 on
loopback, and never add a Nginx `root`/`alias` for `/var/lib/junkyard`.

The example overwrites `X-Forwarded-For` with the socket client address; it never
appends client-supplied chains. Only this proxy address is trusted by the app.
Other forwarded headers are stripped. The local host/proxy is a trust boundary:
an untrusted local process can connect to loopback and claim an IP, so do not
share this backend with untrusted local users or expose it through another proxy
without revisiting trust configuration.

Keep `client_max_body_size` **equal in bytes** to `max_file_bytes`. If you change
one, change the other and validate/reload both services. The default values are
checked automatically. Authentication JSON still has its smaller application
limit. Edge-generated errors such as Nginx `413`/`502` may be HTML rather than
the application's JSON.

Request and response buffering and proxy caching are disabled. Upstream HTTP/1.1
allows chunked uploads to stream; bodies reach application staging before the
whole request arrives. This avoids storing a second upload copy outside the
application quota. Proxy retries are disabled because a create operation can
have committed even if its response was lost. The 60-second proxy timeouts are
inactivity limits, not a total file-transfer duration. These choices follow
Nginx's [proxy-module behavior](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
and [request-size limit](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size).

Because Uvicorn deliberately ignores forwarded scheme headers, automatic
trailing-slash redirects are disabled. Use the documented exact paths; the
explicit `/u/` alias and relative `/admin` redirect still work.

## Logging and diagnostics

The application writes one JSON event when each HTTP request finishes. Fields
include UTC time, level, operation, normalized method, status, duration, outcome,
and a newly generated request ID returned in `X-Request-ID`. No path, filename,
shared ID/URL, IP, query, request body, cookie, authorization value, or incoming
correlation ID is copied into the record. An incomplete stream can have status
`200` with outcome `failed`, `cancelled`, or `incomplete`: headers may already
have been sent.

Unexpected exceptions return a generic `500` before headers are sent; after
headers, the stream fails. Exception text and tracebacks are omitted from
production formatting because they may contain client input. Startup/cleanup
events use fixed labels; other server events retain their level without arbitrary
text. Inspect operation, outcome, status, and request ID, then reproduce a problem
locally with synthetic data for detailed debugging.

```sh
sudo journalctl -u junkyard.service --since '1 hour ago'
sudo tail -n 50 /var/log/nginx/junkyard-access.log
```

The Nginx access log contains time, status, duration, a proxy request ID, and the
application request ID when available. Nginx also returns `X-Proxy-Request-ID`.
Its unredacted error log is deliberately sent to `/dev/null`; Nginx cannot apply
the access-log field selection to arbitrary errors. Thus a proxy `502` remains
observable without recording the target link, but detailed proxy error text is
unavailable. Do not enable ordinary combined/access/debug logs, request tracing,
or body capture on this virtual host. Configure bounded journal retention and
log rotation for `junkyard-access.log` using your host's existing facilities.
See [Nginx's log format documentation](https://nginx.org/en/docs/http/ngx_http_log_module.html).

## Smoke check and release validation

Run these through the real HTTPS origin, without disabling certificate validation:

```sh
curl -u admin -i https://files.example.com/auth/check
curl -u admin -T example.txt https://files.example.com/u/
```

Expect `204` on the authentication check and `201` plus a share URL on upload.
Read that link with curl and in a browser; verify HEAD, byte ranges, and browser
administration at `/admin/`. Confirm that private storage paths such as
`/metadata.sqlite3` return `404`. Inspect logs to confirm that none of these links,
filenames, passwords, or cookies appears. A direct loopback smoke check, unlike
the HTTPS proxy path, must supply the required single-IP `X-Forwarded-For` header.

The release suite runs real Nginx and Uvicorn as the ordinary test user on
loopback high ports, validates the unit with `systemd-analyze`, verifies TLS with
a synthetic CA certificate, exercises streaming and trusted-proxy handling, and
checks log privacy on successes and errors. Four simultaneous 32 MiB uploads and
downloads must complete correctly while transfer-phase process RSS growth stays
below 32 MiB; a fifth transfer is rejected. This bounds transfer memory growth,
not total machine memory, kernel socket buffers, page cache, or metadata size.
The suite also covers a process crash during upload and a full offline restore.

Run the [test commands in README](../README.md#tests-and-packaging). Nginx,
OpenSSL, and `systemd-analyze` must be installed for all release checks; skips do
not count as a release pass. Host-specific systemd isolation, firewall rules,
certificate renewal, and reboot behavior still need verification on the target
host: the test suite validates examples and unprivileged processes, not a live
systemd deployment.
