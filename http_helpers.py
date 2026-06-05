# =============================================================================
# http_helpers.py — HTTP retry helpers (public API)
# =============================================================================

import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


_SECRET_PATTERNS = [
    re.compile(
        r'(?i)(api[_-]?key|authorization|x-api-key|ocp-apim-subscription-key|bearer)\s*[:=]\s*["\']?(?:Bearer\s+)?[\w\-\.]+'
    ),
    re.compile(r'(?i)(key|token|secret|password|pat)\s*[:=]\s*["\']?[\w\-\.]{8,}'),
    re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),
    re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}'),
]


def _log(prefix: str, msg: str) -> None:
    logging.info("[%s] %s", prefix, msg)


def _sanitize_error_response(text: str | None, max_len: int = 200) -> str:
    """Redact potential secrets from HTTP error bodies before logging."""
    if not text:
        return ""
    truncated = str(text)[:max_len]
    for pattern in _SECRET_PATTERNS:
        truncated = pattern.sub("[REDACTED]", truncated)
    return truncated


async def _request_with_retry(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    content_body: str | bytes | None = None,
    max_retries: int = 3,
    timeout: int = 30,
    log_prefix: str = "HTTP",
    client: httpx.AsyncClient | None = None,
) -> dict:
    request_method = (method or "GET").upper()
    if request_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return {"error": f"{log_prefix} método não suportado: {request_method}"}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(1, max_retries + 1):
            try:
                request_kwargs: dict[str, Any] = {"headers": headers}
                if content_body is not None:
                    request_kwargs["content"] = content_body
                elif json_body is not None:
                    request_kwargs["json"] = json_body

                resp = await client.request(request_method, url, **request_kwargs)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = int(float(retry_after)) if retry_after is not None else 2 ** (attempt - 1)
                    except (TypeError, ValueError):
                        wait = 2 ** (attempt - 1)
                    wait = max(1, min(wait, 30))
                    if attempt == max_retries:
                        return {"error": f"{log_prefix} 429 após {max_retries} tentativas"}
                    _log(log_prefix, f"429 attempt {attempt}/{max_retries}, retry em {wait}s")
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    wait = max(1, min(2 ** (attempt - 1), 30))
                    if attempt == max_retries:
                        return {"error": f"{log_prefix} {resp.status_code} após {max_retries} tentativas"}
                    _log(log_prefix, f"{resp.status_code} attempt {attempt}/{max_retries}, retry em {wait}s")
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    return {
                        "error": f"{log_prefix} {resp.status_code}: {_sanitize_error_response(resp.text, 200)}"
                    }

                if not resp.content:
                    return {}
                try:
                    payload = resp.json()
                except ValueError:
                    return {"error": f"{log_prefix} resposta não-JSON"}
                if isinstance(payload, dict):
                    return payload
                return {"value": payload}

            except httpx.TimeoutException:
                wait = max(1, min(2 ** (attempt - 1), 30))
                if attempt == max_retries:
                    return {"error": f"{log_prefix} timeout após {max_retries} tentativas"}
                _log(log_prefix, f"timeout attempt {attempt}/{max_retries}, retry em {wait}s")
                await asyncio.sleep(wait)
            except httpx.RequestError as e:
                wait = max(1, min(2 ** (attempt - 1), 30))
                if attempt == max_retries:
                    return {"error": f"{log_prefix} request error após {max_retries} tentativas: {str(e)}"}
                _log(log_prefix, f"request error attempt {attempt}/{max_retries}: {str(e)}; retry em {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                wait = max(1, min(2 ** (attempt - 1), 30))
                if attempt == max_retries:
                    return {"error": f"{log_prefix} erro: {str(e)}"}
                _log(log_prefix, f"erro attempt {attempt}/{max_retries}: {str(e)}; retry em {wait}s")
                await asyncio.sleep(wait)
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return {"error": f"{log_prefix} erro desconhecido"}


async def devops_request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    *,
    content_body: str | bytes | None = None,
    max_retries: int = 5,
    timeout: int = 30,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Call Azure DevOps REST API with retry for 429/5xx/timeouts."""
    return await _request_with_retry(
        method=method,
        url=url,
        headers=headers,
        json_body=json_body,
        content_body=content_body,
        max_retries=max_retries,
        timeout=timeout,
        log_prefix="DevOps",
        client=client,
    )


async def search_request_with_retry(
    url: str,
    headers: dict[str, str] | None,
    json_body: Any,
    max_retries: int = 3,
    timeout: int = 30,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """POST to Azure AI Search endpoint with retry for 429/5xx/timeouts."""
    return await _request_with_retry(
        method="POST",
        url=url,
        headers=headers,
        json_body=json_body,
        max_retries=max_retries,
        timeout=timeout,
        log_prefix="Search",
        client=client,
    )
