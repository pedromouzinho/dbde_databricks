"""Sub-agent delegation — run heavy/complex sub-tasks in isolated contexts.

A bounded, self-contained agent loop the main assistant delegates to via the
``delegate_task`` tool. Each sub-agent gets a focused system prompt, a fresh
message list (it does NOT see the parent conversation — saves tokens, isolates
context), and a tool subset chosen by its TYPE. Several sub-tasks can run in
PARALLEL. Any file/chart artifacts produced are surfaced back so the parent can
show them in the UI.

Safety: sub-agents never get ``delegate_task`` (anti-recursion) nor
``create_workitem`` (writes need human two-step confirmation, which a headless
sub-agent cannot provide). A depth guard also refuses nested delegation.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List

from llm_provider_databricks import llm_with_fallback, make_assistant_message_from_response
from tool_registry_databricks import execute_tool, get_all_tool_definitions
from config_databricks import AGENT_MAX_TOKENS, AGENT_TEMPERATURE

logger = logging.getLogger(__name__)

# Tools a sub-agent must never get, regardless of type.
_EXCLUDED_FROM_SUBAGENTS = {"delegate_task", "create_workitem"}

_BASE_SYSTEM = (
    "Es um sub-agente que executa UMA tarefa delegada pelo assistente principal. "
    "Usa as tools disponiveis para a completar e devolve um RESULTADO FINAL claro e "
    "objetivo em PT-PT. Nao peças input ao utilizador (nao ha interacao). Se gerares "
    "ficheiros ou graficos, menciona-os. Nao tens acesso ao historico da conversa "
    "principal — usa apenas a tarefa e o contexto dados."
)

# Specialized types: each restricts the tool set and adds a role focus. ``tools=None``
# means "all tools" (minus the always-excluded ones). Unknown names in a set are
# simply ignored if that tool isn't registered (e.g. Figma/Miro disabled).
_AGENT_PROFILES: dict = {
    "general": {
        "tools": None,
        "focus": "Resolve a tarefa de forma geral, escolhendo as tools adequadas.",
    },
    "data_analyst": {
        "tools": {"code_interpreter", "search_uploaded_document", "generate_chart",
                  "generate_file", "compute_kpi", "query_workitems"},
        "focus": "Es analista de dados. Analisa ficheiros/dados, calcula metricas e gera "
                 "graficos/ficheiros quando util.",
    },
    "story_writer": {
        "tools": {"query_workitems", "query_hierarchy", "search_workitems",
                  "generate_user_stories", "get_writer_profile", "analyze_patterns"},
        "focus": "Es PO senior. Escreve user stories no template MSE, fundamentadas em "
                 "itens reais do DevOps. Nao crias work items (apenas propostas).",
    },
    "researcher": {
        "tools": {"query_workitems", "query_hierarchy", "search_workitems", "search_knowledge",
                  "search_uploaded_document", "compute_kpi", "analyze_patterns",
                  "search_figma", "search_miro"},
        "focus": "Es investigador (read-only). Recolhe e sintetiza informacao; nao escrevas "
                 "nem geres ficheiros.",
    },
    "presenter": {
        "tools": {"generate_presentation", "generate_file", "generate_chart",
                  "query_workitems", "query_hierarchy", "search_workitems"},
        "focus": "Es responsavel por entregaveis. Constroi apresentacoes/ficheiros claros a "
                 "partir do contexto dado.",
    },
}

AGENT_TYPES = list(_AGENT_PROFILES.keys())

_MAX_INPUT_CHARS = 6000
_MAX_RESULT_CHARS = 8000
_TOOL_RESULT_CAP = 10000
_MAX_PARALLEL_TASKS = 6
_DEFAULT_CONCURRENCY = 4


def _profile(agent_type: str) -> dict:
    return _AGENT_PROFILES.get(str(agent_type or "").strip(), _AGENT_PROFILES["general"])


def _subagent_tools(agent_type: str = "general") -> list:
    """Tool definitions for this agent type (always minus the excluded tools)."""
    allow = _profile(agent_type)["tools"]
    out = []
    for d in get_all_tool_definitions():
        name = d.get("function", {}).get("name")
        if name in _EXCLUDED_FROM_SUBAGENTS:
            continue
        if allow is not None and name not in allow:
            continue
        out.append(d)
    return out


def _collect_artifacts(result: Any, files: list, chart_holder: list) -> None:
    if not isinstance(result, dict):
        return
    fd = result.get("_file_download")
    if isinstance(fd, dict) and fd.get("endpoint"):
        files.append(fd)
    for auto in result.get("_auto_file_downloads") or []:
        if isinstance(auto, dict) and auto.get("endpoint"):
            files.append(auto)
    if not chart_holder and isinstance(result.get("_chart"), dict):
        chart_holder.append(result["_chart"])


async def run_subagent(
    task: str, context: str = "", *, agent_type: str = "general",
    conv_id: str = "", user_sub: str = "", tier: str = "standard",
    max_iterations: int = 6, depth: int = 0,
) -> dict:
    """Run a single delegated task in an isolated, bounded agent loop."""
    task = str(task or "").strip()
    if not task:
        return {"error": "task vazio."}
    if depth >= 1:
        return {"error": "Delegação aninhada não permitida."}

    agent_type = agent_type if agent_type in _AGENT_PROFILES else "general"
    prof = _profile(agent_type)
    context = str(context or "")[:_MAX_INPUT_CHARS]
    user_msg = f"TAREFA:\n{task[:_MAX_INPUT_CHARS]}"
    if context:
        user_msg += f"\n\nCONTEXTO:\n{context}"
    messages = [
        {"role": "system", "content": f"{_BASE_SYSTEM}\nPapel: {prof['focus']}"},
        {"role": "user", "content": user_msg},
    ]
    tools = _subagent_tools(agent_type)

    tools_used: list = []
    files: list = []
    chart_holder: list = []
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
            logger.warning("[Subagent:%s] LLM call failed: %s", agent_type, e)
            return {"error": f"Falha do sub-agente: {str(e)[:200]}",
                    "agent_type": agent_type, "tools_used": tools_used, "iterations": iterations}

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
            _collect_artifacts(result, files, chart_holder)
            result_str = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
            if len(result_str) > _TOOL_RESULT_CAP:
                result_str = result_str[:_TOOL_RESULT_CAP] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    out = {
        "result": (last_text or "(o sub-agente terminou sem resultado textual)")[:_MAX_RESULT_CHARS],
        "agent_type": agent_type,
        "tools_used": tools_used,
        "iterations": iterations,
    }
    if files:
        out["_auto_file_downloads"] = files
    if chart_holder:
        out["_chart"] = chart_holder[0]
    return out


async def run_subagents_parallel(
    tasks: List[dict], *, conv_id: str = "", user_sub: str = "", tier: str = "standard",
    max_concurrency: int = _DEFAULT_CONCURRENCY, depth: int = 0,
) -> dict:
    """Run several delegated sub-tasks concurrently (bounded). Each item is a dict
    {task, context?, agent_type?}. Returns one entry per task plus aggregated artifacts."""
    if not isinstance(tasks, list) or not tasks:
        return {"error": "tasks vazio."}
    items = [t for t in tasks if isinstance(t, dict) and str(t.get("task", "")).strip()][:_MAX_PARALLEL_TASKS]
    if not items:
        return {"error": "Nenhuma sub-tarefa válida em tasks."}

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _one(item: dict) -> dict:
        async with sem:
            return await run_subagent(
                item.get("task", ""), item.get("context", ""),
                agent_type=item.get("agent_type", "general"),
                conv_id=conv_id, user_sub=user_sub, tier=tier, depth=depth,
            )

    raw = await asyncio.gather(*[_one(it) for it in items], return_exceptions=True)

    results = []
    files: list = []
    chart_holder: list = []
    for r in raw:
        if isinstance(r, Exception):
            results.append({"error": str(r)[:200]})
            continue
        results.append(r)
        _collect_artifacts(r, files, chart_holder)

    out = {"results": results, "count": len(results)}
    if files:
        out["_auto_file_downloads"] = files
    if chart_holder:
        out["_chart"] = chart_holder[0]
    return out
