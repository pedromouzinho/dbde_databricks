# =============================================================================
# tools_figma.py - Figma read-only tool (optional)
# =============================================================================

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse, parse_qs, unquote

import httpx

from config_databricks import FIGMA_ACCESS_TOKEN
from http_helpers import _sanitize_error_response
from tool_registry_databricks import register_tool

logger = logging.getLogger(__name__)

_FIGMA_API_BASE = "https://api.figma.com/v1"
_FIGMA_CACHE_TTL_SECONDS = 300
_MAX_CACHE_ENTRIES = 200
_figma_cache = {}
_http_client: httpx.AsyncClient | None = None


def _get_figma_token() -> str:
    return (FIGMA_ACCESS_TOKEN or "").strip()


def _cache_key(query: str, file_key: str, node_id: str) -> str:
    return f"{(query or '').strip().lower()}|{(file_key or '').strip()}|{(node_id or '').strip()}"


def _parse_figma_url(raw: str) -> dict:
    candidate = str(raw or "").strip()
    if not candidate or "figma.com" not in candidate.lower():
        return {}
    try:
        parsed = urlparse(candidate)
    except Exception:
        return {}
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return {}
    if path_parts[0] not in {"file", "design", "proto"}:
        return {}

    file_key = path_parts[1].strip()
    if not file_key:
        return {}

    query = parse_qs(parsed.query or "")
    node_id = ""
    for key in ("node-id", "node_id", "starting-point-node-id"):
        values = query.get(key) or []
        if values:
            node_id = unquote(str(values[0] or "")).strip()
            break
    node_id = node_id.replace("-", ":") if node_id.count("-") == 1 and ":" not in node_id else node_id
    return {
        "file_key": file_key,
        "node_id": node_id,
    }


def _normalize_figma_inputs(
    query: str = "",
    file_key: str = "",
    node_id: str = "",
    figma_url: str = "",
) -> tuple[str, str, str]:
    q = str(query or "").strip()
    fk = str(file_key or "").strip()
    nid = str(node_id or "").strip()
    raw_url = str(figma_url or "").strip()

    parsed = _parse_figma_url(raw_url)
    if not parsed and "figma.com" in fk.lower():
        parsed = _parse_figma_url(fk)
        if parsed:
            fk = ""
    if not parsed and "figma.com" in q.lower():
        parsed = _parse_figma_url(q)
        if parsed and q.strip() == raw_url.strip():
            q = ""

    if parsed:
        fk = str(parsed.get("file_key", "") or fk).strip()
        if not nid:
            nid = str(parsed.get("node_id", "") or "").strip()

    return q, fk, nid


def _cache_get(key: str):
    hit = _figma_cache.get(key)
    if not hit:
        return None
    if datetime.now(timezone.utc) - hit["ts"] > timedelta(seconds=_FIGMA_CACHE_TTL_SECONDS):
        _figma_cache.pop(key, None)
        return None
    return hit["data"]


def _cache_set(key: str, data):
    if key in _figma_cache:
        _figma_cache.pop(key, None)
    if len(_figma_cache) >= _MAX_CACHE_ENTRIES:
        oldest_key = next(iter(_figma_cache))
        _figma_cache.pop(oldest_key, None)
    _figma_cache[key] = {"ts": datetime.now(timezone.utc), "data": data}


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


async def _figma_get(path: str, params=None):
    token = _get_figma_token()
    if not token:
        return {"error": "Integração Figma não configurada (token em falta)"}
    headers = {"X-Figma-Token": token}
    url = f"{_FIGMA_API_BASE}{path}"
    client = _get_http_client()
    for attempt in range(1, 4):
        try:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "2")), 20)
                if attempt == 3:
                    return {"error": "Figma 429: limite de requests"}
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                if attempt == 3:
                    return {"error": f"Figma {resp.status_code}: erro servidor"}
                await asyncio.sleep(attempt)
                continue
            if resp.status_code >= 400:
                return {
                    "error": f"Figma {resp.status_code}: {_sanitize_error_response(resp.text, 200)}"
                }
            return resp.json()
        except httpx.TimeoutException:
            if attempt == 3:
                return {"error": "Figma timeout"}
            await asyncio.sleep(attempt)
        except Exception as e:
            if attempt == 3:
                return {"error": f"Figma erro: {str(e)}"}
            await asyncio.sleep(attempt)
    return {"error": "Figma erro desconhecido"}


