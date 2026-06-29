# =============================================================================
# llm_provider_databricks.py — LLM Provider via Databricks Foundation Model APIs
# =============================================================================
# Drop-in replacement for llm_provider.py (Azure OpenAI + Anthropic direct).
# Uses OpenAI SDK pointing at Databricks serving endpoints.
# Supports: chat completions, streaming, tool calling, embeddings.
# =============================================================================

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Any, List, Optional

from openai import AsyncOpenAI, OpenAI

from config_databricks import (
    DATABRICKS_HOST,
    DATABRICKS_TOKEN,
    TIER_TO_ENDPOINT,
    FALLBACK_CHAIN,
    EMBEDDING_ENDPOINT,
    EMBEDDING_VECTOR_DIMENSIONS,
    LLM_DEFAULT_TIER,
    LLM_TIER_FAST,
    LLM_TIER_STANDARD,
    LLM_TIER_PRO,
    LLM_TIER_VISION,
    LLM_FALLBACK,
    AGENT_MAX_TOKENS,
    AGENT_TEMPERATURE,
    DEBUG_LOG_SIZE,
)

logger = logging.getLogger(__name__)
_llm_debug_log: deque = deque(maxlen=DEBUG_LOG_SIZE)


def get_debug_log() -> list:
    return list(_llm_debug_log)


def _log(msg: str):
    _llm_debug_log.append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
    logger.info("[LLM] %s", msg)


# =============================================================================
# CLIENT INITIALIZATION
# =============================================================================

import time
import httpx as _httpx_sync

_oauth_token_cache: Dict[str, Any] = {"token": "", "expires_at": 0}


