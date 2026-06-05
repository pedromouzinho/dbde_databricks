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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

    sql = (
        f"INSERT INTO {_safe_table(table_name)} (partition_key, row_key, data, created_at, updated_at) "
        f"VALUES ($1, $2, $3, NOW(), NOW()) "
        f"ON CONFLICT (partition_key, row_key) DO UPDATE SET data = $3, updated_at = NOW()"
    )
    payload = json.dumps(data, default=str)
    async with _pool.acquire() as conn:
        try:
            await conn.execute(sql, partition_key, row_key, payload)
        except Exception as e:
            if _is_undefined_table(e):
                await _ensure_table(conn, table_name)
                await conn.execute(sql, partition_key, row_key, payload)
            else:
                raise
    return True


async def table_query(
    table_name: str,
    filter_expr: Optional[str] = None,
    top: int = 100,
    *,
    partition_key: Optional[str] = None,
    row_key: Optional[str] = None,
    filter_str: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Query rows from a table.
    Original: Azure Table Storage OData filter.
    Now: Postgres query.

    Backwards compatible with the original callers, which pass an Azure-style
    OData string as the second positional argument (or via ``filter_str=``),
    e.g. ``"PartitionKey eq 'x' and RowKey eq 'y'"``. These are translated to
    SQL against the ``partition_key``/``row_key`` columns and the JSONB ``data``
    column. ``partition_key``/``row_key`` may also be passed directly.
    """
    if not _pool:
        return []

    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if partition_key is not None:
        conditions.append(f"partition_key = ${idx}")
        params.append(partition_key)
        idx += 1
    if row_key is not None:
        conditions.append(f"row_key = ${idx}")
        params.append(row_key)
        idx += 1

    expr = filter_expr if filter_expr is not None else filter_str
    if expr:
        odata_conditions, odata_params = _odata_to_sql(expr, idx)
        conditions.extend(odata_conditions)
        params.extend(odata_params)
        idx += len(odata_params)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        f"SELECT partition_key, row_key, data, created_at, updated_at "
        f"FROM {_safe_table(table_name)} {where} "
        f"ORDER BY updated_at DESC LIMIT {int(top)}"
    )

    async with _pool.acquire() as conn:
        try:
            rows = await conn.fetch(sql, *params)
        except Exception as e:
            if _is_undefined_table(e):
                await _ensure_table(conn, table_name)
                return []
            raise

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
        try:
            await conn.execute(
                f"DELETE FROM {_safe_table(table_name)} WHERE partition_key = $1 AND row_key = $2",
                partition_key, row_key
            )
        except Exception as e:
            if _is_undefined_table(e):
                return False
            raise
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
    -- embedding column (vector(1024)) added separately iff pgvector is available
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
        # Enable pgvector extension if available (optional — search falls back to
        # cosine similarity computed in Python over JSONB-stored embeddings).
        has_vector = False
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            has_vector = True
        except Exception:
            logger.warning("[Storage] pgvector extension not available. Native vector search disabled.")
        # Base schema must not depend on pgvector, otherwise nothing gets created.
        await conn.execute(INIT_SCHEMA_SQL)
        if has_vector:
            try:
                await conn.execute("ALTER TABLE upload_index ADD COLUMN IF NOT EXISTS embedding vector(1024)")
            except Exception as e:
                logger.warning("[Storage] could not add embedding column: %s", e)
    logger.info("[Storage] Schema initialized successfully (pgvector=%s)", has_vector)


# =============================================================================
# HELPERS
# =============================================================================

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _safe_table(name: str) -> str:
    """
    Map an Azure-style table name to the Postgres table name and sanitize it.
    PascalCase/camelCase names are converted to snake_case so the original
    callers (e.g. ``WriterProfiles``, ``UploadIndex``, ``ChatHistory``) line up
    with the snake_case tables created in the schema (``writer_profiles``, ...).
    """
    snake = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    snake = _CAMEL_BOUNDARY_2.sub(r"\1_\2", snake).lower()
    return "".join(c for c in snake if c.isalnum() or c == "_")


# --- OData filter translation (Azure Table Storage -> Postgres) ---------------

_ODATA_OPS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
# Matches a single clause: Field op 'value'  (value may contain doubled '' quotes)
_ODATA_CLAUSE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(eq|ne|gt|ge|lt|le)\s+'(.*)'\s*$", re.DOTALL
)


def _odata_to_sql(filter_expr: str, start_idx: int = 1) -> Tuple[List[str], List[Any]]:
    """
    Translate a simple Azure OData filter into SQL conditions + params.
    Supports an AND-list of ``Field <op> 'value'`` comparisons (the only shape
    the codebase produces). ``PartitionKey``/``RowKey`` map to the dedicated
    columns; any other field maps to the JSONB ``data`` column. Unparseable
    clauses are skipped (fail-open rather than crash).
    """
    conditions: List[str] = []
    params: List[Any] = []
    idx = start_idx
    for clause in re.split(r"\s+and\s+", filter_expr.strip(), flags=re.IGNORECASE):
        m = _ODATA_CLAUSE.match(clause)
        if not m:
            continue
        field, op, value = m.group(1), m.group(2), m.group(3)
        value = value.replace("''", "'")
        sql_op = _ODATA_OPS.get(op, "=")
        if field == "PartitionKey":
            col = "partition_key"
        elif field == "RowKey":
            col = "row_key"
        else:
            # field is regex-guarded to [A-Za-z0-9_], safe to inline
            col = f"data->>'{field}'"
        conditions.append(f"{col} {sql_op} ${idx}")
        params.append(value)
        idx += 1
    return conditions, params


# --- Self-healing table creation ---------------------------------------------

def _is_undefined_table(exc: Exception) -> bool:
    """True if the error is Postgres 'relation does not exist' (table missing)."""
    return exc.__class__.__name__ == "UndefinedTableError" or "does not exist" in str(exc).lower()


async def _ensure_table(conn, table_name: str) -> None:
    """Create a generic key/value table on demand (matches the schema pattern)."""
    table = _safe_table(table_name)
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        f"partition_key TEXT NOT NULL, row_key TEXT NOT NULL, "
        f"data JSONB DEFAULT '{{}}'::jsonb, "
        f"created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(), "
        f"PRIMARY KEY (partition_key, row_key))"
    )
    logger.info("[Storage] Auto-created missing table '%s'", table)
