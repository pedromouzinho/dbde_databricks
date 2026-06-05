# DBDE AI Assistant — Complete Migration Context

> **Purpose:** This document provides full context for any AI agent (Claude Code, Copilot, etc.)
> working on this codebase. Read this FIRST before making any changes.

---

## 1. PROJECT OVERVIEW

**What is this:** An internal AI assistant for the DBDE team (Direcção de Banca Digital e Everyday)
at Millennium BCP (largest Portuguese bank). It helps product managers and engineers with DevOps
queries, user story generation, file exports, presentations, and more.

**Migration:** Originally built as a React SPA + FastAPI backend on Azure (Azure OpenAI, Azure Table
Storage, Azure AI Search, Azure Blob Storage). Now migrated to run as a **Databricks App** using
Databricks-native services (Foundation Model APIs, Lakebase/PostgreSQL, workspace storage).

**Language:** The assistant communicates in **Portuguese (PT-PT)**. All system prompts, UI text,
and user-facing messages must be in Portuguese.

---

## 2. DEPLOYMENT ENVIRONMENT

| Item | Value |
|------|-------|
| Platform | Databricks Apps (Azure) |
| Workspace | `https://adb-1476615256855539.19.azuredatabricks.net` |
| App name | `dbde-assistant` |
| App URL | `https://dbde-assistant-1476615256855539.19.azure.databricksapps.com` |
| Compute size | MEDIUM (~1.5GB RAM, 1 vCPU) |
| Runtime | Python 3.11+ with uvicorn |
| Source path (workspace) | `/Workspace/Users/x251367@bcpcorp.net/dbde-assistant-databricks` |
| Git Folder (synced) | `/Workspace/Users/x251367@bcpcorp.net/dbde_databricks` |
| GitHub repo | `https://github.com/pedromouzinho/dbde_databricks` |
| Service principal | `app-5jct3z dbde-assistant` (id: `51a92d07-1e1b-417b-aa66-bccfbe3e1d55`) |

### How to Deploy

```bash
# From Databricks CLI or SDK:
databricks apps deploy dbde-assistant --source-code-path /Workspace/Users/x251367@bcpcorp.net/dbde_databricks

# Or via REST API:
POST /api/2.0/apps/dbde-assistant/deployments
{"source_code_path": "/Workspace/Users/x251367@bcpcorp.net/dbde_databricks", "mode": "SNAPSHOT"}
```

After pushing to GitHub, sync the Databricks Git Folder then deploy from it.

---

## 3. AUTHENTICATION MODEL (CRITICAL)

Databricks Apps do NOT get a `DATABRICKS_TOKEN` at runtime. Instead they get:
- `DATABRICKS_CLIENT_ID` — Service principal client ID
- `DATABRICKS_CLIENT_SECRET` — Service principal secret
- `DATABRICKS_HOST` — Workspace hostname (WITHOUT `https://` prefix!)

The `llm_provider_databricks.py` implements OAuth M2M (machine-to-machine):
1. POST to `https://{host}/oidc/v1/token` with `client_credentials` grant
2. Gets back a bearer token (expires in 3600s)
3. Token is cached in `_oauth_token_cache` dict and auto-refreshed
4. OpenAI client is recreated when token changes

**Key gotcha:** `DATABRICKS_HOST` may or may not include `https://`. The code handles both via
`_get_base_url()` which adds the prefix if missing.

---

## 4. LLM CONFIGURATION

| Tier | Endpoint | Model | Notes |
|------|----------|-------|-------|
| Standard | `databricks-claude-sonnet-4-6` | Claude Sonnet 4 | Default tier |
| Fast | `databricks-claude-haiku-4-5` | Claude Haiku 4.5 | Quick responses |
| Pro | `databricks-claude-opus-4-8` | Claude Opus 4 | **Does NOT support `temperature` param** |
| Vision | `databricks-claude-sonnet-4-6` | Same as standard | Not fully implemented |
| Embedding | `databricks-gte-large-en` | GTE Large | 1024 dimensions |

**Fallback chain:** Pro → Standard → Fast. If Opus fails, it tries Sonnet, then Haiku.

**Important:** When calling Opus, do NOT pass `temperature` in the API call — it returns 400.
The code already handles this with: `if "opus" not in endpoint.lower(): call_kwargs["temperature"] = temperature`

---

## 5. ARCHITECTURE

