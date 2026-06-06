"""Shared temporary generated-file storage with ownership and cleanup."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config_databricks import GENERATED_FILES_BLOB_CONTAINER, GENERATED_FILE_TTL_SECONDS
from storage_databricks import blob_delete, blob_download_bytes, blob_download_json, blob_list, blob_upload_bytes, blob_upload_json
from utils import create_logged_task

logger = logging.getLogger(__name__)

_generated_files_store: dict[str, dict] = {}
_generated_files_lock = asyncio.Lock()
_GENERATED_FILE_TTL_SECONDS = max(300, int(GENERATED_FILE_TTL_SECONDS or 1800))
_GENERATED_FILE_MAX = 100
_GENERATED_FILE_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB
_LAST_REMOTE_SWEEP_AT: Optional[datetime] = None
_REMOTE_SWEEP_INTERVAL_SECONDS = 15 * 60


def generated_file_ttl_seconds() -> int:
    return _GENERATED_FILE_TTL_SECONDS


def _as_dt(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    txt = str(value or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _generated_blob_paths(download_id: str, fmt: str = "") -> tuple[str, str]:
    safe_id = "".join(c if c.isalnum() else "_" for c in str(download_id or "").strip())[:80] or "file"
    ext = "".join(c if c.isalnum() else "" for c in str(fmt or "").lower())[:10] or "bin"
    base = f"generated/{safe_id}"
    return f"{base}/content.{ext}", f"{base}/meta.json"


async def _purge_generated_file(download_id: str, meta: Optional[dict] = None) -> None:
    entry = meta or {}
    if not entry:
        try:
            _, meta_blob_name = _generated_blob_paths(download_id)
            entry = await blob_download_json(GENERATED_FILES_BLOB_CONTAINER, meta_blob_name) or {}
        except Exception:
            entry = {}
    fmt = str(entry.get("format", "") or "")
    content_blob_name = str(entry.get("content_blob_name", "") or "")
    if not content_blob_name:
        content_blob_name, _ = _generated_blob_paths(download_id, fmt)
    _, meta_blob_name = _generated_blob_paths(download_id, fmt)
    try:
        await blob_delete(GENERATED_FILES_BLOB_CONTAINER, content_blob_name)
    except Exception as e:
        logger.warning("[GeneratedFiles] content delete failed for %s: %s", download_id, e)
    try:
        await blob_delete(GENERATED_FILES_BLOB_CONTAINER, meta_blob_name)
    except Exception as e:
        logger.warning("[GeneratedFiles] meta delete failed for %s: %s", download_id, e)
    async with _generated_files_lock:
        _generated_files_store.pop(download_id, None)


async def _sweep_remote_expired_files(force: bool = False) -> None:
    global _LAST_REMOTE_SWEEP_AT
    now = datetime.now(timezone.utc)
    if not force and _LAST_REMOTE_SWEEP_AT is not None:
        if (now - _LAST_REMOTE_SWEEP_AT).total_seconds() < _REMOTE_SWEEP_INTERVAL_SECONDS:
            return
    _LAST_REMOTE_SWEEP_AT = now

    # blob_list (Postgres/Lakebase) returns a plain list of {name, content_type,
    # created_at} — no Azure-style marker pagination (that broke the sweep).
    try:
        items = await blob_list(GENERATED_FILES_BLOB_CONTAINER, prefix="generated/")
    except Exception as e:
        logger.warning("[GeneratedFiles] remote list failed: %s", e)
        return
    for item in items[:500]:
        name = str(item.get("name", "") or "")
        if not name.endswith("/meta.json"):
            continue
        try:
            meta = await blob_download_json(GENERATED_FILES_BLOB_CONTAINER, name)
        except Exception as e:
            logger.warning("[GeneratedFiles] remote meta read failed for %s: %s", name, e)
            continue
        if not isinstance(meta, dict) or not meta:
            continue
        created_at = _as_dt(meta.get("created_at"))
        ttl_seconds = int(meta.get("ttl_seconds", _GENERATED_FILE_TTL_SECONDS) or _GENERATED_FILE_TTL_SECONDS)
        if created_at and (now - created_at).total_seconds() > max(60, ttl_seconds):
            await _purge_generated_file(str(meta.get("download_id", "") or name.rsplit("/", 2)[-2]), meta)


async def cleanup_generated_files(force_remote_sweep: bool = False) -> None:
    now = datetime.now(timezone.utc)
    expired_ids: list[str] = []
    async with _generated_files_lock:
        for fid, meta in list(_generated_files_store.items()):
            created_at = _as_dt(meta.get("created_at")) or now
            if (now - created_at).total_seconds() > _GENERATED_FILE_TTL_SECONDS:
                expired_ids.append(fid)
        for fid in expired_ids:
            _generated_files_store.pop(fid, None)

        def _total_bytes() -> int:
            total = 0
            for meta in _generated_files_store.values():
                content = meta.get("content", b"")
                if isinstance(content, (bytes, bytearray)):
                    total += len(content)
            return total

        while len(_generated_files_store) > _GENERATED_FILE_MAX or _total_bytes() > _GENERATED_FILE_MAX_TOTAL_BYTES:
            oldest_id = min(
                _generated_files_store.items(),
                key=lambda item: _as_dt(item[1].get("created_at")) or now,
            )[0]
            expired_ids.append(oldest_id)
            _generated_files_store.pop(oldest_id, None)

    for fid in expired_ids:
        await _purge_generated_file(fid)

    if force_remote_sweep:
        await _sweep_remote_expired_files(force=True)
    else:
        create_logged_task(_sweep_remote_expired_files(force=False), name="generated_files_remote_sweep")


async def store_generated_file(
    content: bytes,
    mime_type: str,
    filename: str,
    fmt: str,
    *,
    user_sub: str = "",
    conversation_id: str = "",
    scope: str = "",
) -> str:
    payload = content if isinstance(content, (bytes, bytearray)) else bytes(content or b"")
    if len(payload) > _GENERATED_FILE_MAX_TOTAL_BYTES:
        logger.error(
            "[GeneratedFiles] file too large: %s bytes (max %s)",
            len(payload),
            _GENERATED_FILE_MAX_TOTAL_BYTES,
        )
        return ""
    await cleanup_generated_files()
    fid = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    cached_entry = {
        "content": payload,
        "mime_type": mime_type,
        "filename": filename,
        "format": fmt,
        "created_at": created_at,
        "user_sub": str(user_sub or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "scope": str(scope or "").strip(),
    }
    async with _generated_files_lock:
        _generated_files_store[fid] = cached_entry
    try:
        content_blob_name, meta_blob_name = _generated_blob_paths(fid, fmt)
        await blob_upload_bytes(
            GENERATED_FILES_BLOB_CONTAINER,
            content_blob_name,
            payload,
            content_type=mime_type or "application/octet-stream",
        )
        await blob_upload_json(
            GENERATED_FILES_BLOB_CONTAINER,
            meta_blob_name,
            {
                "download_id": fid,
                "filename": filename,
                "mime_type": mime_type,
                "format": fmt,
                "size_bytes": len(payload),
                "created_at": created_at.isoformat(),
                "ttl_seconds": _GENERATED_FILE_TTL_SECONDS,
                "content_blob_name": content_blob_name,
                "user_sub": cached_entry["user_sub"],
                "conversation_id": cached_entry["conversation_id"],
                "scope": cached_entry["scope"],
            },
        )
    except Exception as e:
        logger.warning("[GeneratedFiles] persistent store failed for %s: %s", fid, e)
    return fid



def get_generated_file_sync(download_id: str) -> Optional[dict]:
    """Synchronous getter for generated files (no cleanup, for route handler)."""
    entry = _generated_files_store.get(str(download_id or "").strip())
    if not entry:
        return None
    created_at = entry.get("created_at")
    if created_at:
        from datetime import datetime, timezone
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except Exception:
                created_at = datetime.now(timezone.utc)
        age = (datetime.now(timezone.utc) - created_at).total_seconds()
        if age > _GENERATED_FILE_TTL_SECONDS:
            return None
    return dict(entry)


async def get_generated_file(download_id: str) -> Optional[dict]:
    await cleanup_generated_files()
    entry = _generated_files_store.get(download_id)
    if entry:
        created_at = _as_dt(entry.get("created_at")) or datetime.now(timezone.utc)
        if (datetime.now(timezone.utc) - created_at).total_seconds() <= _GENERATED_FILE_TTL_SECONDS:
            return dict(entry)
        await _purge_generated_file(download_id, entry)
        return None

    try:
        _, meta_blob_name = _generated_blob_paths(download_id)
        meta = await blob_download_json(GENERATED_FILES_BLOB_CONTAINER, meta_blob_name)
        if not isinstance(meta, dict) or not meta:
            return None
        created_at = _as_dt(meta.get("created_at"))
        ttl_seconds = int(meta.get("ttl_seconds", _GENERATED_FILE_TTL_SECONDS) or _GENERATED_FILE_TTL_SECONDS)
        if created_at and (datetime.now(timezone.utc) - created_at).total_seconds() > max(60, ttl_seconds):
            await _purge_generated_file(download_id, meta)
            return None

        blob_name = str(meta.get("content_blob_name", "") or "")
        if not blob_name:
            fmt = str(meta.get("format", "") or "")
            blob_name, _ = _generated_blob_paths(download_id, fmt)
        content = await blob_download_bytes(GENERATED_FILES_BLOB_CONTAINER, blob_name)
        if not content:
            return None
        hydrated = {
            "content": content,
            "mime_type": str(meta.get("mime_type", "") or "application/octet-stream"),
            "filename": str(meta.get("filename", "") or f"download-{download_id}"),
            "format": str(meta.get("format", "") or ""),
            "created_at": created_at or datetime.now(timezone.utc),
            "user_sub": str(meta.get("user_sub", "") or ""),
            "conversation_id": str(meta.get("conversation_id", "") or ""),
            "scope": str(meta.get("scope", "") or ""),
        }
        async with _generated_files_lock:
            _generated_files_store[download_id] = hydrated
        return dict(hydrated)
    except Exception as e:
        logger.warning("[GeneratedFiles] get failed for %s: %s", download_id, e)
        return None
