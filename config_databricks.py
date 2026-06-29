# =============================================================================
# config_databricks.py — Configuração para Databricks Apps runtime
# =============================================================================
# Substitui config.py original. Lê de:
# 1. Databricks App resources (env vars injectados automaticamente)
# 2. Databricks Secrets (via env vars mapeados no app.yaml)
# 3. Env vars padrão (fallbacks)
# =============================================================================

import os
import logging

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    """Lê env var com fallback."""
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else default


# =============================================================================
# DATABRICKS WORKSPACE
# =============================================================================
DATABRICKS_HOST = _get_env("DATABRICKS_HOST", "https://adb-1476615256855539.19.azuredatabricks.net")
DATABRICKS_TOKEN = _get_env("DATABRICKS_TOKEN", "")  # Auto-injected in Apps

# =============================================================================
# LLM ENDPOINTS (Databricks Foundation Model APIs)
# =============================================================================
# Endpoints are accessed via OpenAI-compatible API at:
# {DATABRICKS_HOST}/serving-endpoints/{endpoint_name}/invocations
#
# Tier mapping (preserves original tier system):
#   fast     -> Claude Haiku 4.5 (cheap, fast)
#   standard -> Claude Sonnet 4.6 (balanced)
#   pro      -> Claude Opus 4.8 (max capability)
#   vision   -> Claude Sonnet 4.6 (multimodal)
# =============================================================================

LLM_ENDPOINT_FAST = _get_env("LLM_ENDPOINT_FAST", "databricks-claude-haiku-4-5")
LLM_ENDPOINT_STANDARD = _get_env("LLM_ENDPOINT_STANDARD", "databricks-claude-sonnet-4-6")
LLM_ENDPOINT_PRO = _get_env("LLM_ENDPOINT_PRO", "databricks-claude-opus-4-8")
LLM_ENDPOINT_VISION = _get_env("LLM_ENDPOINT_VISION", "databricks-claude-sonnet-4-6")

# Tier aliases (backward compat with original config)
LLM_DEFAULT_TIER = _get_env("LLM_DEFAULT_TIER", "standard")
LLM_TIER_FAST = "fast"
LLM_TIER_STANDARD = "standard"
LLM_TIER_PRO = "pro"
LLM_TIER_VISION = "vision"
LLM_FALLBACK = _get_env("LLM_FALLBACK", "true").lower() == "true"

# Map tier -> endpoint name
TIER_TO_ENDPOINT = {
    LLM_TIER_FAST: LLM_ENDPOINT_FAST,
    LLM_TIER_STANDARD: LLM_ENDPOINT_STANDARD,
    LLM_TIER_PRO: LLM_ENDPOINT_PRO,
    LLM_TIER_VISION: LLM_ENDPOINT_VISION,
}

# Fallback chain: pro -> standard -> fast
FALLBACK_CHAIN = {
    LLM_TIER_PRO: [LLM_ENDPOINT_PRO, LLM_ENDPOINT_STANDARD, LLM_ENDPOINT_FAST],
    LLM_TIER_STANDARD: [LLM_ENDPOINT_STANDARD, LLM_ENDPOINT_FAST],
    LLM_TIER_FAST: [LLM_ENDPOINT_FAST],
    LLM_TIER_VISION: [LLM_ENDPOINT_VISION, LLM_ENDPOINT_STANDARD],
}

# =============================================================================
# EMBEDDINGS
# =============================================================================
EMBEDDING_ENDPOINT = _get_env("EMBEDDING_ENDPOINT", "databricks-gte-large-en")
EMBEDDING_VECTOR_DIMENSIONS = 1024  # GTE Large output dim