def _match_query(text: str, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    return q in (text or "").strip().lower()


def _figma_file_url(file_key: str, node_id: str = "") -> str:
    safe_key = quote(str(file_key or "").strip(), safe="")
    if not node_id:
        return f"https://www.figma.com/file/{safe_key}"
    return f"https://www.figma.com/file/{safe_key}?node-id={quote(str(node_id).strip(), safe='')}"


def _normalize_node_ids(node_ids) -> list[str]:
    if isinstance(node_ids, list):
        return [str(x).strip() for x in node_ids if str(x).strip()]

    raw = str(node_ids or "").strip()
    if not raw:
        return []

    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass

    return [part.strip() for part in raw.split(",") if part.strip()]


def _extract_transition_targets(document: dict) -> list[str]:
    if not isinstance(document, dict):
        return []

    targets = []
    direct = document.get("transitionNodeID")
    if direct:
        targets.append(str(direct))

    interactions = document.get("interactions") or []
    if isinstance(interactions, list):
        for interaction in interactions:
            if not isinstance(interaction, dict):
                continue
            actions = interaction.get("actions") or []
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    destination = action.get("destinationId") or action.get("nodeId")
                    if destination:
                        targets.append(str(destination))

    deduped = []
    seen = set()
    for node_id in targets:
        key = str(node_id).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _collect_ui_components(document: dict) -> list[str]:
    children = document.get("children") or []
    if not isinstance(children, list):
        return []

    keywords = (
        "button", "btn", "cta", "continuar", "confirmar", "cancelar",
        "input", "campo", "iban", "email", "password", "dropdown",
        "select", "modal", "toast", "card", "header", "tab", "stepper",
        "toggle", "checkbox", "radio", "erro", "error",
    )
    items = []
    for child in children[:120]:
        if not isinstance(child, dict):
            continue
        name = str(child.get("name", "") or "").strip()
        ctype = str(child.get("type", "") or "NODE").strip()
        if not name:
            continue
        lowered = name.lower()
        if any(k in lowered for k in keywords) or ctype.upper() in {
            "COMPONENT", "COMPONENT_SET", "INSTANCE", "TEXT", "FRAME",
        }:
            items.append(f"{ctype.title()}: {name}")

    deduped = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped[:25]


def _infer_step_action(node_name: str, ui_components: list[str]) -> str:
    n = str(node_name or "").lower()
    comps = " ".join(ui_components or []).lower()
    text = f"{n} {comps}"
    if any(k in text for k in ("erro", "error", "inválid", "inval")):
        return "Tratamento de erro e validação de feedback ao utilizador"
    if any(k in text for k in ("confirmar", "submeter", "finalizar")):
        return "Confirmação da ação principal do fluxo"
    if any(k in text for k in ("continuar", "próximo", "proximo", "next", "avançar", "avancar")):
        return "Avanço no fluxo para o step seguinte"
    if any(k in text for k in ("input", "campo", "iban", "email", "password", "preench")):
        return "Preenchimento e validação de dados de entrada"
    if any(k in text for k in ("cancelar", "cancel", "fechar", "close")):
        return "Cancelamento/saída controlada do fluxo"
    return "Interação funcional no step atual"


def _branch_type_from_name(node_name: str) -> str:
    lowered = str(node_name or "").lower()
    if "erro" in lowered or "error" in lowered:
        return "error"
    if "fallback" in lowered or "timeout" in lowered:
        return "fallback"
    if "cancel" in lowered or "cancelar" in lowered:
        return "cancel"
    return "other"


def _layout_sort_key(node: dict):
    document = (node or {}).get("document") or {}
    bounds = document.get("absoluteBoundingBox") or {}
    x = bounds.get("x", 0)
    y = bounds.get("y", 0)
    name = str(document.get("name", "") or "")
    return (str(name).lower(), y, x)


async def tool_analyze_figma_flow(
    file_key: str,
    node_ids: str = "",
    start_node_id: str = "",
    include_branches: bool = True,
    max_steps: int = 15,
    figma_url: str = "",
) -> dict:
    _, fk, parsed_node_id = _normalize_figma_inputs("", file_key, start_node_id, figma_url)
    if not fk:
        return {"error": "file_key é obrigatório para analisar fluxo Figma."}

    assumptions = []
    truncated = False
    try:
        safe_max_steps = max(1, int(max_steps or 15))
    except Exception:
        safe_max_steps = 15
    safe_max_steps = min(safe_max_steps, 50)

    normalized_node_ids = _normalize_node_ids(node_ids)
    start_node = str(parsed_node_id or start_node_id or "").strip()
    if not normalized_node_ids and not start_node:
        return {
            "source": "figma",
            "file_key": fk,
            "ordering_mode": "manual",
            "total_steps": 0,
            "steps": [],
            "branches": [],
            "truncated": False,
            "assumptions": ["Fornece node_ids (CSV/lista) ou start_node_id para decompor o fluxo."],
            "warning": "Sem frames para análise. Indica node_ids ou start_node_id.",
        }

    nodes_by_id = {}

    async def _fetch_node(nid: str):
        response = await _figma_get(
            f"/files/{quote(fk, safe='')}/nodes",
            params={"ids": nid},
        )
        if "error" in response:
            return {"error": response["error"]}
        raw_nodes = response.get("nodes", {})
        if not isinstance(raw_nodes, dict) or not raw_nodes:
            return {"error": "Node não encontrado no ficheiro Figma."}
        node_payload = raw_nodes.get(nid)
        if not node_payload:
            node_payload = next(iter(raw_nodes.values()), None)
        if not isinstance(node_payload, dict):
            return {"error": "Resposta inválida da API Figma para o node pedido."}
        return {"data": node_payload}

    discovered_from_start = []
    if not normalized_node_ids and start_node:
        ordering_mode = "prototype_links"
        current = start_node
        visited = set()
        while current and current not in visited and len(discovered_from_start) < safe_max_steps:
            visited.add(current)
            fetched = await _fetch_node(current)
            if "error" in fetched:
                if not discovered_from_start:
                    return {"error": f"Falha ao ler start_node_id '{current}': {fetched['error']}"}
                assumptions.append(f"Paragem na cadeia de protótipo em '{current}': {fetched['error']}")
                break
            node_payload = fetched["data"]
            nodes_by_id[current] = node_payload
            discovered_from_start.append(current)
            doc = node_payload.get("document") or {}
            transitions = _extract_transition_targets(doc)
            next_node = ""
            for candidate in transitions:
                if candidate not in visited:
                    next_node = candidate
                    break
            current = next_node
        if current and current not in visited:
            truncated = True
            assumptions.append(f"Fluxo truncado ao limite max_steps={safe_max_steps}.")
        normalized_node_ids = discovered_from_start[:]
    else:
        ordering_mode = "manual"

    pending_node_ids = [nid for nid in normalized_node_ids if nid and nid not in nodes_by_id]
    batch_size = 50
    for offset in range(0, len(pending_node_ids), batch_size):
        batch_ids = pending_node_ids[offset:offset + batch_size]
        missing_ids = []
        batch_resp = await _figma_get(
            f"/files/{quote(fk, safe='')}/nodes",
            params={"ids": ",".join(batch_ids)},
        )
        if "error" in batch_resp:
            assumptions.append(
                f"Lote de nodes Figma falhou (fallback individual): {batch_resp['error']}"
            )
            missing_ids.extend(batch_ids)
        else:
            raw_nodes = batch_resp.get("nodes", {})
            if not isinstance(raw_nodes, dict) or not raw_nodes:
                missing_ids.extend(batch_ids)
            else:
                for nid in batch_ids:
                    node_payload = raw_nodes.get(nid)
                    if not node_payload and len(batch_ids) == 1:
                        node_payload = next(iter(raw_nodes.values()), None)
                    if isinstance(node_payload, dict):
                        nodes_by_id[nid] = node_payload
                    else:
                        missing_ids.append(nid)

        for nid in missing_ids:
            if nid in nodes_by_id:
                continue
            fetched = await _fetch_node(nid)
            if "error" in fetched:
                assumptions.append(f"Node '{nid}' ignorado: {fetched['error']}")
                continue
            nodes_by_id[nid] = fetched["data"]

    if not nodes_by_id:
        return {"error": "Não foi possível carregar nenhum frame do fluxo Figma."}

    ordered_ids = [nid for nid in normalized_node_ids if nid in nodes_by_id]
    if ordering_mode != "manual":
        graph = {}
        incoming = {}
        for nid, payload in nodes_by_id.items():
            transitions = _extract_transition_targets((payload or {}).get("document") or {})
            graph[nid] = [t for t in transitions if t in nodes_by_id]
            incoming.setdefault(nid, 0)
            for t in graph[nid]:
                incoming[t] = incoming.get(t, 0) + 1

        if graph and any(graph.values()):
            roots = [nid for nid, cnt in incoming.items() if cnt == 0]
            if start_node and start_node in nodes_by_id:
                roots = [start_node] + [r for r in roots if r != start_node]
            chain = []
            seen = set()
            queue = roots[:] if roots else [next(iter(nodes_by_id.keys()))]
            while queue:
                cur = queue.pop(0)
                if cur in seen:
                    continue
                seen.add(cur)
                chain.append(cur)
                for nxt in graph.get(cur, []):
                    if nxt not in seen and nxt not in queue:
                        queue.append(nxt)
            for nid in nodes_by_id.keys():
                if nid not in seen:
                    chain.append(nid)
            ordered_ids = chain
            ordering_mode = "prototype_links"
        else:
            ordered_ids = sorted(nodes_by_id.keys(), key=lambda nid: _layout_sort_key(nodes_by_id.get(nid)))
            ordering_mode = "name_layout_fallback"

    if len(ordered_ids) > safe_max_steps:
        ordered_ids = ordered_ids[:safe_max_steps]
        truncated = True
        assumptions.append(f"Aplicado limite max_steps={safe_max_steps}.")

    steps = []
    secondary_branch_targets = {}
    for idx, nid in enumerate(ordered_ids, 1):
        payload = nodes_by_id.get(nid) or {}
        document = payload.get("document") or {}
        name = str(document.get("name", "") or nid)
        transitions = _extract_transition_targets(document)
        primary_transition = transitions[0] if transitions else ""
        secondary_transitions = transitions[1:] if len(transitions) > 1 else []
        if secondary_transitions:
            secondary_branch_targets[idx] = secondary_transitions

        ui_components = _collect_ui_components(document)
        steps.append(
            {
                "step_index": idx,
                "node_id": nid,
                "node_name": name,
                "ui_components": ui_components,
                "inferred_action": _infer_step_action(name, ui_components),
                "transitions_to": primary_transition,
            }
        )

    branches = []
    if include_branches:
        branch_candidates = {}
        for step in steps:
            node_name = str(step.get("node_name", "") or "")
            nid = str(step.get("node_id", "") or "")
            if re.search(r"(erro|error|fallback|cancel|timeout)", node_name, flags=re.IGNORECASE):
                branch_candidates[nid] = {
                    "node_id": nid,
                    "node_name": node_name,
                    "triggered_from_step": step["step_index"],
                }

        for step_idx, branch_ids in secondary_branch_targets.items():
            for bid in branch_ids:
                if bid in branch_candidates:
                    continue
                payload = nodes_by_id.get(bid)
                if not payload:
                    fetched = await _fetch_node(bid)
                    if "error" not in fetched:
                        payload = fetched["data"]
                        nodes_by_id[bid] = payload
                if payload:
                    bname = str((payload.get("document") or {}).get("name", "") or bid)
                else:
                    bname = bid
                branch_candidates[bid] = {
                    "node_id": bid,
                    "node_name": bname,
                    "triggered_from_step": step_idx,
                }

        for bid, data in branch_candidates.items():
            branches.append(
                {
                    "node_id": bid,
                    "node_name": data.get("node_name", bid),
                    "branch_type": _branch_type_from_name(data.get("node_name", "")),
                    "triggered_from_step": data.get("triggered_from_step"),
                }
            )

    return {
        "source": "figma",
        "file_key": fk,
        "ordering_mode": ordering_mode,
        "total_steps": len(steps),
        "steps": steps,
        "branches": branches,
        "truncated": truncated,
        "assumptions": assumptions,
    }


async def tool_search_figma(query: str = "", file_key: str = "", node_id: str = "", figma_url: str = ""):
    if not _get_figma_token():
        return {"error": "Integração Figma não configurada (token em falta)"}

    q, fk, nid = _normalize_figma_inputs(query, file_key, node_id, figma_url)

    cache_key = _cache_key(q, fk, nid)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if fk:
        file_name = ""
        thumbnail_url = ""
        last_modified = ""
        items = []

        if nid:
            # Fast path: fetch only the requested node and avoid loading full file payload.
            nodes = await _figma_get(
                f"/files/{quote(fk, safe='')}/nodes",
                params={"ids": nid},
            )
            if "error" in nodes:
                return nodes
            file_name = nodes.get("name", "")
            thumbnail_url = nodes.get("thumbnailUrl", "")
            last_modified = nodes.get("lastModified", "")
            raw_nodes = nodes.get("nodes", {})
            for node_key, node_val in raw_nodes.items():
                document = (node_val or {}).get("document", {})
                name = document.get("name", "")
                if _match_query(name, q):
                    items.append(
                        {
                            "id": node_key,
                            "name": name,
                            "type": document.get("type", "NODE"),
                            "file_key": fk,
                            "file_name": file_name,
                            "ui_components": _collect_ui_components(document),
                            "transition_targets": _extract_transition_targets(document),
                            "thumbnail_url": thumbnail_url,
                            "last_modified": last_modified,
                            "url": _figma_file_url(fk, node_key),
                        }
                    )
        else:
            # Use bounded depth to prevent very large responses on large design files.
            file_meta = await _figma_get(
                f"/files/{quote(fk, safe='')}",
                params={"depth": 2},
            )
            if "error" in file_meta:
                return file_meta

            file_name = file_meta.get("name", "")
            thumbnail_url = file_meta.get("thumbnailUrl", "")
            last_modified = file_meta.get("lastModified", "")
            doc = file_meta.get("document", {})
            for page in doc.get("children", [])[:50]:
                page_name = page.get("name", "")
                page_id = page.get("id", "")
                if _match_query(page_name, q):
                    items.append(
                        {
                            "id": page_id,
                            "name": page_name,
                            "type": page.get("type", "PAGE"),
                            "file_key": fk,
                            "file_name": file_name,
                            "ui_components": _collect_ui_components(page),
                            "transition_targets": _extract_transition_targets(page),
                            "thumbnail_url": thumbnail_url,
                            "last_modified": last_modified,
                            "url": _figma_file_url(fk, page_id),
                        }
                    )
                for frame in (page.get("children") or [])[:50]:
                    frame_name = frame.get("name", "")
                    frame_id = frame.get("id", "")
                    if _match_query(frame_name, q):
                        items.append(
                            {
                                "id": frame_id,
                                "name": frame_name,
                                "type": frame.get("type", "FRAME"),
                                "file_key": fk,
                                "file_name": file_name,
                                "page_name": page_name,
                                "ui_components": _collect_ui_components(frame),
                                "transition_targets": _extract_transition_targets(frame),
                                "thumbnail_url": thumbnail_url,
                                "last_modified": last_modified,
                                "url": _figma_file_url(fk, frame_id),
                            }
                        )
            items = items[:100]

        result = {
            "source": "figma",
            "query": q,
            "file_key": fk,
            "total_results": len(items),
            "items": items,
        }
        _cache_set(cache_key, result)
        return result

    # Nota: a API pública do Figma não expõe endpoint para "recent files".
    # Validamos o token com /me e devolvemos instrução clara para usar file_key.
    me = await _figma_get("/me")
    if "error" in me:
        return me

    result = {
        "source": "figma",
        "query": q,
        "total_results": 0,
        "items": [],
        "notice": (
            "A API pública do Figma não disponibiliza listagem de ficheiros recentes por token. "
            "Fornece o file_key para obter detalhes de um ficheiro/frames."
        ),
        "user": {
            "id": me.get("id", ""),
            "email": me.get("email", ""),
            "handle": me.get("handle", ""),
        },
    }
    _cache_set(cache_key, result)
    return result


_SEARCH_FIGMA_DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_figma",
        "description": "Pesquisa no Figma (read-only). Usa quando o utilizador mencionar designs, mockups, ecras, UI ou prototipos.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto de pesquisa em nomes de ficheiro/frame."},
                "figma_url": {"type": "string", "description": "URL completa do Figma (opcional). O tool extrai file_key e node_id automaticamente."},
                "file_key": {"type": "string", "description": "Figma file key para detalhar um ficheiro especifico."},
                "node_id": {"type": "string", "description": "Node/frame id para detalhe especifico dentro do ficheiro."},
            },
        },
    },
}

