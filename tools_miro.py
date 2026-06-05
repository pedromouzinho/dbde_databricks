# =============================================================================
# tools_miro.py - Miro read-only tool (optional)
# =============================================================================

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from config_databricks import MIRO_ACCESS_TOKEN
from http_helpers import _sanitize_error_response
from tool_registry_databricks import register_tool

logger = logging.getLogger(__name__)

_MIRO_API_BASE = "https://api.miro.com/v2"
_MIRO_CACHE_TTL_SECONDS = 300
_MAX_CACHE_ENTRIES = 200
_miro_cache = {}
_http_client: httpx.AsyncClient | None = None


def _get_miro_token() -> str:
    return (MIRO_ACCESS_TOKEN or "").strip()


def _cache_key(query: str, board_id: str) -> str:
    return f"{(query or '').strip().lower()}|{(board_id or '').strip()}"


def _cache_get(key: str):
    hit = _miro_cache.get(key)
    if not hit:
        return None
    if datetime.now(timezone.utc) - hit["ts"] > timedelta(seconds=_MIRO_CACHE_TTL_SECONDS):
        _miro_cache.pop(key, None)
        return None
    return hit["data"]


def _cache_set(key: str, data):
    if key in _miro_cache:
        _miro_cache.pop(key, None)
    if len(_miro_cache) >= _MAX_CACHE_ENTRIES:
        oldest_key = next(iter(_miro_cache))
        _miro_cache.pop(oldest_key, None)
    _miro_cache[key] = {"ts": datetime.now(timezone.utc), "data": data}


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=25)
    return _http_client


async def _close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


async def _miro_get(path: str, params=None):
    token = _get_miro_token()
    if not token:
        return {"error": "Integração Miro não configurada (token em falta)"}
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_MIRO_API_BASE}{path}"
    client = _get_http_client()
    for attempt in range(1, 4):
        try:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "2")), 20)
                if attempt == 3:
                    return {"error": "Miro 429: limite de requests"}
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                if attempt == 3:
                    return {"error": f"Miro {resp.status_code}: erro servidor"}
                await asyncio.sleep(attempt)
                continue
            if resp.status_code >= 400:
                return {
                    "error": f"Miro {resp.status_code}: {_sanitize_error_response(resp.text, 200)}"
                }
            return resp.json()
        except httpx.TimeoutException:
            if attempt == 3:
                return {"error": "Miro timeout"}
            await asyncio.sleep(attempt)
        except Exception as e:
            if attempt == 3:
                return {"error": f"Miro erro: {str(e)}"}
            await asyncio.sleep(attempt)
    return {"error": "Miro erro desconhecido"}


def _match_query(text: str, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    return q in (text or "").strip().lower()


def _extract_item_text(item: dict) -> str:
    data = item.get("data", {}) if isinstance(item, dict) else {}
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return ""


async def tool_search_miro(query: str = "", board_id: str = ""):
    if not _get_miro_token():
        return {"error": "Integração Miro não configurada (token em falta)"}

    q = (query or "").strip()
    bid = (board_id or "").strip()

    cache_key = _cache_key(q, bid)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if bid:
        board = await _miro_get(f"/boards/{quote(bid, safe='')}")
        if "error" in board:
            return board

        items_resp = await _miro_get(
            f"/boards/{quote(bid, safe='')}/items",
            params={"limit": 50},
        )
        if "error" in items_resp:
            return items_resp

        out_items = []
        for it in items_resp.get("data", []) or []:
            item_text = _extract_item_text(it)
            item_type = it.get("type", "")
            if q and not _match_query(item_text or item_type, q):
                continue
            out_items.append(
                {
                    "id": it.get("id", ""),
                    "type": item_type,
                    "text": item_text,
                    "style": it.get("style", {}),
                    "created_at": it.get("createdAt", ""),
                    "modified_at": it.get("modifiedAt", ""),
                    "created_by": (it.get("createdBy") or {}).get("name", ""),
                    "url": it.get("links", {}).get("self", ""),
                }
            )

        result = {
            "source": "miro",
            "query": q,
            "board_id": bid,
            "board_name": board.get("name", ""),
            "board_url": board.get("viewLink", ""),
            "total_results": len(out_items),
            "items": out_items[:150],
        }
        _cache_set(cache_key, result)
        return result

    boards_resp = await _miro_get("/boards", params={"limit": 50})
    if "error" in boards_resp:
        return boards_resp

    boards = []
    for b in boards_resp.get("data", []) or []:
        name = b.get("name", "")
        description = b.get("description", "")
        if q and not (_match_query(name, q) or _match_query(description, q)):
            continue
        boards.append(
            {
                "id": b.get("id", ""),
                "name": name,
                "description": description,
                "created_at": b.get("createdAt", ""),
                "modified_at": b.get("modifiedAt", ""),
                "owner": (b.get("owner") or {}).get("name", ""),
                "url": b.get("viewLink", ""),
            }
        )

    result = {
        "source": "miro",
        "query": q,
        "total_results": len(boards),
        "items": boards[:100],
    }
    _cache_set(cache_key, result)
    return result


_SEARCH_MIRO_DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_miro",
        "description": "Pesquisa no Miro (read-only). Usa quando o utilizador mencionar workshops, brainstorms, boards, sticky notes ou planning sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto de pesquisa para boards/conteudo."},
                "board_id": {"type": "string", "description": "Board id para detalhar conteudo desse board."},
            },
        },
    },
}


def _register_miro_tool() -> None:
    register_tool(
        "search_miro",
        lambda args: tool_search_miro(
            query=args.get("query", ""),
            board_id=args.get("board_id", ""),
        ),
        definition=_SEARCH_MIRO_DEFINITION,
    )
    if _get_miro_token():
        logging.info("[Miro] search_miro registada")
    else:
        logging.warning("[Miro] search_miro registada sem token (vai devolver erro controlado)")
