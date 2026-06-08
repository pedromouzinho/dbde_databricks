# =============================================================================
# routes_chat_databricks.py — Complete Chat API (streaming + REST + files)
# =============================================================================

import json
import logging
import uuid
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from config_databricks import (
    AGENT_MAX_ITERATIONS,
    AGENT_MAX_TOKENS,
    AGENT_TEMPERATURE,
    LLM_DEFAULT_TIER,
)
from llm_provider_databricks import (
    llm_with_fallback,
    llm_stream_with_fallback,
    make_assistant_message_from_response,
    LLMResponse,
    StreamEvent,
)
from tool_registry_databricks import execute_tool, get_all_tool_definitions, get_registered_tool_names
from storage_databricks import table_insert, table_query, table_delete

logger = logging.getLogger(__name__)
router = APIRouter()

# Lakebase partition keys
_CONV_TABLE = "conversations"
_CONV_PK = "conv"
_FEEDBACK_TABLE = "feedback"
_FEEDBACK_PK = "fb"

# =============================================================================
# IN-MEMORY STORES
# =============================================================================

_conversations: dict = {}       # conv_id -> {messages: [], title: str, created: str, updated: str}
_uploaded_files: dict = {}      # upload_id -> {filename, bytes, content_type, ...}


# =============================================================================
# PERSISTENCE (Lakebase, write-through cache)
# =============================================================================

async def _persist_conversation(conv_id: str, conv: dict) -> None:
    """Write a conversation through to Lakebase (best effort)."""
    try:
        await table_insert(_CONV_TABLE, {
            "PartitionKey": _CONV_PK,
            "RowKey": conv_id,
            "messages": conv.get("messages", []),
            "title": conv.get("title", ""),
            "created": conv.get("created", ""),
            "updated": conv.get("updated", ""),
        })
    except Exception as e:
        logger.warning("[Chat] persist conversation failed: %s", e)


async def _load_conversation(conv_id: str) -> Optional[dict]:
    """Load a conversation from the in-memory cache, falling back to Lakebase."""
    if conv_id in _conversations:
        return _conversations[conv_id]
    try:
        rows = await table_query(_CONV_TABLE, partition_key=_CONV_PK, row_key=conv_id, top=1)
        if rows:
            r = rows[0]
            conv = {
                "messages": r.get("messages") or [],
                "title": r.get("title", ""),
                "created": r.get("created", ""),
                "updated": r.get("updated", ""),
            }
            _conversations[conv_id] = conv
            return conv
    except Exception as e:
        logger.warning("[Chat] load conversation failed: %s", e)
    return None


# =============================================================================
# MODELS
# =============================================================================

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    tier: Optional[str] = None
    stream: bool = False


class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    tool_calls_made: int = 0
    model: str = ""
    tier_used: str = ""
    tools_used: List[str] = []
    artifacts: List[dict] = []


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_index: int
    value: int  # 1 = positive, -1 = negative
    comment: Optional[str] = None


# =============================================================================
# ARTIFACT SURFACING
# =============================================================================
# Tools embed client-facing artifacts in their result dict (a download link or a
# Plotly chart spec). These go to the LLM as text, but the UI needs them too, so
# we extract and surface them to the client (SSE event + ChatResponse.artifacts).

def _extract_artifacts(result) -> list:
    """Pull client-facing artifacts (files, charts) out of a tool result dict."""
    if not isinstance(result, dict):
        return []
    out = []
    fd = result.get("_file_download")
    if isinstance(fd, dict) and fd.get("endpoint"):
        out.append({"kind": "file", **fd})
    for auto in result.get("_auto_file_downloads") or []:
        if isinstance(auto, dict) and auto.get("endpoint"):
            out.append({"kind": "file", **auto})
    chart = result.get("_chart")
    if isinstance(chart, dict) and chart.get("data"):
        out.append({"kind": "chart", "spec": chart, "title": result.get("title", "")})
    return out


# =============================================================================
# AGENT LOOP (non-streaming)
# =============================================================================

