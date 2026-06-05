# =============================================================================
# chat_ttl.py — Auto-purge expired ChatHistory entries (SEC-09)
# =============================================================================
# Runs as a background task on startup, then every 24h.
# Deletes chat entries older than CHAT_HISTORY_TTL_DAYS.
# =============================================================================

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from config_databricks import CHAT_HISTORY_TTL_DAYS
from storage_databricks import table_query, table_delete

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 24 * 3600  # 24h


async def cleanup_expired_chats() -> int:
    """Delete ChatHistory entries older than TTL. Returns count deleted."""
    if CHAT_HISTORY_TTL_DAYS <= 0:
        logger.info("[ChatTTL] TTL disabled (CHAT_HISTORY_TTL_DAYS=%d).", CHAT_HISTORY_TTL_DAYS)
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=CHAT_HISTORY_TTL_DAYS)
    cutoff_iso = cutoff.isoformat()

    try:
        deleted = 0
        batch_size = 200
        # Server-side OData filter: only fetch rows older than cutoff
        odata_filter = f"UpdatedAt lt '{cutoff_iso}'"
        while True:
            rows = await table_query("ChatHistory", filter_str=odata_filter, top=batch_size)
            if not rows:
                break
            for row in rows:
                pk = str(row.get("PartitionKey", ""))
                rk = str(row.get("RowKey", ""))
                if pk and rk:
                    await table_delete("ChatHistory", pk, rk)
                    deleted += 1
            # If batch returned fewer than batch_size, all expired rows processed
            if len(rows) < batch_size:
                break

        if deleted:
            logger.info("[ChatTTL] Purged %d expired chat(s) (older than %d days).", deleted, CHAT_HISTORY_TTL_DAYS)
        return deleted
    except Exception as e:
        logger.error("[ChatTTL] cleanup_expired_chats failed: %s", e)
        return 0


async def chat_ttl_background_loop():
    """Background loop: run cleanup on startup, then every 24h."""
    # Wait 60s after startup to avoid contention
    await asyncio.sleep(60)
    while True:
        try:
            await cleanup_expired_chats()
        except Exception as e:
            logger.error("[ChatTTL] background loop error: %s", e)
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
