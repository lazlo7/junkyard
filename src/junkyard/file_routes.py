"""Streaming HTTP adapters; file operations and SQL live in FileStore."""

from dataclasses import replace
import errno
import re

import anyio
from fastapi import Depends, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

from junkyard.database import StoreError
from junkyard.file_options import FileError, parse_options, validate_encoding, validate_name
from junkyard.files import CHUNK_SIZE, Download, FileRecord, FileStore
from junkyard.http_files import PRIVATE_HEADERS, download_headers, select_range
from junkyard.presentation import error_response, viewer_response, wants_html
from junkyard.previews import inspect_preview
from junkyard.security import require_cli

def expected_length(request: Request) -> int | None:
    values = request.headers.getlist("content-length")
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,20}", values[0]) or "transfer-encoding" in request.headers:
        raise FileError(400, "Invalid upload framing")
    return int(values[0])


def wants_json(request: Request) -> bool:
    for item in request.headers.get("accept", "").split(","):
        parts = [part.strip().lower() for part in item.split(";")]
        if parts[0] == "application/json":
            quality = next((part[2:] for part in parts[1:] if part.startswith("q=")), "1")
            try:
                if 0 < float(quality) <= 1:
                    return True
            except ValueError:
                pass
    return False


def mutation_response(request: Request, record: FileRecord, status: int) -> Response:
    url = request.app.state.files.settings.public_url + "/" + record.id
    headers = {**PRIVATE_HEADERS, "Location": url, "Vary": "Accept"}
    if wants_json(request):
        return JSONResponse({
            "id": record.id, "url": url, "filename": record.name, "size": record.size,
            "created_at": record.created_at, "modified_at": record.modified_at,
            "expires_at": record.expires_at, "revision": record.revision,
        }, status_code=status, headers=headers)
    return PlainTextResponse(url + "\n", status_code=status, headers=headers)


class DownloadResponse(StreamingResponse):
    def __init__(self, store: FileStore, download: Download, request: Request | None = None, headers=None):
        self.store = store
        self.download = download
        headers = download_headers(download.record) if headers is None else headers
        start, length, status = select_range(request, download.record.size, headers["ETag"]) if request else (0, download.record.size, 200)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{download.record.size}"
            headers["Content-Length"] = str(length)

        async def chunks():
            await run_in_threadpool(download.file.seek, start)
            remaining = length
            while remaining:
                chunk = await run_in_threadpool(download.file.read, min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError("blob ended before its declared length")
                remaining -= len(chunk)
                yield chunk

        super().__init__(chunks(), status_code=status, headers=headers)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BackgroundTask alone does not run on every disconnect/error path.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.store.close_download, self.download)


async def upload_file(request: Request, filename: str | None = None, file_id: str | None = None) -> Response:
    store: FileStore = request.app.state.files
    validate_encoding(request.scope["raw_path"])
    options = parse_options(request.scope["query_string"], creating=file_id is None)
    if filename is not None:
        validate_name(filename)  # Reject unsafe path names even if name= overrides them.
        if options.name is None:
            options = replace(options, name=filename)
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise FileError(415, "Encoded upload bodies are not supported; send raw file bytes")
    length = expected_length(request)
    if length is not None and length > store.settings.max_file_bytes:
        raise FileError(413, "File exceeds the configured size limit")
    upload = await run_in_threadpool(store.begin, options, file_id)
    try:
        async for chunk in request.stream():
            # Bound individual disk writes even if an ASGI server delivers a large chunk.
            for start in range(0, len(chunk), CHUNK_SIZE):
                await run_in_threadpool(store.write, upload, memoryview(chunk)[start:start + CHUNK_SIZE])
        if length is not None and length != upload.size:
            raise FileError(400, "Upload length does not match Content-Length")
        if getattr(request.state, "browser_session", None) is not None and request.url.path.startswith("/admin/"):
            # Recheck after a potentially long transfer, before publishing it.
            await run_in_threadpool(request.app.state.browser_auth.require_live, request.state.browser_session)
        record = await run_in_threadpool(store.finish, upload)
        return mutation_response(request, record, 201 if file_id is None else 200)
    except ClientDisconnect:
        raise FileError(400, "Upload interrupted") from None
    finally:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(store.abort, upload)


