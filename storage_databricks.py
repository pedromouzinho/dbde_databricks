# =============================================================================
# storage_databricks.py — Lakebase (Postgres) storage layer
# =============================================================================
# Drop-in replacement for storage.py (Azure Table Storage + Blob).
# Uses Lakebase (managed Postgres in Databricks Apps) for:
#   - Structured data (conversations, users, feedback, etc.)
#   - JSON blobs (tool results, generated files stored as JSONB)
# =============================================================================

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

logger = logging.getLogger(__name__)

# =============================================================================
# CONNECTION MANAGEMENT
# =============================================================================

_pool: Optional[Any] = None


def _get_connection_string() -> str:
    """Build Postgres connection string from env vars (injected by Databricks Apps)."""
    # Lakebase injects PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "dbde_assistant")
    user = os.environ.get("PGUSER", "")
    password = os.environ.get("PGPASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def init_pool():
    """Initialize asyncpg connection pool. Call on app startup."""
    global _pool
    if not HAS_ASYNCPG:
        logger.warning("[Storage] asyncpg not installed. Using psycopg2 sync fallback.")
        return
    dsn = _get_connection_string()
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    logger.info("[Storage] Lakebase pool initialized")


async def close_pool():
    """Close connection pool on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# =============================================================================
# TABLE OPERATIONS (compatible interface with original storage.py)
# =============================================================================

async def table_insert(table_name: str, entity: Dict[str, Any]) -> bool:
    """
    Insert a row into a table.
    Original: Azure Table Storage insert (PartitionKey + RowKey + fields).
    Now: Postgres INSERT with JSONB data column.
    """
    if not _pool:
        logger.error("[Storage] Pool not initialized")
        return False

    partition_key = entity.get("PartitionKey", "default")
    row_key = entity.get("RowKey", "")
    # Store remaining fields as JSONB
    data = {k: v for k, v in entity.items() if k not in ("PartitionKey", "RowKey")}

    async with _pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO {_safe_table(table_name)} (partition_key, row_key, data, created_at, updated_at)
            VALUES ($1, $2, $3, NOW(), NOW())
            ON CONFLICT (partition_key, row_key) DO UPDATE
            SET data = $3, updated_at = NOW()
            """,
            partition_key, row_key, json.dumps(data, default=str)
        )
    return True


async def table_query(
    table_name: str,
    partition_key: Optional[str] = None,
    filter_expr: Optional[str] = None,
    top: int = 100,
) -> List[Dict[str, Any]]:
    """
    Query rows from a table.
    Original: Azure Table Storage OData filter.
    Now: Postgres query with optional partition filter.
    """
    if not _pool:
        return []

    conditions = []
    params = []
    param_idx = 1

    if partition_key:
        conditions.append(f"partition_key = ${param_idx}")
        params.append(partition_key)
        param_idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT partition_key, row_key, data, created_at, updated_at "
            f"FROM {_safe_table(table_name)} {where} "
            f"ORDER BY updated_at DESC LIMIT {int(top)}",
            *params
        )

    results = []
    for row in rows:
        entity = json.loads(row["data"]) if row["data"] else {}
        entity["PartitionKey"] = row["partition_key"]
        entity["RowKey"] = row["row_key"]
        entity["_created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        entity["_updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
        results.append(entity)
    return results


async def table_merge(table_name: str, entity: Dict[str, Any]) -> bool:
    """
    Merge (upsert) a row. Same as insert with ON CONFLICT UPDATE.
    """
    return await table_insert(table_name, entity)


async def table_delete(table_name: str, partition_key: str, row_key: str) -> bool:
    """Delete a specific row."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM {_safe_table(table_name)} WHERE partition_key = $1 AND row_key = $2",
            partition_key, row_key
        )
    return True


# =============================================================================
# BLOB OPERATIONS (stored as rows in a 'blobs' table with BYTEA/JSONB)
# =============================================================================

async def blob_upload_bytes(container: str, blob_name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to blob storage (now Lakebase blobs table)."""
    if not _pool:
        return ""
    ref = f"{container}/{blob_name}"
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO blobs (container, blob_name, content, content_type, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (container, blob_name) DO UPDATE
            SET content = $3, content_type = $4, created_at = NOW()
            """,
            container, blob_name, data, content_type
        )
    return ref


async def blob_upload_json(container: str, blob_name: str, obj: Any) -> str:
    """Upload JSON object as blob."""
    data = json.dumps(obj, default=str).encode("utf-8")
    return await blob_upload_bytes(container, blob_name, data, "application/json")


async def blob_download_bytes(container: str, blob_name: str) -> Optional[bytes]:
    """Download blob content."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content FROM blobs WHERE container = $1 AND blob_name = $2",
            container, blob_name
        )
    return row["content"] if row else None


async def blob_download_json(container: str, blob_name: str) -> Optional[Any]:
    """Download and parse JSON blob."""
    data = await blob_download_bytes(container, blob_name)
    if data:
        return json.loads(data.decode("utf-8"))
    return None


def parse_blob_ref(ref: str) -> tuple:
    """Parse 'container/blob_name' reference."""
    parts = ref.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ref




async def blob_delete(container: str, blob_name: str) -> bool:
    """Delete a blob."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM blobs WHERE container = $1 AND blob_name = $2",
            container, blob_name
        )
    return True


async def blob_list(container: str, prefix: str = "") -> list:
    """List blobs in a container (optional prefix filter)."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        if prefix:
            rows = await conn.fetch(
                "SELECT blob_name, content_type, created_at FROM blobs WHERE container = $1 AND blob_name LIKE $2 ORDER BY created_at DESC",
                container, f"{prefix}%"
            )
        else:
            rows = await conn.fetch(
                "SELECT blob_name, content_type, created_at FROM blobs WHERE container = $1 ORDER BY created_at DESC",
                container
            )
    return [{"name": r["blob_name"], "content_type": r["content_type"], "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows]

# =============================================================================
# SCHEMA INITIALIZATION (call on first deploy)
# =============================================================================

INIT_SCHEMA_SQL = """
-- Generic key-value table pattern (replaces Azure Table Storage)
CREATE TABLE IF NOT EXISTS conversations (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS users (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS feedback (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS chat_history (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS prompt_rules (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS writer_profiles (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS upload_index (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    embedding vector(1024),  -- for pgvector similarity search
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS token_quota (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

CREATE TABLE IF NOT EXISTS data_dictionary (
    partition_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (partition_key, row_key)
);

-- Blob storage table
CREATE TABLE IF NOT EXISTS blobs (
    container TEXT NOT NULL,
    blob_name TEXT NOT NULL,
    content BYTEA,
    content_type TEXT DEFAULT 'application/octet-stream',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (container, blob_name)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_history_partition ON chat_history (partition_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_blobs_container ON blobs (container, created_at DESC);
"""


async def init_schema():
    """Create all tables on first run."""
    if not _pool:
        logger.error("[Storage] Cannot init schema: pool not initialized")
        return
    async with _pool.acquire() as conn:
        # Enable pgvector extension if available
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:
            logger.warning("[Storage] pgvector extension not available. Vector search disabled.")
        await conn.execute(INIT_SCHEMA_SQL)
    logger.info("[Storage] Schema initialized successfully")


# =============================================================================
# HELPERS
# =============================================================================

def _safe_table(name: str) -> str:
    """Sanitize table name to prevent SQL injection."""
    clean = "".join(c for c in name.lower() if c.isalnum() or c == "_")
    return clean
