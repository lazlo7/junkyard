# Offline backup and restore

Back up **the entire private data directory together**, including SQLite, blobs,
staging, and any SQLite sidecars. Copying only the database or copying database
and blobs while uploads continue can produce an inconsistent backup. The simple
v1 procedure stops the service and all other writers; no online snapshot service
or archive format is introduced.

Backups contain file contents and credential hashes. Keep them private and
protect off-host copies according to the sensitivity of the files. Deletion and
expiration do not erase older backups. These commands are operator procedures
for the deployed paths in [deployment.md](deployment.md), not commands executed
by the development agent.

## Create a backup

1. Choose a new destination outside `/var/lib/junkyard`. Use a private parent on
   a filesystem with enough free space. Do not reuse/merge an existing snapshot.
2. Stop `junkyard.service` and wait until it is inactive. Do not run `passwd`,
   `migrate`, another server, or any other database writer until copying finishes.
3. Copy the complete directory with ownership and mode preservation, together
   with the configuration and lockfile for the installed version.
4. Write and verify checksums, flush the destination, then restart the service.

For example, from a root shell on the deployment host (adjust the fresh name):

```sh
umask 077
backup_dir=/srv/junkyard-backups/snapshot-2026-11-08
mkdir -m 0700 -- "$backup_dir"
systemctl stop junkyard.service
systemctl is-active junkyard.service
# Continue only when the service is inactive and no other writer is running.
cp -a -- /var/lib/junkyard "$backup_dir/data"
cp -a -- /etc/junkyard/junkyard.toml "$backup_dir/junkyard.toml"
cp -a -- /opt/junkyard/uv.lock "$backup_dir/uv.lock"
cd -- "$backup_dir"
find data -type f -print0 | sort -z | xargs -0 sha256sum -- > SHA256SUMS
sha256sum -- junkyard.toml uv.lock >> SHA256SUMS
sha256sum --check SHA256SUMS
sync -f "$backup_dir"
systemctl start junkyard.service
```

The backup parent must already exist and be private. `systemctl is-active`
normally exits nonzero for an inactive service; inspect its result rather than
running this as an unchecked command chain. If copying or verification fails,
keep the failed snapshot separate and diagnose the failure before treating it as
a backup. Retain the matching reviewed application version as well as `uv.lock`.
For the installed source checkout, record its release/commit identifier with the
backup. A lockfile alone does not contain application code.

## Verify and restore

Restore only trusted snapshots produced by your backup process. A checksum
manifest detects accidental damage, not a maliciously replaced backup and
manifest. Copy into a **new** private directory on the same filesystem as the
deployed data; never overlay an existing live directory.

1. Verify `SHA256SUMS` before changing the service. Use the matching application
   version to begin recovery; if an upgrade is needed, follow its migration path.
2. Stop the service and all writers. Preserve the current data directory for
   rollback; do not delete it.
3. Copy the snapshot to a fresh restore directory. Verify ownership is the
   service UID/GID, directories are `0700`, files are `0600`, and no symlinks or
   hardlinks were introduced. On a different host, explicitly adjust ownership
   for its `junkyard` account. Verify the copied data against the manifest before
   opening SQLite. Do not silently loosen permissions.
4. Create a temporary private configuration using this restore directory and a
   different loopback port, such as `8001`. Keep the real public URL. Nothing in
   Nginx should point to this port yet.
5. Check SQLite integrity, then reset the **browser** password on the restored
   database to revoke sessions that came back with the snapshot. A restore can
   also bring back an older CLI password: reset it if necessary before exposure.
6. Start the restored instance on the test port. Startup checks schema,
   permissions, blob references, and sizes before removing expired/orphaned data.
   Verify known live files by downloading them and comparing their bytes/checksums.
   Expiration timestamps are preserved; files whose deadlines passed during
   downtime become inaccessible and are cleaned up on startup.
7. Stop the test instance. Move the current directory aside, move the verified
   restore into the configured data path, then start the service and smoke-test
   through HTTPS. Keep the old directory until recovery has been reviewed.

Example copy and SQLite check from the operator shell:

```sh
cd -- /srv/junkyard-backups/snapshot-2026-11-08
sha256sum --check SHA256SUMS
systemctl stop junkyard.service
# The destination must not exist; cp -a preserves the snapshot's modes/owners.
test ! -e /var/lib/junkyard.restore-2026-11-08 &&
  test ! -L /var/lib/junkyard.restore-2026-11-08 &&
  cp -a -- data /var/lib/junkyard.restore-2026-11-08
```

Check the copied data (the manifest has a `data/` prefix that is removed here):

```sh
cd -- /var/lib/junkyard.restore-2026-11-08
sed -n 's#  data/#  #p' /srv/junkyard-backups/snapshot-2026-11-08/SHA256SUMS | sha256sum --check -
```

After ensuring the original service UID is mapped correctly on this host, open
only the working restore copy read-write so SQLite can recover any copied hot
journal before checking integrity. The backup itself stays untouched:

```sh
sudo -u junkyard /opt/junkyard/.venv/bin/python -c 'import sqlite3; c=sqlite3.connect("file:/var/lib/junkyard.restore-2026-11-08/metadata.sqlite3?mode=rw", uri=True); rows=c.execute("PRAGMA integrity_check").fetchall(); c.close(); assert rows == [("ok",)], "SQLite integrity check failed"'
```

Install the temporary settings as `/etc/junkyard/restore.toml`, with
`data_dir = "/var/lib/junkyard.restore-2026-11-08"` and `bind_port = 8001`. Then:

```sh
sudo -u junkyard /opt/junkyard/.venv/bin/junkyard --config /etc/junkyard/restore.toml passwd browser
sudo -u junkyard /opt/junkyard/.venv/bin/junkyard --config /etc/junkyard/restore.toml serve
```

If the snapshot predates schema 3, run `migrate` against the temporary configuration
before these steps. In another terminal, use the retained CLI credential (or a
freshly reset one) for loopback checks, supplying the trusted-proxy header:

```sh
curl -H 'X-Forwarded-For: 127.0.0.1' http://127.0.0.1:8001/KNOWN_ID/download -o restored-file
sha256sum -- restored-file
```

This local check is on the same host and downloads public bytes; do not send a
password over remote plain HTTP. Stop the temporary server with Ctrl-C after
verification. With both servers stopped, a cutover that preserves the old data
could be:

```sh
test ! -e /var/lib/junkyard.before-restore-2026-11-08 &&
  test ! -L /var/lib/junkyard.before-restore-2026-11-08 &&
  mv -T -- /var/lib/junkyard /var/lib/junkyard.before-restore-2026-11-08 &&
  mv -T -- /var/lib/junkyard.restore-2026-11-08 /var/lib/junkyard &&
  systemctl start junkyard.service
```

Treat each precondition as a stop-on-failure check; these are reviewable operator
steps, not an unattended restore script. If verification fails, leave the current
data in place and repair or choose another snapshot. The release test exercises
this offline-copy approach with replaced/deleted source files, credential
changes, expired links, session revocation, and an incomplete copy that startup
must reject without deleting its metadata.