async def _run_agent_loop(messages: list, tier: str, conv_id: str = "", user_sub: str = "") -> tuple:
    """Returns (content, tool_calls_made, model_name, tools_used_names, artifacts)"""
    tools = get_all_tool_definitions()
    iterations = 0
    total_tool_calls = 0
    last_model = ""
    tools_used = []
    artifacts = []

    while iterations < AGENT_MAX_ITERATIONS:
        iterations += 1
        response = await llm_with_fallback(
            messages=messages, tier=tier,
            tools=tools if tools else None,
            max_tokens=AGENT_MAX_TOKENS, temperature=AGENT_TEMPERATURE,
        )
        last_model = response.model

        if not response.tool_calls:
            return response.content, total_tool_calls, last_model, tools_used, artifacts

        messages.append(make_assistant_message_from_response(response))

        for tc in response.tool_calls:
            total_tool_calls += 1
            tools_used.append(tc.name)
            logger.info("[Agent] Tool: %s", tc.name)
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
                result = await execute_tool(tc.name, args, conv_id=conv_id, user_sub=user_sub)
                artifacts.extend(_extract_artifacts(result))
                result_str = json.dumps(result, default=str, ensure_ascii=False) if not isinstance(result, str) else result
            except Exception as e:
                result_str = f"Error executing {tc.name}: {str(e)[:500]}"
                logger.error("[Agent] Tool error: %s", e, exc_info=True)

            if len(result_str) > 10000:
                result_str = result_str[:10000] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    return "Limite de iteracoes atingido.", total_tool_calls, last_model, tools_used, artifacts


# =============================================================================
# STREAMING AGENT LOOP (SSE)
# =============================================================================

async def _stream_agent_loop(messages: list, tier: str, conv_id: str = "", user_sub: str = ""):
    tools = get_all_tool_definitions()
    iterations = 0
    tools_used = []

    while iterations < AGENT_MAX_ITERATIONS:
        iterations += 1
        full_content = ""
        tool_calls = []

        async for event in llm_stream_with_fallback(
            messages=messages, tier=tier,
            tools=tools if tools else None,
            max_tokens=AGENT_MAX_TOKENS, temperature=AGENT_TEMPERATURE,
        ):
            if event.event_type == "content_delta":
                yield f"data: {json.dumps({'type':'content','delta': event.data['content']})}\n\n"
                full_content += event.data["content"]
            elif event.event_type == "stream_end":
                full_content = event.data.get("content", full_content)
                tool_calls = event.data.get("tool_calls", [])
            elif event.event_type == "error":
                yield f"data: {json.dumps({'type':'error','message': event.data.get('message','')})}\n\n"
                return

        if not tool_calls:
            # Save final response to messages list for persistence
            if full_content:
                messages.append({"role": "assistant", "content": full_content})
            yield f"data: {json.dumps({'type':'done','tools_used': tools_used})}\n\n"
            return

        assistant_msg = {"role": "assistant", "content": full_content or None}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        for tc in tool_calls:
            tools_used.append(tc["name"])
            yield f"data: {json.dumps({'type':'tool_start','name': tc['name']})}\n\n"
            artifacts = []
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                result = await execute_tool(tc["name"], args, conv_id=conv_id, user_sub=user_sub)
                artifacts = _extract_artifacts(result)
                result_str = json.dumps(result, default=str, ensure_ascii=False) if not isinstance(result, str) else result
            except Exception as e:
                result_str = f"Error: {str(e)[:500]}"

            if len(result_str) > 10000:
                result_str = result_str[:10000] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
            for art in artifacts:
                yield f"data: {json.dumps({'type':'artifact','artifact': art}, default=str)}\n\n"
            yield f"data: {json.dumps({'type':'tool_end','name': tc['name']})}\n\n"

    yield f"data: {json.dumps({'type':'done','tools_used': tools_used})}\n\n"


# =============================================================================
# ENDPOINTS: CHAT
# =============================================================================