def install_file_routes(app: FastAPI) -> None:
    @app.exception_handler(FileError)
    async def file_error(request: Request, error: FileError):
        headers = {**PRIVATE_HEADERS, **error.headers}
        if error.status == 503:
            headers["Retry-After"] = "1"
        return error_response(request, error.status, error.detail, headers)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(request, error.status_code, str(error.detail), error.headers)

    @app.exception_handler(StoreError)
    async def store_error(request: Request, error: StoreError):
        return error_response(request, 503, "File storage unavailable", {"Retry-After": "1"})

    @app.exception_handler(OSError)
    async def io_error(request: Request, error: OSError):
        status = 507 if error.errno in (errno.ENOSPC, errno.EDQUOT) else 503
        return error_response(request, status, "File storage unavailable")

    @app.put("/u", dependencies=[Depends(require_cli)])
    @app.put("/u/", dependencies=[Depends(require_cli)])
    async def create_unnamed(request: Request):
        return await upload_file(request)

    @app.put("/u/{filename:path}", dependencies=[Depends(require_cli)])
    async def create_named(request: Request, filename: str):
        return await upload_file(request, filename=filename)

    @app.put("/{file_id}", dependencies=[Depends(require_cli)])
    async def replace_contents(request: Request, file_id: str):
        return await upload_file(request, file_id=file_id)

    @app.patch("/{file_id}", dependencies=[Depends(require_cli)])
    async def patch_metadata(request: Request, file_id: str):
        validate_encoding(request.scope["raw_path"])
        options = parse_options(request.scope["query_string"], creating=False)
        if expected_length(request) not in (None, 0) or "transfer-encoding" in request.headers:
            raise FileError(400, "Metadata updates use query parameters, not a request body")
        record = await run_in_threadpool(request.app.state.files.patch, file_id, options)
        return mutation_response(request, record, 200)

    @app.delete("/{file_id}", dependencies=[Depends(require_cli)])
    async def delete_file(request: Request, file_id: str):
        validate_encoding(request.scope["raw_path"])
        if request.scope["query_string"]:
            raise FileError(400, "Delete does not accept query parameters")
        await run_in_threadpool(request.app.state.files.delete, file_id)
        return Response(status_code=204, headers=PRIVATE_HEADERS)

    @app.api_route("/{file_id}", methods=["GET", "HEAD"])
    @app.api_route("/{file_id}/raw", methods=["GET", "HEAD"])
    @app.api_route("/{file_id}/download", methods=["GET", "HEAD"])
    async def download_file(request: Request, file_id: str):
        validate_encoding(request.scope["raw_path"])
        store: FileStore = request.app.state.files
        download = await run_in_threadpool(store.open_download, file_id)
        try:
            is_raw = request.url.path.endswith("/raw")
            negotiated = not is_raw and not request.url.path.endswith("/download")
            if negotiated and wants_html(request):
                preview = await run_in_threadpool(inspect_preview, download.file, download.record.name, include_text=True)
                response = await run_in_threadpool(viewer_response, request, download.record, preview, store.settings.public_url)
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(store.close_download, download)
                return response
            headers = download_headers(download.record)
            if negotiated:
                headers["Vary"] = "Accept"
            if is_raw:
                preview = await run_in_threadpool(inspect_preview, download.file, download.record.name, include_text=False)
                if preview.kind in ("image", "audio", "video"):
                    headers["Content-Type"] = preview.media_type
                    headers["Content-Disposition"] = headers["Content-Disposition"].replace("attachment;", "inline;", 1)
            if request.method == "HEAD":
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(store.close_download, download)
                return Response(headers=headers)
            return DownloadResponse(store, download, request, headers)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(store.close_download, download)
            raise