_ANALYZE_FIGMA_FLOW_DEFINITION = {
    "type": "function",
    "function": {
        "name": "analyze_figma_flow",
        "description": "Analisa um fluxo Figma e decompõe em steps ordenados para geração de User Stories. Usa quando o utilizador fornecer um fluxo/protótipo Figma com múltiplos ecrãs.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key."},
                "figma_url": {"type": "string", "description": "URL completa do Figma (opcional). O tool extrai file_key e start_node_id automaticamente."},
                "node_ids": {"type": "string", "description": "IDs de frames em CSV (ex: 1:2,1:3) ou JSON list string."},
                "start_node_id": {"type": "string", "description": "Node inicial opcional para seguir ligações de protótipo."},
                "include_branches": {"type": "boolean", "description": "Incluir branches de erro/fallback/cancel. Default true."},
                "max_steps": {"type": "integer", "description": "Máximo de steps a processar. Default 15."},
            },
            "required": ["file_key"],
        },
    },
}


def _register_figma_tool() -> None:
    register_tool(
        "search_figma",
        lambda args: tool_search_figma(
            query=args.get("query", ""),
            file_key=args.get("file_key", ""),
            node_id=args.get("node_id", ""),
            figma_url=args.get("figma_url", ""),
        ),
        definition=_SEARCH_FIGMA_DEFINITION,
    )
    register_tool(
        "analyze_figma_flow",
        lambda args: tool_analyze_figma_flow(
            file_key=args.get("file_key", ""),
            node_ids=args.get("node_ids", ""),
            start_node_id=args.get("start_node_id", ""),
            include_branches=args.get("include_branches", True),
            max_steps=args.get("max_steps", 15),
            figma_url=args.get("figma_url", ""),
        ),
        definition=_ANALYZE_FIGMA_FLOW_DEFINITION,
    )
    if _get_figma_token():
        logging.info("[Figma] search_figma e analyze_figma_flow registadas")
    else:
        logging.warning("[Figma] search_figma e analyze_figma_flow registadas sem token (erro controlado)")