# =============================================================================
# AGENT PARAMETERS (same as original)
# =============================================================================
AGENT_MAX_ITERATIONS = int(_get_env("AGENT_MAX_ITERATIONS", "15"))
AGENT_MAX_TOKENS = int(_get_env("AGENT_MAX_TOKENS", "4096"))
AGENT_TEMPERATURE = float(_get_env("AGENT_TEMPERATURE", "0.3"))
AGENT_HISTORY_LIMIT = int(_get_env("AGENT_HISTORY_LIMIT", "30"))
AGENT_TOOL_RESULT_MAX_SIZE = int(_get_env("AGENT_TOOL_RESULT_MAX_SIZE", "8000"))
AGENT_TOOL_RESULT_KEEP_ITEMS = int(_get_env("AGENT_TOOL_RESULT_KEEP_ITEMS", "10"))
# Per tool-call timeout (seconds): a hung tool returns an error instead of blocking
# the agent loop forever. Heavy tools override this in the registry.
TOOL_CALL_TIMEOUT_SECONDS = int(_get_env("TOOL_CALL_TIMEOUT_SECONDS", "90"))

# =============================================================================
# LAKEBASE (Postgres) — replaces Azure Table Storage + Blob Storage
# =============================================================================
# Connection string injected by Databricks Apps resource system
LAKEBASE_CONNECTION_STRING = _get_env("LAKEBASE_CONN_STR", "")
# If not available, construct from individual parts
LAKEBASE_HOST = _get_env("PGHOST", "")
LAKEBASE_PORT = _get_env("PGPORT", "5432")
LAKEBASE_DB = _get_env("PGDATABASE", "")
LAKEBASE_USER = _get_env("PGUSER", "")
LAKEBASE_PASSWORD = _get_env("PGPASSWORD", "")

# =============================================================================
# EXTERNAL APIS (tokens from Databricks Secrets / KeyVault)
# =============================================================================
# Azure DevOps
DEVOPS_PAT = _get_env("DEVOPS_PAT", "")
DEVOPS_ORG = _get_env("DEVOPS_ORG", "ptbcp")
DEVOPS_PROJECT = _get_env("DEVOPS_PROJECT", "IT.DIT")
DEVOPS_AREAS = [a.strip() for a in _get_env("DEVOPS_AREAS", "").split(",") if a.strip()]
DEVOPS_WORKITEM_TYPES = [t.strip() for t in _get_env("DEVOPS_WORKITEM_TYPES", "User Story,Bug,Task,Feature,Epic").split(",")]
DEVOPS_FIELDS = [
    "System.Id", "System.Title", "System.State",
    "System.WorkItemType", "System.AreaPath",
    "System.IterationPath", "System.AssignedTo", "System.CreatedBy",
    "System.CreatedDate", "System.ChangedDate",
]

# Figma
FIGMA_ACCESS_TOKEN = _get_env("FIGMA_ACCESS_TOKEN", "")

# Miro
MIRO_ACCESS_TOKEN = _get_env("MIRO_ACCESS_TOKEN", "")

