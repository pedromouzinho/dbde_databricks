"""Repair chat histories so the OpenAI-compatible endpoint never 400s.

The chat API has two hard rules around tool calls:
  - every assistant message with ``tool_calls`` must be answered by exactly one
    ``tool`` message per ``tool_call_id`` (a missing answer is rejected);
  - a ``tool`` message with no preceding assistant ``tool_calls`` that declared
    its id is an orphan and is rejected.

A partial/errored stream (or a crash mid tool-loop) can persist a broken state,
and reloading it would 400 on the next request. ``sanitize_messages`` repairs any
history into a valid shape: it drops orphan tool messages and appends a synthetic
``[no result]`` answer for any declared tool_call that was never answered.

Pure and idempotent: ``sanitize_messages(sanitize_messages(x)) == sanitize_messages(x)``.
"""
from __future__ import annotations

from typing import Any, List


def _tool_call_ids(assistant_msg: dict) -> List[str]:
    ids = []
    for tc in assistant_msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            tid = tc.get("id")
            if tid:
                ids.append(tid)
    return ids


def sanitize_messages(messages: Any) -> Any:
    """Return a new, API-valid message list (never raises; passes through non-lists)."""
    if not isinstance(messages, list):
        return messages

    out: List[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1  # drop malformed entries
            continue
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            out.append(msg)
            ids = _tool_call_ids(msg)
            # Gather the contiguous run of tool messages that answers this turn.
            j = i + 1
            answers: dict = {}
            while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid is not None and tid not in answers:
                    answers[tid] = messages[j]
                j += 1
            # Emit one answer per declared id, in order; synthesize the missing ones.
            # Tool messages in the run whose id was not declared are dropped (orphans).
            for tid in ids:
                if tid in answers:
                    out.append(answers[tid])
                else:
                    out.append({"role": "tool", "tool_call_id": tid, "content": "[no result]"})
            i = j  # skip the consumed run
        elif role == "tool":
            i += 1  # standalone tool message with no parent -> orphan, drop
        else:
            out.append(msg)
            i += 1

    return out
