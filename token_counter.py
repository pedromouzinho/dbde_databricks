"""Token counting utilities for DBDE AI Assistant."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_encoder = None

MODEL_CONTEXT_WINDOWS = {
    "gpt-4.1-mini": 128_000,
    "gpt-4.1": 128_000,
    "gpt-5.1": 200_000,
    "gpt-5": 200_000,
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
}
RESPONSE_RESERVE_TOKENS = 4096


def _get_encoder():
    global _encoder
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            logger.warning("[TokenCounter] tiktoken not available, falling back to char estimate")
            _encoder = "FALLBACK"
    return _encoder


def count_tokens(text: str) -> int:
    """Count tokens in text. Falls back to len(text)//4 if tiktoken unavailable."""
    if not text:
        return 0
    enc = _get_encoder()
    if enc == "FALLBACK":
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Count total tokens across a list of messages (OpenAI-like format)."""
    total = 0
    for msg in messages or []:
        total += count_single_message_tokens(msg)
    return total


def count_single_message_tokens(msg: dict[str, Any]) -> int:
    """Count tokens for a single message dict."""
    total = 4
    content = msg.get("content", "")
    if isinstance(content, str):
        total += count_tokens(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            txt = block.get("text", "")
            if txt:
                total += count_tokens(str(txt))
            if block.get("type") == "image_url":
                total += 765

    tool_calls = msg.get("tool_calls", [])
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if isinstance(fn, dict):
                total += count_tokens(str(fn.get("name", "")))
                total += count_tokens(str(fn.get("arguments", "")))
    return total


def count_tools_tokens(tools: list[dict[str, Any]]) -> int:
    """Count tokens in tool definitions (schema overhead)."""
    try:
        text = json.dumps(tools or [], ensure_ascii=False)
    except Exception:
        text = "[]"
    return count_tokens(text)


def resolve_context_window(model_name: str) -> int:
    """Resolve context window from model/deployment name."""
    normalized = str(model_name or "").strip().lower()
    for key, size in MODEL_CONTEXT_WINDOWS.items():
        if key in normalized:
            return size
    return 128_000