# =============================================================================
# FEATURE FLAGS
# =============================================================================
# PII masking is a no-op stub in Databricks mode (internal, data stays on platform).
# Default off to match pii_shield.py and app.yaml; flip to "true" only if a real shield is wired.
PII_ENABLED = _get_env("PII_ENABLED", "false").lower() == "true"
CODE_INTERPRETER_ENABLED = _get_env("CODE_INTERPRETER_ENABLED", "true").lower() == "true"
CODE_INTERPRETER_TIMEOUT = int(_get_env("CODE_INTERPRETER_TIMEOUT", "30"))
CODE_INTERPRETER_MAX_OUTPUT = int(_get_env("CODE_INTERPRETER_MAX_OUTPUT", "50000"))
CODE_INTERPRETER_MAX_INPUT_FILE_BYTES = int(_get_env("CODE_INTERPRETER_MAX_INPUT_FILE_BYTES", "10485760"))
CODE_INTERPRETER_MAX_MOUNT_BYTES = int(_get_env("CODE_INTERPRETER_MAX_MOUNT_BYTES", "52428800"))
VISION_ENABLED = _get_env("VISION_ENABLED", "true").lower() == "true"
# Chat attachments: send pasted/uploaded images to the model as native vision blocks
# (image_url), extract a searchable transcription from uploaded images via the vision
# endpoint, and accept video by extracting keyframes in the browser (no server deps).
CHAT_VISION_ATTACH_ENABLED = _get_env("CHAT_VISION_ATTACH_ENABLED", "true").lower() == "true"
IMAGE_INGEST_OCR_ENABLED = _get_env("IMAGE_INGEST_OCR_ENABLED", "true").lower() == "true"
VIDEO_ATTACH_ENABLED = _get_env("VIDEO_ATTACH_ENABLED", "true").lower() == "true"
# Max keyframes extracted (client-side) per attached video.
VIDEO_MAX_FRAMES = int(_get_env("VIDEO_MAX_FRAMES", "6"))
# Reject videos larger than this for storage/playback (default 25 MB).
VIDEO_MAX_BYTES = int(_get_env("VIDEO_MAX_BYTES", "26214400"))
# Downscale target (longest edge, px) for keyframes/attachments — keeps payloads small.
ATTACHMENT_IMAGE_MAX_EDGE = int(_get_env("ATTACHMENT_IMAGE_MAX_EDGE", "1568"))
# Max image blocks attached to a single chat turn (Databricks Claude allows up to 100).
CHAT_ATTACH_MAX_IMAGES = int(_get_env("CHAT_ATTACH_MAX_IMAGES", "10"))
RERANK_ENABLED = _get_env("RERANK_ENABLED", "false").lower() == "true"
WEB_SEARCH_ENABLED = _get_env("WEB_SEARCH_ENABLED", "false").lower() == "true"

# =============================================================================
# UPLOAD / FILES
# =============================================================================
UPLOAD_MAX_IMAGES_PER_MESSAGE = int(_get_env("UPLOAD_MAX_IMAGES_PER_MESSAGE", "5"))
UPLOAD_INDEX_TOP = int(_get_env("UPLOAD_INDEX_TOP", "10"))
GENERATED_FILES_BLOB_CONTAINER = "generated-files"  # now a Lakebase table or local dir
CHAT_TOOLRESULT_BLOB_CONTAINER = "chat-toolresults"

# =============================================================================
# MISC
# =============================================================================
DEBUG_LOG_SIZE = int(_get_env("DEBUG_LOG_SIZE", "200"))
TOKEN_QUOTA_ENFORCEMENT_ENABLED = _get_env("TOKEN_QUOTA_ENFORCEMENT_ENABLED", "false").lower() == "true"
USER_DAILY_TOKEN_LIMIT = int(_get_env("USER_DAILY_TOKEN_LIMIT", "500000"))
WRITER_PROFILE_LEARN_THRESHOLD = int(_get_env("WRITER_PROFILE_LEARN_THRESHOLD", "3"))

logger.info("[Config] Databricks mode active. Primary LLM: %s, Embedding: %s", LLM_ENDPOINT_STANDARD, EMBEDDING_ENDPOINT)

# =============================================================================
# EXPORT / FILE GENERATION
# =============================================================================
EXPORT_BRAND_COLOR = _get_env("EXPORT_BRAND_COLOR", "#C8102E")
EXPORT_BRAND_NAME = _get_env("EXPORT_BRAND_NAME", "DBDE AI Assistant")
EXPORT_AGENT_NAME = _get_env("EXPORT_AGENT_NAME", "Assistente AI")
APP_VERSION = _get_env("APP_VERSION", "8.0.0-databricks")
EXPORT_ASYNC_THRESHOLD_ROWS = int(_get_env("EXPORT_ASYNC_THRESHOLD_ROWS", "5000"))
EXPORT_FILE_ROW_CAP = int(_get_env("EXPORT_FILE_ROW_CAP", "10000"))
EXPORT_FILE_ROW_CAP_MAX = int(_get_env("EXPORT_FILE_ROW_CAP_MAX", "50000"))
PPTX_LEGACY_PLANNER_ENABLED = _get_env("PPTX_LEGACY_PLANNER_ENABLED", "true").lower() == "true"
PPTX_VNEXT_ENABLED = _get_env("PPTX_VNEXT_ENABLED", "false").lower() == "true"
GENERATED_FILE_TTL_SECONDS = int(_get_env("GENERATED_FILE_TTL_SECONDS", "3600"))