```
┌─────────────────────────────────────────────────────┐
│  Browser (static/index.html)                         │
│  - Vanilla JS, single HTML file                      │
│  - Streaming SSE for real-time responses             │
│  - Millennium brand (#D1005D, Manrope font)          │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP
┌──────────────────────▼──────────────────────────────┐
│  FastAPI (app.py)                                    │
│  - Serves static/ at GET /                           │
│  - Mounts router at /api prefix                      │
│  - Health: /health, /api/status                      │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  routes_chat_databricks.py (API Router)              │
│  Endpoints:                                          │
│  - POST /api/chat (streaming SSE + non-streaming)    │
│  - GET  /api/conversations                           │
│  - GET  /api/conversations/{id}/messages             │
│  - DELETE /api/conversation/{id}                     │
│  - POST /api/feedback                                │
│  - POST /api/upload                                  │
│  - GET  /api/download/{id}                           │
│  - GET  /api/tools                                   │
│  - GET  /api/test-llm                                │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Agent Loop (_run_agent_loop / _stream_agent_loop)   │
│  - Up to 15 iterations                               │
│  - Calls LLM with tool definitions                   │
│  - Executes tools, feeds results back                │
│  - Streaming: yields SSE events as tokens arrive     │
└───────────┬──────────────────────┬──────────────────┘
            │                      │
┌───────────▼────────┐  ┌─────────▼──────────────────┐
│  llm_provider      │  │  tool_registry             │
│  - OAuth M2M       │  │  - 10 tools registered     │
│  - Fallback chain  │  │  - Adapter pattern         │
│  - Stream support  │  │  - Resilient registration  │
└────────────────────┘  └─────────┬──────────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            │                     │                     │
    ┌───────▼───────┐  ┌─────────▼────────┐  ┌────────▼────────┐
    │ tools_devops  │  │ code_interpreter  │  │ tools_email     │
    │ tools_figma   │  │ (subprocess)      │  │ tools_export    │
    │ tools_miro    │  │                   │  │ tools_knowledge │
    └───────────────┘  └───────────────────┘  └─────────────────┘
```

---

## 6. FILE REFERENCE

### Core App
| File | Purpose |
|------|---------|
| `app.py` | FastAPI app entry point. Lifespan init, static file serving, mounts router. |
| `app.yaml` | Databricks App manifest. Command, env vars, resources (LLM endpoints, Lakebase). |
| `config_databricks.py` | All configuration. Tier mapping, fallback chains, env var loading. |
| `requirements.txt` | Python dependencies (fastapi, uvicorn, httpx, openai, openpyxl, etc.) |

### API Layer
| File | Purpose |
|------|---------|
| `routes_chat_databricks.py` | All HTTP endpoints. Agent loop (streaming + non-streaming). In-memory stores for conversations, files, feedback. |

### LLM & Auth
| File | Purpose |
|------|---------|
| `llm_provider_databricks.py` | OAuth M2M token acquisition, OpenAI client management, `llm_with_fallback()`, `llm_stream_with_fallback()`, `llm_simple()` |
| `token_counter.py` | Tiktoken-based token counting for context management |

### Tools (10 registered)
| File | Tools | External Dependency |
|------|-------|---------------------|
| `code_interpreter.py` | `code_interpreter` | None (subprocess) |
| `tools_knowledge.py` | `search_knowledge` | Lakebase (pgvector) |
| `tools_devops.py` | `query_workitems`, `generate_user_stories` | Azure DevOps REST API |
| `tools_figma.py` | `search_figma` | Figma REST API |
| `tools_miro.py` | `search_miro` | Miro REST API |
| `tools_export.py` | `generate_file`, `generate_chart`, `generate_presentation` | None |
| `tools_email.py` | `prepare_outlook_draft` | None (generates .cmd file) |

### Storage & Files
| File | Purpose |
|------|---------|
| `storage_databricks.py` | Blob operations (upload/download/list/delete), table queries via Lakebase |
| `generated_files.py` | In-memory store for generated files (PPTX, XLSX, PDF, .cmd). TTL-based expiry. `get_generated_file_sync()` for route handler. |

### Export Engines
| File | Purpose |
|------|---------|
| `export_engine.py` | Orchestrates file generation (routes to pptx/xlsx engines) |
| `pptx_engine.py` | PowerPoint generation (python-pptx) |
| `xlsx_engine.py` | Excel generation (openpyxl) |

### Data Processing
| File | Purpose |
|------|---------|
| `tabular_loader.py` | Loads CSV/XLSX files for preview. `load_tabular_preview(raw_bytes, filename)` |
| `tabular_artifacts.py` | Advanced tabular processing |
| `data_dictionary.py` | Schema/metadata for uploaded datasets |

