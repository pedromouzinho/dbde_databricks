"""Shared utility functions for DBDE AI Assistant."""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)


def odata_escape(value: str) -> str:
    """Escape single quotes for OData filter expressions."""
    return str(value or "").replace("'", "''")


def safe_blob_component(value: str, fallback: str = "file", max_len: int = 120) -> str:
    """Sanitize a string for use as Azure Blob name component."""
    txt = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", txt).strip().replace(" ", "_")
    if not safe:
        safe = str(fallback or "file")
    return safe[:max_len]


def create_logged_task(coro, name: str = "") -> asyncio.Task:
    """Create an asyncio task with error logging callback."""
    task = asyncio.create_task(coro)

    def _done(done_task: asyncio.Task) -> None:
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc:
            logger.error("Background task %s failed: %s", name, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task

