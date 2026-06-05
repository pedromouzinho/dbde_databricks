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

logger = logging.getLogger(__name__)
router = APIRouter()

# =============================================================================
# IN-MEMORY STORES
# =============================================================================

_conversations: dict = {}       # conv_id -> {messages: [], title: str, created: str, updated: str}
_uploaded_files: dict = {}      # upload_id -> {filename, bytes, content_type, ...}
_feedback: list = []            # [{conv_id, msg_idx, value, ts}]


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


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_index: int
    value: int  # 1 = positive, -1 = negative
    comment: Optional[str] = None


# =============================================================================
# AGENT LOOP (non-streaming)
# =============================================================================

async def _run_agent_loop(messages: list, tier: str) -> tuple:
    """Returns (content, tool_calls_made, model_name, tools_used_names)"""
    tools = get_all_tool_definitions()
    iterations = 0
    total_tool_calls = 0
    last_model = ""
    tools_used = []

    while iterations < AGENT_MAX_ITERATIONS:
        iterations += 1
        response = await llm_with_fallback(
            messages=messages, tier=tier,
            tools=tools if tools else None,
            max_tokens=AGENT_MAX_TOKENS, temperature=AGENT_TEMPERATURE,
        )
        last_model = response.model

        if not response.tool_calls:
            return response.content, total_tool_calls, last_model, tools_used

        messages.append(make_assistant_message_from_response(response))

        for tc in response.tool_calls:
            total_tool_calls += 1
            tools_used.append(tc.name)
            logger.info("[Agent] Tool: %s", tc.name)
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
                result = await execute_tool(tc.name, args)
                result_str = json.dumps(result, default=str, ensure_ascii=False) if not isinstance(result, str) else result
            except Exception as e:
                result_str = f"Error executing {tc.name}: {str(e)[:500]}"
                logger.error("[Agent] Tool error: %s", e, exc_info=True)

            if len(result_str) > 10000:
                result_str = result_str[:10000] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    return "Limite de iteracoes atingido.", total_tool_calls, last_model, tools_used


# =============================================================================
# STREAMING AGENT LOOP (SSE)
# =============================================================================

async def _stream_agent_loop(messages: list, tier: str):
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
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                result = await execute_tool(tc["name"], args)
                result_str = json.dumps(result, default=str, ensure_ascii=False) if not isinstance(result, str) else result
            except Exception as e:
                result_str = f"Error: {str(e)[:500]}"

            if len(result_str) > 10000:
                result_str = result_str[:10000] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
            yield f"data: {json.dumps({'type':'tool_end','name': tc['name']})}\n\n"

    yield f"data: {json.dumps({'type':'done','tools_used': tools_used})}\n\n"


# =============================================================================
# ENDPOINTS: CHAT
# =============================================================================

@router.post("/chat")
async def chat(req: ChatRequest):
    conv_id = req.conversation_id or str(uuid.uuid4())
    tier = req.tier or LLM_DEFAULT_TIER

    if conv_id not in _conversations:
        _conversations[conv_id] = {
            "messages": [{"role": "system", "content": _get_system_prompt()}],
            "title": "", "created": datetime.now(timezone.utc).isoformat(),
            "updated": datetime.now(timezone.utc).isoformat(),
        }

    conv = _conversations[conv_id]
    conv["messages"].append({"role": "user", "content": req.message})
    conv["updated"] = datetime.now(timezone.utc).isoformat()

    if not conv["title"]:
        conv["title"] = req.message[:60]

    if req.stream:
        async def sse():
            async for chunk in _stream_agent_loop(conv["messages"], tier):
                yield chunk
        return StreamingResponse(sse(), media_type="text/event-stream", headers={"X-Conversation-Id": conv_id})

    content, tool_calls_made, model, tools_used = await _run_agent_loop(list(conv["messages"]), tier)
    conv["messages"].append({"role": "assistant", "content": content})

    return ChatResponse(
        conversation_id=conv_id, message=content,
        tool_calls_made=tool_calls_made, model=model,
        tier_used=tier, tools_used=tools_used,
    )


# =============================================================================
# ENDPOINTS: CONVERSATIONS
# =============================================================================

@router.get("/conversations")
async def list_conversations():
    items = []
    for cid, conv in _conversations.items():
        user_msgs = [m for m in conv["messages"] if m["role"] == "user"]
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
    conv = _conversations.get(conv_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    display_msgs = [
        {"role": m["role"], "content": m.get("content", ""), "index": i}
        for i, m in enumerate(conv["messages"])
        if m["role"] in ("user", "assistant")
    ]
    return {"conversation_id": conv_id, "title": conv.get("title", ""), "messages": display_msgs}


@router.delete("/conversation/{conv_id}")
async def delete_conversation(conv_id: str):
    _conversations.pop(conv_id, None)
    return {"status": "deleted"}


# =============================================================================
# ENDPOINTS: FEEDBACK
# =============================================================================

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest):
    _feedback.append({
        "conversation_id": req.conversation_id,
        "message_index": req.message_index,
        "value": req.value,
        "comment": req.comment or "",
        "ts": datetime.now(timezone.utc).isoformat(),
    })
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
- Quando o utilizador faz upload de um ficheiro, usa o code_interpreter para o analisar
- Para ficheiros Excel grandes, usa SEMPRE: pd.read_excel(path, engine='openpyxl', nrows=100) para preview, ou openpyxl com read_only=True
- Os ficheiros uploaded estao disponiveis na variavel UPLOADED_FILES (lista de nomes) e no diretorio DATA_DIR
- Se nao souberes, diz que nao sabes
"""