### Other
| File | Purpose |
|------|---------|
| `models.py` | Pydantic models (shared across modules) |
| `structured_schemas.py` | JSON schemas for structured LLM outputs |
| `http_helpers.py` | HTTP utility functions (retry, timeout) |
| `utils.py` | Small utilities (odata_escape, etc.) |
| `pii_shield.py` | PII masking stubs (disabled) |
| `prompt_shield.py` | Prompt injection protection stubs (disabled) |
| `auth.py`, `azure_auth.py` | Auth stubs (no-op, kept for import compatibility) |
| `request_context.py` | Request context stubs |
| `chat_ttl.py` | Conversation TTL management |
| `learning.py`, `tools_learning.py` | Learning/memory subsystem (not fully active) |
| `tools_upload.py` | Upload processing utilities |

### Frontend
| File | Purpose |
|------|---------|
| `static/index.html` | Single-file vanilla JS frontend (~22KB). CSS + HTML + JS. |

---

## 7. EXTERNAL SERVICES & TOKENS

| Service | Purpose | Token Location |
|---------|---------|----------------|
| Azure DevOps | Work items, sprints, user stories | `DEVOPS_PAT` in app.yaml env |
| Azure DevOps | Org: `ptbcp`, Project: `IT.DIT` | `DEVOPS_ORG`, `DEVOPS_PROJECT` in app.yaml |
| Figma | Design search & inspection | `FIGMA_ACCESS_TOKEN` in app.yaml env |
| Miro | Board search | `MIRO_ACCESS_TOKEN` in app.yaml env |
| Databricks LLM | Chat completions | OAuth M2M (automatic via service principal) |
| Databricks Lakebase | Vector search, conversation storage | `lakebase` resource in app.yaml |

**Note:** The `app.yaml` in GitHub has tokens REDACTED. The real tokens are only in the
workspace-local copy. Never commit real tokens to GitHub.

---

## 8. KNOWN ISSUES & LIMITATIONS

### Current Bugs
1. **MemoryError on large Excel files** — The MEDIUM compute has ~1.5GB RAM. Large .xlsx files
   (>20MB) cause openpyxl to OOM in the code_interpreter subprocess. Mitigation: system prompt
   tells LLM to use `pd.read_excel(..., nrows=100)` or `openpyxl read_only=True`.

2. **Conversations are in-memory** — All conversations, uploaded files, and feedback are stored
   in Python dicts. They reset on every deploy/restart. A future improvement would persist to
   Lakebase.

3. **Streaming message persistence** — Fixed: `_stream_agent_loop` now appends the final assistant
   message to `conv["messages"]` before yielding the `done` event.

