"""Sub-agent delegation — run a heavy/complex sub-task in an isolated context.

A bounded, self-contained agent loop the main assistant delegates to via the
``delegate_task`` tool. The sub-agent gets a focused system prompt, a fresh
message list (it does NOT see the parent conversation, which saves tokens and
isolates context), and the full tool set EXCEPT ``delegate_task`` (anti-recursion).
Any file/chart artifacts produced by its tools are surfaced back so the parent
can show them in the UI.

Mirrors routes_chat_databricks._run_agent_loop but standalone (no FastAPI/streaming
concerns) and with tighter limits.
"""
from __future__ import annotations

import json
import logging

from llm_provider_databricks import llm_with_fallback, make_assistant_message_from_response
from tool_registry_databricks import execute_tool, get_all_tool_definitions
from config_databricks import AGENT_MAX_TOKENS, AGENT_TEMPERATURE

logger = logging.getLogger(__name__)

_SUBAGENT_SYSTEM = (
    "Es um sub-agente especializado que executa UMA tarefa delegada pelo assistente "
    "principal. Usa as tools disponiveis para a completar e devolve um RESULTADO FINAL "
    "claro e objetivo em PT-PT. Nao peças mais input ao utilizador (não há interação). "
    "Se gerares ficheiros ou graficos, refere-os no resultado. "
    "Não tens acesso ao histórico da conversa principal — usa apenas a tarefa e o contexto dados."
)

_MAX_INPUT_CHARS = 6000
_MAX_RESULT_CHARS = 8000
_TOOL_RESULT_CAP = 10000


def _subagent_tools() -> list:
    """All tool definitions except delegate_task (prevents recursion)."""
    return [
        d for d in get_all_tool_definitions()
        if d.get("function", {}).get("name") != "delegate_task"
    ]


async def run_subagent(
    task: str, context: str = "", *, conv_id: str = "", user_sub: str = "",
    tier: str = "standard", max_iterations: int = 6, depth: int = 0,
) -> dict:
    """Run a delegated task in an isolated, bounded agent loop.

    Returns {"result": text, "tools_used": [...], "iterations": n} and, when the
    sub-agent's tools produced them, "_auto_file_downloads" / "_chart" so the
    parent loop's artifact extraction surfaces them to the client.
    """
    task = str(task or "").strip()
    if not task:
        return {"error": "task vazio."}
    if depth >= 1:
        # Only one level of delegation — a sub-agent cannot itself delegate.
        return {"error": "Delegação aninhada não permitida."}

    context = str(context or "")[:_MAX_INPUT_CHARS]
    user_msg = f"TAREFA:\n{task[:_MAX_INPUT_CHARS]}"
    if context:
        user_msg += f"\n\nCONTEXTO:\n{context}"
    messages = [
        {"role": "system", "content": _SUBAGENT_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    tools = _subagent_tools()

    tools_used: list = []
    collected_files: list = []
    collected_chart = None
    last_text = ""
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        try:
            resp = await llm_with_fallback(
                messages=messages, tier=tier,
                tools=tools if tools else None,
                max_tokens=AGENT_MAX_TOKENS, temperature=AGENT_TEMPERATURE,
            )
        except Exception as e:
            logger.warning("[Subagent] LLM call failed: %s", e)
            return {"error": f"Falha do sub-agente: {str(e)[:200]}",
                    "tools_used": tools_used, "iterations": iterations}

        if resp.content:
            last_text = resp.content
        if not resp.tool_calls:
            break

        messages.append(make_assistant_message_from_response(resp))
        for tc in resp.tool_calls:
            tools_used.append(tc.name)
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
            except Exception:
                args = {}
            result = await execute_tool(tc.name, args, conv_id=conv_id, user_sub=user_sub)
            # Collect artifacts to surface up to the parent (so the UI shows them).
            if isinstance(result, dict):
                fd = result.get("_file_download")
                if isinstance(fd, dict) and fd.get("endpoint"):
                    collected_files.append(fd)
                for auto in result.get("_auto_file_downloads") or []:
                    if isinstance(auto, dict) and auto.get("endpoint"):
                        collected_files.append(auto)
                if collected_chart is None and isinstance(result.get("_chart"), dict):
                    collected_chart = result["_chart"]
            result_str = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
            if len(result_str) > _TOOL_RESULT_CAP:
                result_str = result_str[:_TOOL_RESULT_CAP] + "\n...[truncated]"
            # One tool result per tool_call_id (keeps the message history valid).
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    out = {
        "result": (last_text or "(o sub-agente terminou sem resultado textual)")[:_MAX_RESULT_CHARS],
        "tools_used": tools_used,
        "iterations": iterations,
    }
    if collected_files:
        out["_auto_file_downloads"] = collected_files
    if collected_chart is not None:
        out["_chart"] = collected_chart
    return out