@router.post("/chat")
async def chat(req: ChatRequest):
    conv_id = req.conversation_id or str(uuid.uuid4())
    tier = req.tier or LLM_DEFAULT_TIER

    conv = await _load_conversation(conv_id)
    if conv is None:
        conv = {
            "messages": [{"role": "system", "content": _get_system_prompt()}],
            "title": "", "created": datetime.now(timezone.utc).isoformat(),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        _conversations[conv_id] = conv

    conv["messages"].append({"role": "user", "content": req.message})
    conv["updated"] = datetime.now(timezone.utc).isoformat()

    if not conv["title"]:
        conv["title"] = req.message[:60]

    if req.stream:
        async def sse():
            async for chunk in _stream_agent_loop(conv["messages"], tier, conv_id=conv_id):
                yield chunk
            # The streaming loop appends the assistant turn in place; persist it.
            conv["updated"] = datetime.now(timezone.utc).isoformat()
            await _persist_conversation(conv_id, conv)
        return StreamingResponse(sse(), media_type="text/event-stream", headers={"X-Conversation-Id": conv_id})

    content, tool_calls_made, model, tools_used, artifacts = await _run_agent_loop(list(conv["messages"]), tier, conv_id=conv_id)
    conv["messages"].append({"role": "assistant", "content": content})
    conv["updated"] = datetime.now(timezone.utc).isoformat()
    await _persist_conversation(conv_id, conv)

    return ChatResponse(
        conversation_id=conv_id, message=content,
        tool_calls_made=tool_calls_made, model=model,
        tier_used=tier, tools_used=tools_used, artifacts=artifacts,
    )


# =============================================================================
# ENDPOINTS: CONVERSATIONS
# =============================================================================

@router.get("/conversations")
async def list_conversations():
    # Source of truth is Lakebase; fall back to the in-memory cache if unavailable.
    convs: dict = {}
    try:
        for r in await table_query(_CONV_TABLE, partition_key=_CONV_PK, top=50):
            convs[r.get("RowKey", "")] = {
                "messages": r.get("messages") or [],
                "title": r.get("title", ""),
                "created": r.get("created", ""),
                "updated": r.get("updated", ""),
            }
    except Exception as e:
        logger.warning("[Chat] list from storage failed: %s", e)
    for cid, conv in _conversations.items():
        convs.setdefault(cid, conv)

    items = []
    for cid, conv in convs.items():
        if not cid:
            continue
        user_msgs = [m for m in conv["messages"] if m.get("role") == "user"]
        items.append({
            "id": cid, "title": conv.get("title", ""),
            "message_count": len(conv["messages"]),
            "created": conv.get("created", ""),
            "updated": conv.get("updated", ""),
            "preview": user_msgs[-1]["content"][:80] if user_msgs else "",
        })
    items.sort(key=lambda x: x["updated"], reverse=True)
    return {"conversations": items[:50]}


@router.get("/conversations/{conv_id}/messages")
async def get_conversation_messages(conv_id: str):
    conv = await _load_conversation(conv_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    display_msgs = [
        {"role": m["role"], "content": m.get("content", ""), "index": i}
        for i, m in enumerate(conv["messages"])
        if m.get("role") in ("user", "assistant")
    ]
    return {"conversation_id": conv_id, "title": conv.get("title", ""), "messages": display_msgs}


@router.delete("/conversation/{conv_id}")
async def delete_conversation(conv_id: str):
    _conversations.pop(conv_id, None)
    try:
        await table_delete(_CONV_TABLE, _CONV_PK, conv_id)
    except Exception as e:
        logger.warning("[Chat] delete from storage failed: %s", e)
    return {"status": "deleted"}


# =============================================================================
# ENDPOINTS: FEEDBACK
# =============================================================================

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest):
    try:
        await table_insert(_FEEDBACK_TABLE, {
            "PartitionKey": _FEEDBACK_PK,
            "RowKey": f"{req.conversation_id}:{req.message_index}:{uuid.uuid4().hex[:8]}",
            "conversation_id": req.conversation_id,
            "message_index": req.message_index,
            "value": req.value,
            "comment": req.comment or "",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("[Chat] feedback persist failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "feedback not stored"})
    return {"status": "ok"}


# =============================================================================
# ENDPOINTS: FILE UPLOAD
# =============================================================================

@router.post("/upload")
async def upload_file(file: UploadFile = File(...), conversation_id: str = Form("")):
    if not file.filename:
        return JSONResponse(status_code=400, content={"error": "No file"})

    file_bytes = await file.read()
    upload_id = str(uuid.uuid4())

    # Tabular preview (best effort)
    preview = None
    try:
        from tabular_loader import is_tabular_filename, load_tabular_preview
        if is_tabular_filename(file.filename):
            preview = load_tabular_preview(file_bytes, file.filename)
    except Exception as e:
        logger.warning("[Upload] Preview: %s", e)

    _uploaded_files[upload_id] = {
        "filename": file.filename, "bytes": file_bytes,
        "content_type": file.content_type or "application/octet-stream",
        "conversation_id": conversation_id,
    }

    result = {"upload_id": upload_id, "filename": file.filename, "size": len(file_bytes), "content_type": file.content_type}
    if preview:
        result["rows"] = preview.get("total_rows", 0)
        result["columns"] = [c.get("name", c) if isinstance(c, dict) else str(c) for c in preview.get("columns", [])]

    # Semantic ingestion: extract -> chunk -> embed -> index (so the document
    # becomes searchable via search_uploaded_document). Best effort.
    if conversation_id:
        try:
            from upload_ingest import ingest_upload
            ingest = await ingest_upload(conversation_id, upload_id, file.filename, file_bytes)
            result["indexed"] = bool(ingest.get("indexed"))
            if ingest.get("chunks"):
                result["chunks"] = ingest["chunks"]
        except Exception as e:
            logger.warning("[Upload] ingestion failed: %s", e)
            result["indexed"] = False
    return result


# =============================================================================
# ENDPOINTS: FILE DOWNLOAD
# =============================================================================

@router.get("/download/{download_id}")
async def download_file(download_id: str):
    try:
        from generated_files import get_generated_file_sync
        entry = get_generated_file_sync(download_id)
    except ImportError:
        try:
            from generated_files import _generated_files_store
            entry = _generated_files_store.get(download_id)
        except Exception:
            entry = None

    if not entry:
        return JSONResponse(status_code=404, content={"error": "File not found or expired"})

    content = entry.get("content", b"")
    filename = entry.get("filename", "download.bin")
    mime_type = entry.get("mime_type", "application/octet-stream")

    return Response(
        content=content, media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =============================================================================
# ENDPOINTS: TOOLS & DEBUG
# =============================================================================

@router.get("/tools")
async def list_tools():
    tools = get_all_tool_definitions()
    return {"tools": [{"name": t["function"]["name"], "description": t["function"].get("description", "")} for t in tools]}


@router.get("/test-llm")
async def test_llm():
    import os as _os
    host = _os.environ.get("DATABRICKS_HOST", "NOT_SET")
    result = {"host": host, "env_keys": [k for k in _os.environ if "DATABRICK" in k.upper() or "DEVOPS" in k.upper()]}
    try:
        from llm_provider_databricks import llm_simple
        answer = await llm_simple("Diz apenas: OK", tier="fast", max_tokens=10)
        result["llm_test"] = "SUCCESS"
        result["llm_response"] = answer
    except Exception as e:
        result["llm_test"] = "FAILED"
        result["llm_error"] = str(e)[:300]
    return result


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

def _get_system_prompt() -> str:
    tools_list = get_registered_tool_names()
    return f"""Es um assistente AI especializado em engenharia de software, gestao de produto e DevOps.

Capacidades:
- Pesquisar e analisar work items no Azure DevOps (org: ptbcp, projeto: IT.DIT)
- Gerar User Stories com criterios de aceitacao (template MSE)
- Gerar apresentacoes PowerPoint, relatorios Excel e exportacoes (CSV, PDF)
- Analisar dados tabulares (CSV, Excel) com code interpreter
- Pesquisar designs no Figma e boards no Miro
- Preparar drafts de email para Outlook

Tools disponiveis: {', '.join(tools_list)}

Regras:
- Responde sempre em portugues (PT-PT)
- Se conciso e orientado a acao
- Usa as tools para obter dados reais antes de responder
- Se nao souberes, diz que nao sabes

Uso correto das tools (IMPORTANTE):
- Estrutura/hierarquia de uma Epic ou Feature ("estrutura da Epic X", "features e user stories da Epic", "filhos de", "dentro de") -> usa SEMPRE query_hierarchy (parent_id=ID). Da ligacoes parent->child reais. NAO uses query_workitems para inferir hierarquia por titulo.
- Pesquisa simples/lista de work items (por texto, estado, tipo, area) -> query_workitems.
- Exportar/gerar ficheiro para download (Excel, CSV, PDF, DOCX) -> usa SEMPRE generate_file com:
    format (ex: "xlsx"), title, data (array de objetos, uma linha por item) e columns (array com a ordem dos headers).
  NUNCA geres ficheiros para download via code_interpreter — ficheiros escritos no sandbox NAO sao descarregaveis pelo utilizador. O generate_file devolve o link de download.
- Graficos/visualizacoes -> usa SEMPRE generate_chart (chart_type + title + x_values/y_values, ou labels/values para pie). Nao desenhes graficos via code_interpreter.
- Perguntas sobre o conteudo de um documento carregado (sobretudo PDF/Word grande) -> usa search_uploaded_document.
- code_interpreter e so para CALCULAR/ANALISAR dados (pandas/numpy/duckdb), nao para produzir ficheiros de download.
- Criar work item no DevOps (User Story/Bug/Task/Feature) -> create_workitem em DOIS passos OBRIGATORIOS: (1) chama PRIMEIRO sem confirmed (confirmed=false) para obteres preview + confirmation_token; mostra o preview e pede confirmacao explicita ao utilizador; (2) SO depois de ele confirmar, chama de novo com confirmed=true e o MESMO confirmation_token. NUNCA uses confirmed=true sem o utilizador ter aprovado explicitamente na conversa.

Uploads e code_interpreter:
- Quando o utilizador faz upload de um ficheiro e queres analisar dados, usa o code_interpreter.
- Para ficheiros Excel grandes, usa SEMPRE: pd.read_excel(path, engine='openpyxl', nrows=100) para preview, ou openpyxl com read_only=True
- Os ficheiros uploaded estao disponiveis na variavel UPLOADED_FILES (lista de nomes) e no diretorio DATA_DIR
"""