def _get_token() -> str:
    """Get Databricks token. Uses OAuth client credentials in Apps runtime."""
    # 1. If DATABRICKS_TOKEN is explicitly set, use it
    token = DATABRICKS_TOKEN or os.environ.get("DATABRICKS_TOKEN", "")
    if token:
        return token

    # 2. Use OAuth M2M (client_id + client_secret) — standard in Databricks Apps
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.error("[LLM] No DATABRICKS_TOKEN and no CLIENT_ID/SECRET available")
        return ""

    # Check cache
    if _oauth_token_cache["token"] and time.time() < _oauth_token_cache["expires_at"] - 60:
        return _oauth_token_cache["token"]

    # Request new token via OAuth2 client credentials
    host = os.environ.get("DATABRICKS_HOST", DATABRICKS_HOST)
    if not host.startswith("http"):
        host = f"https://{host}"
    token_url = f"{host}/oidc/v1/token"
    try:
        resp = _httpx_sync.post(
            token_url,
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _oauth_token_cache["token"] = data["access_token"]
        _oauth_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
        logger.info("[LLM] OAuth token acquired (expires in %ds)", data.get("expires_in", 3600))
        return data["access_token"]
    except Exception as e:
        logger.error("[LLM] OAuth token request failed: %s", e)
        return ""


def _get_base_url() -> str:
    host = os.environ.get("DATABRICKS_HOST", DATABRICKS_HOST)
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/serving-endpoints"


_async_client: Optional[AsyncOpenAI] = None
_sync_client: Optional[OpenAI] = None


def get_async_client() -> AsyncOpenAI:
    global _async_client
    token = _get_token()
    if _async_client is None or getattr(_async_client, '_last_token', '') != token:
        _async_client = AsyncOpenAI(
            api_key=token,
            base_url=_get_base_url(),
        )
        _async_client._last_token = token  # type: ignore
    return _async_client


def get_sync_client() -> OpenAI:
    global _sync_client
    token = _get_token()
    if _sync_client is None or getattr(_sync_client, '_last_token', '') != token:
        _sync_client = OpenAI(
            api_key=token,
            base_url=_get_base_url(),
        )
        _sync_client._last_token = token  # type: ignore
    return _sync_client


# =============================================================================
# DATA CLASSES (compatible with original models.py)
# =============================================================================

class LLMResponse:
    """Normalized LLM response (same interface as original)."""
    def __init__(self, content: str = "", tool_calls: list = None,
                 usage: dict = None, model: str = "", finish_reason: str = ""):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.model = model
        self.finish_reason = finish_reason


class LLMToolCall:
    """Normalized tool call."""
    def __init__(self, id: str, name: str, arguments: str):
        self.id = id
        self.name = name
        self.arguments = arguments

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


class StreamEvent:
    """SSE stream event."""
    def __init__(self, event_type: str, data: Any = None):
        self.event_type = event_type
        self.data = data


# =============================================================================
# PROVIDER RESOLUTION
# =============================================================================

# Keys the OpenAI-compatible chat endpoint accepts on a message. Anything else
# (e.g. our internal `_images` display refs carried on a user message) must be
# stripped before the request, or the API may 400 on unknown fields.
_ALLOWED_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def _clean_messages(messages: List[dict]) -> List[dict]:
    """Return a copy of messages with only API-valid keys per message.

    Preserves multimodal list content (text + image_url blocks) untouched; only
    drops non-standard top-level keys such as ``_images``.
    """
    cleaned: List[dict] = []
    for m in messages or []:
        if isinstance(m, dict):
            cleaned.append({k: v for k, v in m.items() if k in _ALLOWED_MESSAGE_KEYS})
        else:
            cleaned.append(m)
    return cleaned


def get_provider(tier: str = None) -> str:
    """Returns the endpoint name for a given tier."""
    tier = tier or LLM_DEFAULT_TIER
    return TIER_TO_ENDPOINT.get(tier, TIER_TO_ENDPOINT[LLM_TIER_STANDARD])


def get_embedding_provider() -> str:
    """Returns the embedding endpoint name."""
    return EMBEDDING_ENDPOINT


# =============================================================================
# CHAT COMPLETION (request/response)
# =============================================================================

async def llm_with_fallback(
    messages: List[dict],
    tier: str = None,
    tools: List[dict] = None,
    max_tokens: int = None,
    temperature: float = None,
    **kwargs,
) -> LLMResponse:
    """
    Call LLM with automatic fallback through the chain.
    Compatible with original llm_with_fallback() interface.
    """
    tier = tier or LLM_DEFAULT_TIER
    max_tokens = max_tokens or AGENT_MAX_TOKENS
    temperature = temperature if temperature is not None else AGENT_TEMPERATURE

    chain = FALLBACK_CHAIN.get(tier, [TIER_TO_ENDPOINT[LLM_TIER_STANDARD]])
    if not LLM_FALLBACK:
        chain = chain[:1]

    messages = _clean_messages(messages)
    client = get_async_client()
    last_error = None

    for endpoint in chain:
        try:
            _log(f"Calling {endpoint} (tier={tier}, tokens={max_tokens})")

            call_kwargs = {
                "model": endpoint,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if "opus" not in endpoint.lower():
                call_kwargs["temperature"] = temperature
            if tools:
                call_kwargs["tools"] = tools

            response = await client.chat.completions.create(**call_kwargs)
            choice = response.choices[0]

            # Parse tool calls
            parsed_tools = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    parsed_tools.append(LLMToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    ))

            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            _log(f"Success from {endpoint}: {usage.get('total_tokens', '?')} tokens")

            return LLMResponse(
                content=choice.message.content or "",
                tool_calls=parsed_tools,
                usage=usage,
                model=response.model or endpoint,
                finish_reason=choice.finish_reason or "",
            )

        except Exception as e:
            last_error = e
            _log(f"Error from {endpoint}: {e}. Trying next in chain...")
            continue

    raise RuntimeError(f"All endpoints failed. Last error: {last_error}")


# =============================================================================
# STREAMING CHAT COMPLETION (SSE)
# =============================================================================

async def llm_stream_with_fallback(
    messages: List[dict],
    tier: str = None,
    tools: List[dict] = None,
    max_tokens: int = None,
    temperature: float = None,
    **kwargs,
) -> AsyncGenerator[StreamEvent, None]:
    """
    Stream LLM response as SSE events with fallback.
    Compatible with original llm_stream_with_fallback() interface.
    """
    tier = tier or LLM_DEFAULT_TIER
    max_tokens = max_tokens or AGENT_MAX_TOKENS
    temperature = temperature if temperature is not None else AGENT_TEMPERATURE

    chain = FALLBACK_CHAIN.get(tier, [TIER_TO_ENDPOINT[LLM_TIER_STANDARD]])
    if not LLM_FALLBACK:
        chain = chain[:1]

    messages = _clean_messages(messages)
    client = get_async_client()

    for endpoint in chain:
        try:
            _log(f"Streaming from {endpoint} (tier={tier})")

            call_kwargs = {
                "model": endpoint,
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if "opus" not in endpoint.lower():
                call_kwargs["temperature"] = temperature
            if tools:
                call_kwargs["tools"] = tools

            stream = await client.chat.completions.create(**call_kwargs)

            # Yield start event
            yield StreamEvent("stream_start", {"model": endpoint})

            accumulated_content = ""
            accumulated_tool_calls: Dict[int, dict] = {}

            async for chunk in stream:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # Content delta
                if delta.content:
                    accumulated_content += delta.content
                    yield StreamEvent("content_delta", {"content": delta.content})

                # Tool call deltas
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                "arguments": "",
                            }
                        if tc_delta.id:
                            accumulated_tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                accumulated_tool_calls[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                accumulated_tool_calls[idx]["arguments"] += tc_delta.function.arguments

                # Finish
                if chunk.choices[0].finish_reason:
                    tool_calls = []
                    for _, tc_data in sorted(accumulated_tool_calls.items()):
                        tool_calls.append(LLMToolCall(
                            id=tc_data["id"],
                            name=tc_data["name"],
                            arguments=tc_data["arguments"],
                        ))

                    yield StreamEvent("stream_end", {
                        "content": accumulated_content,
                        "tool_calls": [tc.to_dict() for tc in tool_calls],
                        "finish_reason": chunk.choices[0].finish_reason,
                        "model": endpoint,
                    })
                    return

            return  # Stream completed

        except Exception as e:
            _log(f"Stream error from {endpoint}: {e}. Trying next...")
            continue

    yield StreamEvent("error", {"message": "All endpoints failed"})


# =============================================================================
# SIMPLE CALL (for internal use, like tools that need LLM)
# =============================================================================

async def llm_simple(
    prompt: str,
    tier: str = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
) -> str:
    """
    Simple LLM call returning just the text content.
    Used by tools internally (e.g., tools_devops for story generation).
    """
    response = await llm_with_fallback(
        messages=[{"role": "user", "content": prompt}],
        tier=tier or LLM_TIER_FAST,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.content


# =============================================================================
# EMBEDDINGS
# =============================================================================

async def get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Generate embeddings using Databricks GTE Large endpoint.
    Returns list of vectors (1024 dimensions each).
    """
    client = get_async_client()
    response = await client.embeddings.create(
        model=EMBEDDING_ENDPOINT,
        input=texts,
    )
    return [item.embedding for item in response.data]


async def get_embedding(text: str) -> List[float]:
    """Get single text embedding."""
    results = await get_embeddings([text])
    return results[0] if results else []


# =============================================================================
# HELPER: format assistant message from response (compat)
# =============================================================================

def make_assistant_message_from_response(response: LLMResponse) -> dict:
    """Create an assistant message dict from LLMResponse."""
    msg = {"role": "assistant"}
    if response.content:
        msg["content"] = response.content
    if response.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in response.tool_calls
        ]
    return msg


def format_llm_error_for_user(error: Exception) -> str:
    """Format LLM error for user display."""
    return f"Erro no modelo de linguagem: {str(error)[:200]}"


# =============================================================================
# CLEANUP
# =============================================================================

async def close_clients():
    """Close HTTP clients on shutdown."""
    global _async_client, _sync_client
    if _async_client:
        await _async_client.close()
        _async_client = None
    if _sync_client:
        _sync_client.close()
        _sync_client = None
