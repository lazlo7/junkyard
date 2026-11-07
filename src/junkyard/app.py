"""Application lifecycle, authentication checks, and file routes."""

from contextlib import asynccontextmanager
import asyncio

from fastapi import Depends, FastAPI, Response
from starlette.concurrency import run_in_threadpool

from junkyard.config import Settings
from junkyard.database import Database
from junkyard.file_routes import install_file_routes
from junkyard.files import FileStore
from junkyard.lifecycle import cleanup_loop
from junkyard.rate_limits import RequestLimits
from junkyard.request_protection import RequestProtection
from junkyard.security import CLIAuthenticator, require_cli


def create_app(settings: Settings) -> FastAPI:
    database = Database(settings.data_dir)
    files = FileStore(database, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(files.start)
        stop = asyncio.Event()
        cleaner = asyncio.create_task(cleanup_loop(files, settings.cleanup_interval_seconds, stop))
        try:
            yield
        finally:
            stop.set()
            try:
                # Wait for any running disk cleanup before releasing the process lock.
                await cleaner
            finally:
                await run_in_threadpool(files.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cli_authenticator = CLIAuthenticator(database)
    app.state.files = files
    app.state.limits = RequestLimits(settings)
    app.add_middleware(RequestProtection, settings=settings,
                       authenticator=app.state.cli_authenticator, limits=app.state.limits)

    @app.get("/auth/check", dependencies=[Depends(require_cli)], status_code=204)
    def check_authentication() -> Response:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    install_file_routes(app)
    return app

