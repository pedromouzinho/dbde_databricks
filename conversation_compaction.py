"""Conversation compaction — keep long chats under the model context window.

Clean-room implementation of the "compact" pattern (concept only, no third-party
code): when the message history approaches the model's context window, the older
messages are summarized with the fast LLM into a single compact block, while the
system prompt and the most recent turns are kept verbatim. This prevents long
conversations from overflowing the context window (the "long chat -> 400" failure).

The planning logic (`plan_compaction`) is pure and unit-tested; the async wrapper
(`compact_conversation`) performs the one LLM summary call when needed.
"""
from __future__ import annotations

import logging
from typing import Any, List, Tuple

from token_counter import count_messages_tokens, resolve_context_window, RESPONSE_RESERVE_TOKENS

logger = logging.getLogger(__name__)

# Compact when the prompt would use more than this fraction of the budget.
COMPACT_THRESHOLD = 0.80
# Always keep at least this many of the most recent messages verbatim.
KEEP_RECENT = 8
# Cap per-message content fed to the summarizer (keeps the summary call bounded).
SUMMARY_INPUT_CHAR_CAP = 2000
SUMMARY_MAX_TOKENS = 1200


def _budget(model_name: str, tools_tokens: int) -> int:
    window = resolve_context_window(model_name)
    return max(8000, window - RESPONSE_RESERVE_TOKENS - max(0, int(tools_tokens or 0)))


def _safe_split_index(messages: List[dict], keep_recent: int, system_offset: int) -> int:
    """Index ``s`` such that messages[system_offset:s] get summarized and
    messages[s:] are kept verbatim.

    Guarantees messages[s] is not an orphan ``tool`` message — its parent
    ``assistant`` (with the matching tool_calls) would have been summarized away,
    and a leading tool message with no parent makes the chat API reject the call.
    """
    n = len(messages)
    s = max(system_offset, n - keep_recent)
    while s < n and messages[s].get("role") == "tool":
        s += 1  # fold orphan tool messages into the summarized segment
    return s


def plan_compaction(messages: List[dict], budget: int, keep_recent: int = KEEP_RECENT) -> dict:
    """Pure planning: decide whether/how to compact. No LLM call.

    Returns a dict with at least ``compacted`` (bool). When True it also includes
    ``system_offset``, ``split`` and ``summarized_count``.
    """
    used = count_messages_tokens(messages)
    if used <= int(budget * COMPACT_THRESHOLD):
        return {"compacted": False, "used": used, "budget": budget}
    has_system = bool(messages) and messages[0].get("role") == "system"
    system_offset = 1 if has_system else 0
    split = _safe_split_index(messages, keep_recent, system_offset)
    if split <= system_offset:
        # everything is either system or "recent" — nothing older to summarize
        return {"compacted": False, "used": used, "budget": budget, "reason": "nothing_old"}
    return {
        "compacted": True,
        "used": used,
        "budget": budget,
        "system_offset": system_offset,
        "split": split,
        "summarized_count": split - system_offset,
    }


def _render_for_summary(messages: List[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):  # multimodal -> keep text blocks only
            content = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
        content = str(content or "")[:SUMMARY_INPUT_CHAR_CAP]
        tool_calls = m.get("tool_calls") or []
        if tool_calls:
            names = ", ".join(
                str(tc.get("function", {}).get("name", ""))
                for tc in tool_calls if isinstance(tc, dict)
            )
            content = (content + f" [tool_calls: {names}]").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


_SUMMARY_PROMPT = (
    "Resume a conversa anterior entre o utilizador e o assistente, em PT-PT e de "
    "forma concisa. PRESERVA obrigatoriamente: pedidos e decisões do utilizador, "
    "IDs/work items e Epics mencionados, ficheiros/artefactos gerados, e qualquer "
    "contexto necessário para continuar sem perder informação. Não inventes. "
    "Devolve apenas o resumo.\n\n--- CONVERSA ---\n{body}"
)


async def compact_conversation(
    messages: List[dict], *, model_name: str, tools_tokens: int = 0,
    keep_recent: int = KEEP_RECENT, summary_tier: str = "fast",
) -> Tuple[List[dict], dict]:
    """Return (possibly-compacted messages, meta).

    Calls the fast LLM only when the history is over threshold; otherwise returns
    the original list untouched. On any summary failure it degrades gracefully by
    keeping the full history (never raises).
    """
    budget = _budget(model_name, tools_tokens)
    plan = plan_compaction(messages, budget, keep_recent=keep_recent)
    if not plan.get("compacted"):
        return messages, plan

    system_offset = plan["system_offset"]
    split = plan["split"]
    body = _render_for_summary(messages[system_offset:split])
    try:
        from llm_provider_databricks import llm_simple
        summary = await llm_simple(
            _SUMMARY_PROMPT.format(body=body),
            tier=summary_tier, max_tokens=SUMMARY_MAX_TOKENS, temperature=0.2,
        )
    except Exception as e:
        logger.warning("[Compact] summary failed, keeping full history: %s", e)
        return messages, {"compacted": False, "error": str(e)[:200], "used": plan["used"]}

    summary = (summary or "").strip()
    if not summary:
        return messages, {"compacted": False, "reason": "empty_summary", "used": plan["used"]}

    summary_msg = {"role": "user", "content": f"[Resumo da conversa anterior]\n{summary}"}
    new_messages = list(messages[:system_offset]) + [summary_msg] + list(messages[split:])
    after = count_messages_tokens(new_messages)
    logger.info("[Compact] %d -> %d tokens (summarized %d msgs)",
                plan["used"], after, plan["summarized_count"])
    return new_messages, {
        "compacted": True, "before": plan["used"], "after": after,
        "summarized_count": plan["summarized_count"], "budget": budget,
    }