### Architecture Limitations
- **No persistent storage for conversations** — resets on deploy
- **No authentication/multi-user** — anyone with the URL can use it (Databricks Apps have SSO but user identity isn't passed to the app)
- **Code interpreter runs in subprocess** — limited to packages in requirements.txt
- **File upload is in-memory** — large files consume app RAM
- **No Microsoft Graph** — email tool generates .cmd files instead of creating Outlook drafts directly

### Not Migrated (Lower Priority)
- `user_story_lane.py` — User Story workspace (view/edit/publish)
- `presentation_orchestrator.py` — Advanced presentation flows
- Original React SPA (28 components, 121K App.jsx + 79K CSS) — replaced with vanilla JS
- Speech-to-text
- Digest panel (daily summary)
- Image upload + vision analysis

---

## 9. TOOL REGISTRY PATTERN

Tools are registered in `tool_registry_databricks.py` using a resilient pattern:

```python
def register_all_tools():
    # Always available
    _register_code_interpreter()
    _register_knowledge_search()
    
    # Conditional on tokens
    if os.environ.get("DEVOPS_PAT"):
        _register_devops_tools()  # query_workitems, generate_user_stories
    if os.environ.get("FIGMA_ACCESS_TOKEN"):
        _register_figma_tools()
    # ... etc
```

Each group is wrapped in try/except so one failure doesn't break others.

The **adapter pattern** is used for DevOps: `_query_workitems_adapter` converts LLM parameters
(`query`, `id`, `state`, `type`, `area_path`, `top`) into a WIQL WHERE clause.

---

## 10. FRONTEND ARCHITECTURE

Single HTML file (`static/index.html`) with:
- **CSS:** Millennium brand colors (#D1005D), Manrope font, responsive
- **HTML:** Shell layout (sidebar + main + input area)
- **JavaScript:** ~400 lines vanilla JS

Key features implemented:
- Streaming SSE (token-by-token rendering via `EventSource`-like fetch reader)
- Sidebar with conversations (fetched from `/api/conversations`)
- Tier selector (Standard/Fast/Pro) — sends tier in chat request
- File upload (multipart POST to `/api/upload`)
- Tool execution feedback (spinner + tool name during streaming)
- Stop generation (AbortController)
- Copy message button
- Feedback buttons (POST to `/api/feedback`)
- Markdown rendering via marked.js CDN
- Contextual suggestions (empty state)

Key features NOT implemented:
- Streaming via native EventSource (uses fetch + ReadableStream instead)
- File download UI for generated files (backend endpoint exists)
- Quick replies / clarification options
- Tool execution time display

---

## 11. APP.YAML STRUCTURE

```yaml
command:
  - uvicorn
  - app:app
  - --host
  - 0.0.0.0
  - --port
  - "8000"

env:
  - name: APP_ENV
    value: databricks
  - name: DEVOPS_PAT
    value: "<real-token>"  # Redacted in GitHub
  - name: DEVOPS_ORG
    value: "ptbcp"
  - name: DEVOPS_PROJECT
    value: "IT.DIT"
  # ... more env vars

resources:
  - name: llm_primary
    type: serving_endpoint
    serving_endpoint_name: databricks-claude-sonnet-4-6
    permission: CAN_QUERY
  - name: llm_fast
    type: serving_endpoint
    serving_endpoint_name: databricks-claude-haiku-4-5
    permission: CAN_QUERY
  - name: llm_pro
    type: serving_endpoint
    serving_endpoint_name: databricks-claude-opus-4-8
    permission: CAN_QUERY
  - name: embedding
    type: serving_endpoint
    serving_endpoint_name: databricks-gte-large-en
    permission: CAN_QUERY
  - name: lakebase
    type: lakebase_database
    permission: CAN_CONNECT_AND_CREATE
```

---

## 12. DEVELOPMENT WORKFLOW

### Making Changes
1. Edit files in the Git Folder or push to GitHub
2. If pushed to GitHub: sync Git Folder via `w.repos.update(repo_id=4007175370069213, branch="main")`
3. Deploy: `POST /api/2.0/apps/dbde-assistant/deployments` with source path
4. Check logs at: `https://dbde-assistant-1476615256855539.19.azure.databricksapps.com/logz`

### Testing Locally (in notebook)
```python
import sys
sys.path.insert(0, "/Workspace/Users/x251367@bcpcorp.net/dbde-assistant-databricks")
os.environ["DEVOPS_PAT"] = "..."
import routes_chat_databricks as rc
# Check routes: [r.path for r in rc.router.routes]
```

### Common Pitfalls
- **Don't use `open(path).read()` inside `open(path, "w")`** — empties the file
- **Always check `python-multipart` is in requirements.txt** — needed for file uploads
- **Opus doesn't support temperature** — check before adding new LLM calls
- **`load_tabular_preview` signature is `(raw_bytes, filename)`** — not a file path
- **`generated_files.get_generated_file` is async** — use `get_generated_file_sync` in route handlers
- **Secret scope `DataBricksKVScopeDataQ` is Azure KeyVault-backed** — cannot write via CLI/SDK, read-only

---

## 13. BRAND GUIDELINES

- Primary color: `#D1005D` (Millennium magenta)
- Font: Manrope (Google Fonts)
- Tone: Professional, concise, action-oriented
- Language: Portuguese (PT-PT), never Brazilian Portuguese
- The assistant name: "DBDE Assistant" with subtitle "Databricks Edition"
- Icon: Letter "M" in magenta circle with rounded corners

---

## 14. PRIORITY IMPROVEMENTS (TODO)

1. **Persist conversations to Lakebase** — survive deploys
2. **File download UI** — render download buttons when tools generate files
3. **Quick replies** — when LLM suggests actions, render as clickable buttons
4. **Better error messages** — show user-friendly errors, retry button
5. **Upgrade to LARGE compute** — if Excel MemoryError persists
6. **Add health monitoring** — structured logging, metrics endpoint

---

## 15. SECRET SCOPE & TOKENS REFERENCE

| Secret/Token | Where | Value (redacted) |
|-------------|-------|------------------|
| DEVOPS_PAT | app.yaml env | `A2tY6Q...` (Azure DevOps PAT for ptbcp org) |
| FIGMA_ACCESS_TOKEN | app.yaml env | `figd_xLK8...` |
| MIRO_ACCESS_TOKEN | app.yaml env | `eyJtaXJv...` |
| DATABRICKS_CLIENT_ID | Auto (runtime) | Service principal ID |
| DATABRICKS_CLIENT_SECRET | Auto (runtime) | Service principal secret |
| DATABRICKS_HOST | Auto (runtime) | `adb-1476615256855539.19.azuredatabricks.net` |

---

*Last updated: 2026-06-05*
*Commit: 15aa94bf (initial push to GitHub)*