# =============================================================================
# UPLOAD LIMITS
# =============================================================================
UPLOAD_MAX_FILE_BYTES = int(_get_env("UPLOAD_MAX_FILE_BYTES", "52428800"))
UPLOAD_MAX_FILE_BYTES_CSV = int(_get_env("UPLOAD_MAX_FILE_BYTES_CSV", "20971520"))
UPLOAD_MAX_FILE_BYTES_TSV = int(_get_env("UPLOAD_MAX_FILE_BYTES_TSV", "20971520"))
UPLOAD_MAX_FILE_BYTES_XLSX = int(_get_env("UPLOAD_MAX_FILE_BYTES_XLSX", "20971520"))
UPLOAD_MAX_FILE_BYTES_XLSB = int(_get_env("UPLOAD_MAX_FILE_BYTES_XLSB", "20971520"))
UPLOAD_MAX_FILE_BYTES_XLS = int(_get_env("UPLOAD_MAX_FILE_BYTES_XLS", "10485760"))
UPLOAD_TABULAR_ARTIFACT_BATCH_ROWS = int(_get_env("UPLOAD_TABULAR_ARTIFACT_BATCH_ROWS", "500"))

# =============================================================================
# CHAT HISTORY
# =============================================================================
CHAT_HISTORY_TTL_DAYS = int(_get_env("CHAT_HISTORY_TTL_DAYS", "90"))

# =============================================================================
# SEARCH / RAG (pgvector replaces Azure AI Search in Databricks mode)
# =============================================================================
SEARCH_SERVICE = _get_env("SEARCH_SERVICE", "")
SEARCH_KEY = _get_env("SEARCH_KEY", "")
API_VERSION_SEARCH = _get_env("API_VERSION_SEARCH", "2024-07-01")
DEVOPS_INDEX = _get_env("DEVOPS_INDEX", "devops-workitems")
OMNI_INDEX = _get_env("OMNI_INDEX", "omni-knowledge")
EXAMPLES_INDEX = _get_env("EXAMPLES_INDEX", "few-shot-examples")
RERANK_ENDPOINT = _get_env("RERANK_ENDPOINT", "")
RERANK_API_KEY = _get_env("RERANK_API_KEY", "")
RERANK_MODEL = _get_env("RERANK_MODEL", "")
RERANK_TOP_N = int(_get_env("RERANK_TOP_N", "5"))
RERANK_TIMEOUT_SECONDS = int(_get_env("RERANK_TIMEOUT_SECONDS", "10"))
RERANK_AUTH_MODE = _get_env("RERANK_AUTH_MODE", "api-key")
WEB_SEARCH_API_KEY = _get_env("WEB_SEARCH_API_KEY", "")
WEB_SEARCH_ENDPOINT = _get_env("WEB_SEARCH_ENDPOINT", "")
WEB_SEARCH_MAX_RESULTS = int(_get_env("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_MARKET = _get_env("WEB_SEARCH_MARKET", "pt-PT")
WEB_ANSWERS_ENABLED = _get_env("WEB_ANSWERS_ENABLED", "false").lower() == "true"
WEB_ANSWERS_API_KEY = _get_env("WEB_ANSWERS_API_KEY", "")
WEB_ANSWERS_ENDPOINT = _get_env("WEB_ANSWERS_ENDPOINT", "")
WEB_ANSWERS_MODEL = _get_env("WEB_ANSWERS_MODEL", "")
WEB_ANSWERS_TIMEOUT_SECONDS = int(_get_env("WEB_ANSWERS_TIMEOUT_SECONDS", "15"))
