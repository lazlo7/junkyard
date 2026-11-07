"""Periodic cleanup with orderly shutdown and retry after storage errors."""

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from junkyard.database import StoreError
from junkyard.files import FileStore

logger = logging.getLogger(__name__)


async def cleanup_loop(files: FileStore, interval: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                await run_in_threadpool(files.cleanup)
            except (OSError, StoreError):
                # Do not expose filenames or blob identifiers in operational logs.
                logger.error("File cleanup failed; it will be retried at the next interval")
