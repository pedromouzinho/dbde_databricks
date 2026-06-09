# =============================================================================
# app.py — DBDE AI Assistant (Databricks Apps Edition)
# =============================================================================

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[App] Starting DBDE AI Assistant (Databricks Edition)...")

    try:
        from storage_databricks import init_pool, init_schema
        await init_pool()
        await init_schema()
        logger.info("[App] Storage initialized")
    except Exception as e:
        logger.warning("[App] Storage init failed (app works without persistence): %s", e)

    try:
        from tool_registry_databricks import register_all_tools
        register_all_tools()
    except Exception as e:
        logger.warning("[App] Tool registration partial failure: %s", e)

    logger.info("[App] Ready.")

    # Background: refresh DevOps search index (non-blocking)
    import asyncio

    async def _background_reindex():
        """Refresh the DevOps semantic index in Lakebase after startup — only when
        it is missing or stale (>24h), so restarts and scale-ups stay cheap."""
        await asyncio.sleep(5)  # let the app settle
        try:
            from datetime import datetime, timezone
            from tools_knowledge import (
                reindex_devops, _load_devops_index, _DEVOPS_AREA_PATHS,
            )

            def _age_hours(built_at) -> float:
                try:
                    dt = datetime.fromisoformat(str(built_at))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
                except Exception:
                    return 1e9  # unknown timestamp -> treat as stale

            # Skip if Lakebase already holds a fresh index (<24h). A local-file-only
            # index (Lakebase empty) does NOT count as fresh — we want it in Lakebase.
            existing = await _load_devops_index(max_age_s=0)
            if (existing and existing.get("_from_lakebase")
                    and _age_hours(existing.get("built_at")) < 24):
                logger.info("[Reindex] Lakebase index fresh (built %s, %d items) — skipping",
                            existing.get("built_at"), existing.get("count", 0))
                return

            # One call with the full area list -> a single deduped blob (a per-area
            # loop would overwrite the blob each time, keeping only the last area).
            result = await reindex_devops(area_path=list(_DEVOPS_AREA_PATHS), top=1000)
            if result.get("indexed"):
                logger.info("[Reindex] Complete: %d items in Lakebase (areas=%d)",
                            result.get("count", 0), len(result.get("areas", [])))
            else:
                logger.warning("[Reindex] Failed: %s", str(result.get("error", ""))[:150])
        except Exception as e:
            logger.warning("[Reindex] Background reindex failed (non-fatal): %s", e)

    asyncio.create_task(_background_reindex())

    yield

    logger.info("[App] Shutting down...")
    try:
        from llm_provider_databricks import close_clients
        await close_clients()
    except Exception:
        pass
    try:
        from storage_databricks import close_pool
        await close_pool()
    except Exception:
        pass


app = FastAPI(
    title="DBDE AI Assistant",
    description="AI Assistant powered by Databricks Foundation Models",
    version="8.0.0-databricks",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# STATIC FILES + FRONTEND
# =============================================================================

# Serve the chat UI at root
@app.get("/", response_class=HTMLResponse)
async def root():
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>DBDE AI Assistant</h1><p>Frontend not found.</p>")


# Serve static assets (vendored Plotly, etc.) under /static
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# API status endpoint
@app.get("/api/status")
async def api_status():
    return {"status": "ok", "service": "DBDE AI Assistant", "runtime": "databricks"}


@app.get("/health")
async def health():
    from config_databricks import LLM_ENDPOINT_STANDARD, DEVOPS_PAT
    from tool_registry_databricks import get_registered_tool_names
    return {
        "status": "healthy",
        "llm_endpoint": LLM_ENDPOINT_STANDARD,
        "devops_configured": bool(DEVOPS_PAT),
        "tools": get_registered_tool_names(),
    }


# Chat routes
try:
    from routes_chat_databricks import router as chat_router
    app.include_router(chat_router, prefix="/api", tags=["chat"])
except Exception as e:
    logger.error("[App] Failed to load chat routes: %s", e)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("[App] Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"error": str(exc)[:200]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
