# curl usage

Always use the configured HTTPS origin. Ordinary curl/wget reads receive bytes;
browser viewer HTML requires an explicit `Accept: text/html`. File management
uses the **CLI** password and username `admin`, not the browser password.

## Authentication

`-u admin` prompts for the password without putting it in the command or shell
history:

```sh
curl -u admin -i https://files.example.com/auth/check
```

Expect `204`. For unattended use, create a service-specific private curl config
file with your editor, mode `0600`, in a directory only you can access. Its contents
can be:

```text
user = "admin:YOUR_CLI_PASSWORD"
proto = "=https"
proto-redir = "=https"
```

Replace the placeholder in the editor, not a shell command. Quote/escape embedded
double quotes and backslashes according to curl's config syntax. Never commit
this file. Use it only with this server's trusted URL: loading a password file
does not constrain which HTTPS host receives credentials.

```sh
curl -q --config /private/junkyard.curl -T report.txt https://files.example.com/u/
```

`-q` must be curl's first argument to ignore implicit `.curlrc` settings. Do not
disable TLS verification or use `--location-trusted`. See curl's
[configuration and authentication options](https://curl.se/docs/manpage.html).

## Create and read

```sh
curl -u admin -T photo.jpg https://files.example.com/u/
curl -u admin -T photo.jpg 'https://files.example.com/u/photo.jpg?s=1&e=7d'
printf 'hello\n' | curl -u admin -T - 'https://files.example.com/u?name=hello.txt&e=12h'
curl https://files.example.com/ID -o saved-file
curl https://files.example.com/ID/download -o saved-file
```

Creation returns `201` and a share URL on its own line. The default ID has six
URL-safe random characters; `s=1` requests 32. Default expiration is 30 days;
`e=7d`/`e=12h` choose a lifetime and `e=0` means never expire.

curl appends the local filename to a trailing `/u/` **only without a query
string**. When supplying options, put an encoded filename in the path or use
`name=`. Otherwise the generated ID is the display filename. Names are metadata,
not storage paths. An explicit output name is preferable to accepting a remote
filename when downloading untrusted files.

Add `-H 'Accept: application/json'` to creation/updates for metadata, including
`id`, `url`, `filename`, `size`, UTC timestamps, expiration, and revision.
For scripts, `--fail --silent --show-error` gives a nonzero exit on HTTP errors;
omit `--fail` and use `-i` when inspecting the server's error body and headers.

## Replace, edit, delete

```sh
curl -u admin -T replacement.txt https://files.example.com/ID
curl -u admin -X PATCH 'https://files.example.com/ID?e=0'
curl -u admin -G -X PATCH --data-urlencode 'name=report final.txt' --data-urlencode 'e=7d' https://files.example.com/ID
curl -u admin -X DELETE https://files.example.com/ID
```

Replacement preserves the ID, name, and expiration unless options explicitly
change them. An explicit lifetime starts at the successful update. `-G` above
puts encoded parameters in the URL while `-X PATCH` retains the PATCH method;
metadata edits must not send a request body. Delete returns `204` and new reads
immediately fail; active downloads may finish. An expired file cannot be revived.

Do not automatically retry creates after an ambiguous network failure. The file
may already have committed and a retry can create another link. Check the admin
list before retrying. Interrupted uploads restart from the beginning.

## HEAD and ranges

```sh
curl -I https://files.example.com/ID
curl --range 0-1023 https://files.example.com/ID -o first-kib
curl -C - https://files.example.com/ID -o partial-file
```

HEAD has the selected GET representation's headers without its body. File bytes
support one closed, open-ended, or suffix range. A range request returns `206`;
invalid/unsatisfiable byte ranges return `416`, while multi-ranges fall back to
the full file. Raw/download endpoints always return bytes even if HTML is accepted.

Plain `-C -` does not check whether the file changed. For a safe resume, retain
the original strong ETag from the first response and send it as `If-Range` with
the range request. A matching tag allows the range; a changed tag returns the
whole new representation. If curl refuses to resume that full response, download
to a new file rather than appending it to the old partial file. Weak/date
validators do not enable a partial response.

## Common errors

| Status | Meaning / next step |
|---|---|
| 400 | Invalid option, filename, framing, or trusted-proxy header |
| 401 | Missing/wrong CLI credential; browser cookies do not authorize CLI writes |
| 404 | Unknown, expired, or deleted link |
| 409 | Target changed during an upload; inspect its current state before retrying |
| 413 | File exceeds the matched proxy/application size limit |
| 429 | Rate limited; wait for `Retry-After` |
| 503 | Transfer/authentication capacity or storage unavailable; inspect headers and retry later |
| 507 | Storage quota or filesystem space exhausted |

Anonymous reads default to one request per second with a burst of ten. Verified
CLI authentication bypasses that read limit. Failed authentication has its own
shared browser/CLI budget. Request IDs in response headers let an operator find
the corresponding safe log record without logging the shared link.
