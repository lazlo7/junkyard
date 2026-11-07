"""Milestone 1 application: initialization checks and CLI authentication."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from junkyard.config import Settings
from junkyard.database import Database, StoreError
from junkyard.security import AuthenticationBusy, CLIAuthenticator, decode_basic

CHALLENGE = {"WWW-Authenticate": 'Basic realm="junkyard-cli", charset="UTF-8"', "Cache-Control": "no-store"}


def require_cli(request: Request) -> None:
    credentials = decode_basic(request.headers.getlist("authorization"))
    if credentials is None:
        raise HTTPException(status_code=401, detail="Invalid CLI credentials", headers=CHALLENGE)
    try:
        valid = request.app.state.cli_authenticator.verify(*credentials)
    except AuthenticationBusy:
        raise HTTPException(status_code=503, detail="Authentication busy", headers={"Retry-After": "1", "Cache-Control": "no-store"}) from None
    except StoreError:
        raise HTTPException(status_code=503, detail="Authentication unavailable", headers={"Cache-Control": "no-store"}) from None
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid CLI credentials", headers=CHALLENGE)


def create_app(settings: Settings) -> FastAPI:
    database = Database(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(database.check_initialized)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cli_authenticator = CLIAuthenticator(database)

    @app.get("/auth/check", dependencies=[Depends(require_cli)], status_code=204)
    def check_authentication() -> Response:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return app

