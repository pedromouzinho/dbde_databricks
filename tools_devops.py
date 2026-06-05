# =============================================================================
# tools_devops.py — DevOps tooling and LLM-assisted story generation
# =============================================================================

import json
import base64
import logging
import re
import unicodedata
import uuid
import httpx
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Optional
from collections import deque

from config_databricks import (
    DEVOPS_PAT,
    DEVOPS_ORG,
    DEVOPS_PROJECT,
    DEVOPS_FIELDS,
    DEVOPS_AREAS,
    DEVOPS_WORKITEM_TYPES,
    DEBUG_LOG_SIZE,
)
from llm_provider_databricks import llm_simple
from http_helpers import devops_request_with_retry
from tools_export import _attach_auto_csv_export
from tools_learning import _save_writer_profile, _load_writer_profile

logger = logging.getLogger(__name__)

_devops_debug_log: deque = deque(maxlen=DEBUG_LOG_SIZE)

def get_devops_debug_log(): return list(_devops_debug_log)

def _log(msg):
    _devops_debug_log.append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
    logging.info("[Tools] %s", msg)

_WIQL_BLOCKLIST_RE = re.compile(
    r"(?i)(;|--|/\*|\*/|\b(select|drop|delete|update|insert|merge|exec|execute|union)\b)"
)

_WORKITEM_TYPE_MAP = {str(t).strip().lower(): str(t).strip() for t in DEVOPS_WORKITEM_TYPES}
_SAFE_DEVOPS_QUERY_FIELDS = set(DEVOPS_FIELDS) | {
    "System.Description",
    "Microsoft.VSTS.Common.AcceptanceCriteria",
    "System.Tags",
}
_CREATE_WORKITEM_CONFIRMATIONS: dict[str, dict] = {}
US_TEMPLATE_VERSION = "mse-revamp-classic-v1"
US_REQUIRED_SECTIONS = ["Proveniência", "Condições", "Composição", "Comportamento", "Mockup"]
US_PREFERRED_VOCAB = [
    "CTA",
    "Label",
    "Card",
    "Stepper",
    "Modal",
    "Toast",
    "Dropdown",
    "Input",
    "Toggle",
    "Header",
    "Tab",
    "Breadcrumb",
    "Sidebar",
]
US_SECTION_SLUGS = {
    "Proveniência": "proveniencia",
    "Condições": "condicoes",
    "Composição": "composicao",
    "Comportamento": "comportamento",
    "Mockup": "mockup",
}

def _devops_headers():
    return {"Authorization": f"Basic {base64.b64encode(f':{DEVOPS_PAT}'.encode()).decode()}", "Content-Type": "application/json"}

def _devops_url(path):
    return f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_apis/{path}"


def issue_create_workitem_confirmation_token(conv_id: str, user_sub: str = "") -> str:
    token = uuid.uuid4().hex
    _CREATE_WORKITEM_CONFIRMATIONS[token] = {
        "conv_id": str(conv_id or "").strip(),
        "user_sub": str(user_sub or "").strip(),
        "created_at": datetime.now(timezone.utc),
    }
    return token


def consume_create_workitem_confirmation_token(token: str, conv_id: str = "", user_sub: str = "") -> bool:
    safe_token = str(token or "").strip()
    if not safe_token:
        return False
    meta = _CREATE_WORKITEM_CONFIRMATIONS.pop(safe_token, None)
    if not isinstance(meta, dict):
        return False
    created_at = meta.get("created_at")
    if isinstance(created_at, datetime):
        if (datetime.now(timezone.utc) - created_at).total_seconds() > 10 * 60:
            return False
    expected_conv = str(conv_id or "").strip()
    expected_user = str(user_sub or "").strip()
    if expected_conv and str(meta.get("conv_id", "") or "") != expected_conv:
        return False
    if expected_user and str(meta.get("user_sub", "") or "") != expected_user:
        return False
    return True


async def _fetch_workitem_details(
    ids,
    fields,
    headers,
    *,
    client: httpx.AsyncClient,
    timeout: int = 60,
    retry_individual_failures: bool = False,
):
    """Fetch work item details using a shared client and no artificial sleeps."""
    all_details = []
    failed_ids = []
    if not ids:
        return all_details, failed_ids

    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        response = await devops_request_with_retry(
            "POST",
            _devops_url("wit/workitemsbatch?api-version=7.1"),
            headers,
            {"ids": batch, "fields": fields},
            timeout=timeout,
            client=client,
        )
        if "error" in response:
            failed_ids.extend(batch)
            continue
        all_details.extend(response.get("value", []))

    if retry_individual_failures and failed_ids and len(failed_ids) <= 50:
        fl = ",".join(fields)
        remaining = []
        for fid in failed_ids:
            response = await devops_request_with_retry(
                "GET",
                _devops_url(f"wit/workitems/{fid}?fields={fl}&api-version=7.1"),
                headers,
                max_retries=3,
                timeout=timeout,
                client=client,
            )
            if "error" not in response and "id" in response:
                all_details.append(response)
            else:
                remaining.append(fid)
        failed_ids = remaining

    return all_details, failed_ids

def _format_wi(item):
    f = item.get("fields", {})
    a = f.get("System.AssignedTo", {}); c = f.get("System.CreatedBy", {})
    result = {
        "id": item["id"], "type": f.get("System.WorkItemType",""),
        "title": f.get("System.Title","").replace(" | "," — "), "state": f.get("System.State",""),
        "area": f.get("System.AreaPath",""),
        "assigned_to": a.get("displayName","") if isinstance(a,dict) else str(a),
        "created_by": c.get("displayName","") if isinstance(c,dict) else str(c),
        "created_date": f.get("System.CreatedDate",""),
        "url": f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_workitems/edit/{item['id']}",
    }
    # Include extra fields when present (Description, AcceptanceCriteria, Tags)
    desc = f.get("System.Description", "")
    ac = f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
    tags = f.get("System.Tags", "")
    if desc: result["description"] = (desc or "")[:3000]
    if ac: result["acceptance_criteria"] = (ac or "")[:3000]
    if tags: result["tags"] = tags
    return result

def _safe_wiql_literal(value: str, max_len: int = 200) -> str:
    text = str(value or "").strip()
    if max_len > 0:
        text = text[:max_len]
    return text.replace("'", "''")

def _normalize_match_text(value: str) -> str:
    lowered = str(value or "").lower()
    deaccented = unicodedata.normalize("NFKD", lowered)
    clean = "".join(ch for ch in deaccented if not unicodedata.combining(ch))
    clean = clean.replace("|", " ").replace("—", " ").replace("-", " ").replace("_", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

def _canonicalize_area_path(area_path: str) -> str:
    raw = str(area_path or "").strip()
    if not raw:
        return ""
    if "\\" in raw:
        return raw
    norm = _normalize_match_text(raw)
    if not norm:
        return raw
    for known in DEVOPS_AREAS:
        known_norm = _normalize_match_text(known)
        if known_norm.endswith(norm) or norm in known_norm:
            return known
    return raw

def _sanitize_wiql_where(wiql_where: str) -> str:
    where = str(wiql_where or "").strip()
    if where.lower().startswith("where "):
        where = where[6:].strip()
    if not where:
        raise ValueError("wiql_where vazio")
    if len(where) > 2000:
        raise ValueError("wiql_where demasiado longo (max 2000 chars)")
    if _WIQL_BLOCKLIST_RE.search(where):
        raise ValueError("wiql_where contém tokens proibidos")
    if where.count("'") % 2 != 0:
        raise ValueError("wiql_where com aspas simples não balanceadas")
    return where


def _clean_html_for_example(html_text: str) -> str:
    """Remove HTML sujo dos exemplos, mantendo apenas tags limpas."""
    text = str(html_text or "")
    if not text:
        return ""
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)

    allowed_tags = {"b", "ul", "li", "br", "div"}

    def _normalize_tag(match):
        closing = bool(match.group(1))
        tag = str(match.group(2) or "").lower()
        if tag not in allowed_tags:
            return ""
        if tag == "br":
            return "<br>"
        return f"</{tag}>" if closing else f"<{tag}>"

    text = re.sub(r"<\s*(/?)\s*([a-zA-Z0-9]+)([^>]*)>", _normalize_tag, text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _validate_us_output(raw_output: str) -> dict:
    """Valida output de US segundo template canónico MSE."""
    issues = []
    score = 1.0
    cleaned = str(raw_output or "")

    stories = _split_generated_stories(cleaned)
    if not stories:
        stories = [cleaned]

    has_any_title = False
    for story in stories:
        title, ac_html = _extract_story_title_and_ac(story)
        if title:
            has_any_title = True
        flags = _collect_quality_flags(title, ac_html)
        for flag in flags:
            if flag == "missing_mse_prefix":
                issues.append("Título sem prefixo 'MSE |'")
                score -= 0.15
            elif flag == "title_segment_count_invalid":
                issues.append("Título fora de 4-6 segmentos")
                score -= 0.1
            elif flag.startswith("missing_section_"):
                section_slug = flag.replace("missing_section_", "")
                issues.append(f"Secção em falta: {section_slug}")
                score -= 0.08
            elif flag.startswith("section_out_of_order_"):
                section_slug = flag.replace("section_out_of_order_", "")
                issues.append(f"Secção fora de ordem: {section_slug}")
                score -= 0.05
            elif flag == "html_escaped":
                issues.append("HTML escapado detectado (&lt; / &gt;)")
                score -= 0.1

    if not has_any_title:
        issues.append("Sem título detectado")
        score -= 0.15

    if "Eu como <b>" not in cleaned and "Eu como" not in cleaned:
        issues.append("Descrição não segue formato 'Eu como <b>[Persona]</b>'")
        score -= 0.1

    dirty_tags = re.findall(
        r"<(?:font|span\s+style|table|td|tr|th|p\s+style|h[1-6]\s+style)[^>]*>",
        cleaned,
    )
    if dirty_tags:
        issues.append(f"HTML sujo detectado: {dirty_tags[:3]}")
        score -= 0.15
        cleaned = re.sub(r"</?(?:font|span|table|tr|td|th|p)(?:\s[^>]*)?>", "", cleaned)
        cleaned = re.sub(r'\s*style="[^"]*"', "", cleaned)
        cleaned = cleaned.replace("&nbsp;", " ")

    vocab_found = sum(1 for term in US_PREFERRED_VOCAB if term.lower() in cleaned.lower())

    return {
        "valid": len(issues) == 0,
        "score": max(0.0, score),
        "issues": issues,
        "cleaned_output": cleaned,
        "vocab_mse_count": vocab_found,
    }


def _normalize_text_ascii(value: str) -> str:
    txt = unicodedata.normalize("NFKD", str(value or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return txt.lower()


def _unescape_html_if_needed(text):
    raw = str(text or "")
    if ("&lt;" not in raw and "&gt;" not in raw and "&amp;" not in raw and "&quot;" not in raw):
        return raw
    return (
        raw.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&amp;", "&")
    )


def _split_generated_stories(text: str):
    raw = str(text or "").strip()
    if not raw:
        return []

    def _split_by_starts(starts):
        parts = []
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(raw)
            chunk = raw[start:end].strip()
            if chunk:
                parts.append(chunk)
        return parts

    marker_patterns = [
        r"(?im)^(?:###|##)\s*User Story\s+\d+.*$",
        r"(?im)^\s*(?:T[ií]tulo|Title)\s*:\s*MSE\s*\|",
    ]
    for pattern in marker_patterns:
        starts = [m.start() for m in re.finditer(pattern, raw)]
        if len(starts) >= 2:
            return _split_by_starts(starts)

    parts = [p.strip() for p in re.split(r"(?m)^\s*(?:---+|===+)\s*$", raw) if p.strip()]
    if len(parts) >= 2:
        return parts

    return [raw]


def _extract_story_title_and_ac(story_text: str):
    txt = str(story_text or "")
    title = ""

    title_match = re.search(r"(?im)^\s*(?:T[ií]tulo|Title)\s*:\s*(.+)$", txt)
    if title_match:
        title = title_match.group(1).strip()
    else:
        inline_title = re.search(r"(?im)\bMSE\s*\|[^\n\r]+", txt)
        if inline_title:
            title = inline_title.group(0).strip()

    ac_html = txt
    ac_match = re.search(
        r"(?is)(?:Crit[eé]rios?\s+de\s+Aceita[cç][aã]o|AC)\s*:?\s*(.+)$",
        txt,
    )
    if ac_match:
        ac_html = ac_match.group(1).strip()

    return title, ac_html


def _check_required_sections(ac_html):
    text = str(ac_html or "")
    normalized_text = _normalize_text_ascii(text)
    if not normalized_text:
        return {"missing": list(US_REQUIRED_SECTIONS), "out_of_order": []}

    positions = {}
    missing = []
    for section in US_REQUIRED_SECTIONS:
        target = _normalize_text_ascii(section)
        bold_match = re.search(rf"<b>\s*{re.escape(target)}\s*</b>", normalized_text, flags=re.IGNORECASE)
        plain_pos = normalized_text.find(target)
        pos = bold_match.start() if bold_match else plain_pos
        if pos < 0:
            missing.append(section)
        else:
            positions[section] = pos

    out_of_order = []
    last_pos = -1
    for section in US_REQUIRED_SECTIONS:
        if section in missing:
            continue
        pos = positions.get(section, -1)
        if pos < last_pos:
            out_of_order.append(section)
        else:
            last_pos = pos

    return {"missing": missing, "out_of_order": out_of_order}


def _collect_quality_flags(title, ac_html):
    title_txt = str(title or "").strip()
    ac_txt = str(ac_html or "")
    flags = []

    if not title_txt.startswith("MSE |"):
        flags.append("missing_mse_prefix")

    segments = [seg.strip() for seg in title_txt.split(" | ") if seg.strip()]
    if not (4 <= len(segments) <= 6):
        flags.append("title_segment_count_invalid")

    section_result = _check_required_sections(ac_txt)
    for section in section_result.get("missing", []):
        slug = US_SECTION_SLUGS.get(section, _normalize_text_ascii(section).replace(" ", "_"))
        flags.append(f"missing_section_{slug}")
    for section in section_result.get("out_of_order", []):
        slug = US_SECTION_SLUGS.get(section, _normalize_text_ascii(section).replace(" ", "_"))
        flags.append(f"section_out_of_order_{slug}")

    combined = f"{title_txt}\n{ac_txt}"
    if "&lt;" in combined or "&gt;" in combined:
        flags.append("html_escaped")

    return flags


def _extract_user_template_request(context, topic):
    keyword_patterns = [
        "template",
        "formato",
        "estrutura",
        "segue este padrao",
        "usa este modelo",
        "use this format",
        "follow this structure",
    ]
    sources = [str(context or "").strip(), str(topic or "").strip()]

    for source in sources:
        if not source:
            continue
        normalized_source = _normalize_text_ascii(source)
        has_keyword = any(k in normalized_source for k in keyword_patterns)
        lines = [line.rstrip() for line in source.splitlines()]
        structured_lines = [
            line for line in lines
            if line.strip() and (
                ":" in line
                or line.strip().startswith("#")
                or line.strip().startswith("- ")
                or line.strip().startswith("* ")
                or "**" in line
            )
        ]
        if has_keyword and len(structured_lines) >= 3:
            return "\n".join(line.strip() for line in lines if line.strip())[:4000]

    merged = "\n".join(part for part in sources if part).strip()
    if not merged:
        return None
    normalized_merged = _normalize_text_ascii(merged)
    has_keyword = any(k in normalized_merged for k in keyword_patterns)
    lines = [line.rstrip() for line in merged.splitlines()]
    structured_lines = [
        line for line in lines
        if line.strip() and (
            ":" in line
            or line.strip().startswith("#")
            or line.strip().startswith("- ")
            or line.strip().startswith("* ")
            or "**" in line
        )
    ]
    if has_keyword and len(structured_lines) >= 3:
        return "\n".join(line.strip() for line in lines if line.strip())[:4000]
    return None


def _resolve_detail_policy(context, topic):
    user_template = _extract_user_template_request(context, topic)
    if user_template:
        return {"policy": "user_template", "user_template": user_template}
    return {"policy": "habitual", "user_template": None}


def _extract_flow_context_json(context):
    marker = "FLOW_CONTEXT_JSON:"
    raw = str(context or "")
    idx = raw.find(marker)
    if idx < 0:
        return None
    payload = raw[idx + len(marker):].strip()
    if not payload:
        return None

    parsed = None
    try:
        parsed = json.loads(payload)
    except Exception:
        parsed = _extract_json_object(payload)

    if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
        return parsed
    return None

def _extract_json_object(text: str):
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _validate_workitem_type(value: str, default: str = "User Story") -> str:
    candidate = str(value or default).strip().lower()
    safe = _WORKITEM_TYPE_MAP.get(candidate)
    if not safe:
        raise ValueError(
            f"Tipo de work item inválido: '{value}'. Permitidos: {', '.join(DEVOPS_WORKITEM_TYPES)}"
        )
    return safe

async def _resolve_parent_id_by_title_hint(
    headers: dict,
    *,
    parent_type: str,
    area_path: str = "",
    title_hint: str = "",
) -> tuple[Optional[int], dict]:
    hint_raw = str(title_hint or "").strip()
    hint_norm = _normalize_match_text(hint_raw)
    score_terms = [t for t in hint_norm.split(" ") if t][:8]
    wiql_terms_src = re.sub(r"[|—\\-_]", " ", hint_raw)
    wiql_terms_src = re.sub(r"\s+", " ", wiql_terms_src).strip()
    wiql_terms = [t for t in wiql_terms_src.split(" ") if t][:8]
    if not wiql_terms:
        wiql_terms = score_terms[:]
    if not score_terms:
        score_terms = [_normalize_match_text(t) for t in wiql_terms]
        score_terms = [t for t in score_terms if t][:8]
    if not score_terms:
        return None, {"attempted": False}

    parent_type_norm = str(parent_type or "").strip().lower()
    apply_area_filter = bool(area_path and parent_type_norm != "epic")
    base_conds = [
        f"[System.TeamProject] = '{_safe_wiql_literal(DEVOPS_PROJECT, 120)}'",
        f"[System.WorkItemType] = '{_safe_wiql_literal(parent_type, 80)}'",
    ]
    if apply_area_filter:
        base_conds.append(f"[System.AreaPath] UNDER '{_safe_wiql_literal(area_path, 300)}'")
    strict_conds = list(base_conds)
    for term in wiql_terms:
        strict_conds.append(f"[System.Title] CONTAINS '{_safe_wiql_literal(term, 80)}'")

    wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE {' AND '.join(strict_conds)} "
        "ORDER BY [System.ChangedDate] DESC"
    )
    resp = await devops_request_with_retry(
        "POST",
        _devops_url("wit/wiql?api-version=7.1"),
        headers,
        {"query": wiql},
        timeout=60,
    )
    if "error" in resp:
        return None, {
            "attempted": True,
            "area_filter_applied": apply_area_filter,
            "error": resp.get("error", "resolve_parent_failed"),
            "wiql_terms": wiql_terms,
        }

    ids = [wi.get("id") for wi in resp.get("workItems", []) if wi.get("id")]
    fallback_broad_used = False
    if not ids and wiql_terms:
        fallback_wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {' AND '.join(base_conds)} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        fallback_resp = await devops_request_with_retry(
            "POST",
            _devops_url("wit/wiql?api-version=7.1"),
            headers,
            {"query": fallback_wiql},
            timeout=60,
        )
        if "error" not in fallback_resp:
            ids = [wi.get("id") for wi in fallback_resp.get("workItems", []) if wi.get("id")]
            fallback_broad_used = True

    if not ids:
        return None, {
            "attempted": True,
            "area_filter_applied": apply_area_filter,
            "matched_candidates": 0,
            "wiql_terms": wiql_terms,
            "fallback_broad_used": fallback_broad_used,
        }

    batch_ids = ids[: min(50, len(ids))]
    det = await devops_request_with_retry(
        "POST",
        _devops_url("wit/workitemsbatch?api-version=7.1"),
        headers,
        {"ids": batch_ids, "fields": ["System.Id", "System.Title", "System.WorkItemType", "System.AreaPath"]},
        timeout=60,
    )
    if "error" in det:
        return None, {
            "attempted": True,
            "area_filter_applied": apply_area_filter,
            "matched_candidates": len(ids),
            "error": det.get("error", "resolve_parent_batch_failed"),
            "wiql_terms": wiql_terms,
            "fallback_broad_used": fallback_broad_used,
        }

    best_id = None
    best_score = -1
    exact_hits = 0
    exact_title_hits = 0
    for it in det.get("value", []):
        f = it.get("fields", {})
        title_norm = _normalize_match_text(str(f.get("System.Title", "") or ""))
        score = sum(1 for term in score_terms if term in title_norm)
        if score_terms and score == len(score_terms):
            exact_hits += 1
        if hint_norm and title_norm == hint_norm:
            exact_title_hits += 1
            score += 100
        elif hint_norm and title_norm.startswith(hint_norm):
            score += 20
        if score > best_score:
            best_score = score
            best_id = it.get("id")

    if best_id is None:
        return None, {
            "attempted": True,
            "area_filter_applied": apply_area_filter,
            "matched_candidates": len(ids),
            "scored_candidates": len(det.get("value", [])),
            "wiql_terms": wiql_terms,
            "fallback_broad_used": fallback_broad_used,
        }
    return int(best_id), {
        "attempted": True,
        "area_filter_applied": apply_area_filter,
        "matched_candidates": len(ids),
        "scored_candidates": len(det.get("value", [])),
        "best_score": best_score,
        "max_score": len(score_terms),
        "exact_hits": exact_hits,
        "exact_title_hits": exact_title_hits,
        "wiql_terms": wiql_terms,
        "fallback_broad_used": fallback_broad_used,
    }

async def tool_query_workitems(wiql_where: str, fields: Optional[list[str]] = None, top: int = 200, user_sub: str = "") -> dict:
    _log(f"query_workitems: top={top}, wiql={str(wiql_where)[:80]}...")
    try:
        safe_where = _sanitize_wiql_where(wiql_where)
    except ValueError as e:
        return {"error": f"WIQL inválido: {e}"}
    _ = user_sub
    requested_fields = fields if isinstance(fields, list) and fields else DEVOPS_FIELDS
    use_fields = [f for f in requested_fields if f in _SAFE_DEVOPS_QUERY_FIELDS]
    if not use_fields:
        use_fields = list(DEVOPS_FIELDS)
    wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{_safe_wiql_literal(DEVOPS_PROJECT, 120)}' "
        f"AND {safe_where} ORDER BY [System.ChangedDate] DESC"
    )
    headers = _devops_headers()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await devops_request_with_retry(
            "POST",
            _devops_url("wit/wiql?api-version=7.1"),
            headers,
            {"query": wiql},
            timeout=60,
            client=client,
        )
        if "error" in resp:
            return resp
        work_items = resp.get("workItems", [])
        total_count = len(work_items)
        if top == 0:
            return {"total_count": total_count, "items": []}
        work_items = work_items[: min(top, 1000) if top > 0 else total_count]
        if not work_items:
            return {"total_count": 0, "items": []}
        ids = [wi["id"] for wi in work_items]
        all_details, failed_ids = await _fetch_workitem_details(
            ids,
            use_fields,
            headers,
            client=client,
            timeout=60,
            retry_individual_failures=True,
        )
    items = [_format_wi(it) for it in all_details]
    if failed_ids and not items:
        items = [
            {
                "id": fid,
                "type": "",
                "title": "(rate limited)",
                "state": "",
                "url": f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_workitems/edit/{fid}",
            }
            for fid in failed_ids
        ]
    result = {"total_count": total_count, "items_returned": len(items), "items": items}
    await _attach_auto_csv_export(
        result,
        title_hint=f"query_workitems_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
        user_sub=user_sub,
    )
    if failed_ids:
        result["_partial"] = True
        result["_failed_batch_count"] = len(failed_ids)
    return result

async def tool_analyze_patterns(created_by: Optional[str] = None, topic: Optional[str] = None, work_item_type: str = "User Story", area_path: Optional[str] = None, sample_size: int = 15) -> dict:
    try:
        safe_type = _validate_workitem_type(work_item_type, "User Story")
    except ValueError as e:
        return {"error": str(e)}

    conds = [f"[System.WorkItemType]='{_safe_wiql_literal(safe_type, 80)}'"]
    if created_by:
        conds.append(f"[System.CreatedBy] CONTAINS '{_safe_wiql_literal(created_by, 200)}'")
    if topic:
        conds.append(f"[System.Title] CONTAINS '{_safe_wiql_literal(topic, 200)}'")
    if area_path:
        conds.append(f"[System.AreaPath] UNDER '{_safe_wiql_literal(area_path, 300)}'")
    else:
        conds.append(
            "(" + " OR ".join(
                f"[System.AreaPath] UNDER '{_safe_wiql_literal(a, 300)}'" for a in DEVOPS_AREAS
            ) + ")"
        )
    result = await tool_query_workitems(" AND ".join(conds), top=sample_size)
    if "error" in result: return result
    ids = [it.get("id") for it in result.get("items",[]) if it.get("id")]
    samples = []
    if ids:
        det_fields = DEVOPS_FIELDS + ["System.Description","Microsoft.VSTS.Common.AcceptanceCriteria","System.Tags"]
        try:
            r = await devops_request_with_retry(
                "POST",
                _devops_url("wit/workitemsbatch?api-version=7.1"),
                _devops_headers(),
                {"ids": ids[:sample_size], "fields": det_fields},
                timeout=30,
            )
            if "error" not in r:
                for it in r.get("value",[]):
                    f=it.get("fields",{}); cb=f.get("System.CreatedBy",{})
                    samples.append({"id":it["id"],"title":f.get("System.Title","").replace(" | "," — "),"created_by":cb.get("displayName","") if isinstance(cb,dict) else str(cb),"description":(f.get("System.Description","") or "")[:2000],"acceptance_criteria":(f.get("Microsoft.VSTS.Common.AcceptanceCriteria","") or "")[:3000],"tags":f.get("System.Tags","")})
        except Exception as e:
            logging.error("[Tools] tool_analyze_patterns LLM block failed: %s", e)
    if not samples: samples = [{"id":it.get("id"),"title":it.get("title","")} for it in result.get("items",[])]
    return {"total_found": result.get("total_count",0), "samples_returned": len(samples), "analysis_data": samples}

async def tool_analyze_patterns_with_llm(created_by: Optional[str] = None, topic: Optional[str] = None, work_item_type: str = "User Story", area_path: Optional[str] = None, sample_size: int = 15, analysis_type: str = "template", user_sub: str = "") -> dict:
    raw = await tool_analyze_patterns(created_by, topic, work_item_type, area_path, sample_size)
    if "error" in raw or raw.get("samples_returned",0)==0: return raw
    txt = ""
    for i,s in enumerate(raw.get("analysis_data",[])[:15],1):
        txt += f"\n--- Exemplo {i} (ID {s.get('id','?')}) ---\nTítulo: {s.get('title','')}\nCriado por: {s.get('created_by','')}\n"
        if s.get("description"): txt += f"Descrição: {s['description'][:600]}\n"
        if s.get("acceptance_criteria"): txt += f"Critérios: {s['acceptance_criteria'][:600]}\n"
    prompts = {"template": f"Analisa {raw['samples_returned']} {work_item_type}s e extrai PADRÃO DE ESCRITA.\n\n{txt}\n\nExtrai: 1.Estrutura 2.Linguagem 3.Campos 4.Template 5.Observações\nPT-PT.", "author_style": f"Analisa estilo de '{created_by or 'autor'}' em:\n\n{txt}\n\nDescreve: estilo, estrutura, vocabulário, detalhe, template.\nPT-PT."}
    fallback_prompt = f"Analisa:\n{txt}\nPT-PT."
    try: analysis = await llm_simple(f"És analista de padrões de escrita.\n\n{prompts.get(analysis_type, fallback_prompt)}", tier="pro", max_tokens=2000)
    except Exception as e:
        logging.error("[Tools] tool_analyze_patterns_with_llm failed: %s", e)
        analysis = f"Erro: {e}"
    profile_saved = False
    preferred_vocabulary = ""
    title_pattern = ""
    ac_structure = ""

    vocab_set = set()
    title_patterns = []
    ac_section_hits = []
    for sample in raw.get("analysis_data", [])[:15]:
        combined = f"{sample.get('description', '')} {sample.get('acceptance_criteria', '')}".lower()
        for term in ["CTA", "Toast", "Modal", "Input", "Dropdown", "Stepper", "Enable", "Disable", "FEE"]:
            if term.lower() in combined:
                vocab_set.add(term)
        title = str(sample.get("title", "") or "")
        if "|" in title:
            parts = [part.strip() for part in title.split("|") if part.strip()]
            if len(parts) >= 3:
                title_patterns.append("|".join(parts[:3]) + "|...")
        ac_text = str(sample.get("acceptance_criteria", "") or "")
        for section in ("Objetivo", "Âmbito", "Composição", "Layout", "Comportamento", "Regras", "Mockup"):
            if section.lower() in ac_text.lower():
                ac_section_hits.append(section)

    if vocab_set:
        preferred_vocabulary = ", ".join(sorted(vocab_set))[:2000]
    if title_patterns:
        counts = {}
        for item in title_patterns:
            counts[item] = counts.get(item, 0) + 1
        title_pattern = max(counts, key=counts.get)[:500]
    if ac_section_hits:
        ordered_sections = []
        for section in ("Objetivo", "Âmbito", "Composição", "Layout", "Comportamento", "Regras", "Mockup"):
            if section in ac_section_hits:
                ordered_sections.append(section)
        if ordered_sections:
            ac_structure = " > ".join(ordered_sections)[:1000]

    if analysis_type == "author_style" and created_by and isinstance(analysis, str) and not analysis.startswith("Erro:"):
        profile_saved = await _save_writer_profile(
            author_name=created_by,
            analysis=analysis,
            sample_ids=[s.get("id") for s in raw.get("analysis_data", []) if s.get("id")],
            sample_count=raw.get("samples_returned", 0),
            topic=topic or "",
            work_item_type=work_item_type,
            preferred_vocabulary=preferred_vocabulary,
            title_pattern=title_pattern,
            ac_structure=ac_structure,
            owner_sub=user_sub,
        )

    return {
        "total_found": raw.get("total_found",0),
        "samples_analyzed": raw.get("samples_returned",0),
        "analysis_type": analysis_type,
        "analysis": analysis,
        "sample_ids": [s.get("id") for s in raw.get("analysis_data",[])],
        "writer_profile_saved": profile_saved,
    }

async def tool_generate_user_stories(topic: str, context: str = "", num_stories: int = 3, reference_area: Optional[str] = None, reference_author: Optional[str] = None, reference_topic: Optional[str] = None, user_sub: str = "") -> dict:
    style_profile = None
    if reference_author:
        style_profile = await _load_writer_profile(reference_author, owner_sub=user_sub)

    raw = {"samples_returned": 0, "analysis_data": []}
    reference_ids = []
    style_hint = ""
    ex = ""

    if style_profile and style_profile.get("style_analysis"):
        _log(f"generate_user_stories: using cached writer profile for '{reference_author}'")
        reference_ids = style_profile.get("sample_ids", [])
        style_hint = (
            f"\nPERFIL DE ESCRITA CACHEADO ({style_profile.get('author_name', reference_author)}):\n"
            f"{style_profile.get('style_analysis', '')[:3000]}\n"
        )
        vocab = style_profile.get("preferred_vocabulary", "")
        if vocab:
            style_hint += f"\nVOCABULÁRIO PREFERIDO DO AUTOR: {vocab}\n"
        title_pattern_hint = style_profile.get("title_pattern", "")
        if title_pattern_hint:
            style_hint += f"TÍTULO PADRÃO DO AUTOR: {title_pattern_hint}\n"
        ac_structure_hint = style_profile.get("ac_structure", "")
        if ac_structure_hint:
            style_hint += f"ESTRUTURA DE AC PREFERIDA: {ac_structure_hint}\n"
        ex = "(Perfil de autor carregado de WriterProfiles; não foi necessário reanalisar padrões.)"
    else:
        search_topic = reference_topic or topic
        raw = await tool_analyze_patterns(
            created_by=reference_author,
            topic=(search_topic[:35] if len(search_topic) > 35 else search_topic) or None,
            area_path=reference_area,
            sample_size=20,
        )
        if raw.get("samples_returned", 0) < 5:
            raw2 = await tool_analyze_patterns(
                created_by=reference_author,
                area_path=reference_area,
                sample_size=20,
            )
            if raw2.get("samples_returned", 0) > raw.get("samples_returned", 0):
                raw = raw2
        for i, s in enumerate(raw.get("analysis_data", [])[:8], 1):
            ex += f"\n{'='*50}\nEXEMPLO {i} (ID:{s.get('id','?')})\n{'='*50}\nTÍTULO: {s.get('title','')}\nCRIADOR: {s.get('created_by','')}\n"
            if s.get("description"):
                clean_desc = _clean_html_for_example(s["description"][:1500])
                ex += f"DESC:\n{clean_desc}\n"
            if s.get("acceptance_criteria"):
                clean_ac = _clean_html_for_example(s["acceptance_criteria"][:2000])
                ex += f"AC:\n{clean_ac}\n"
        if not ex:
            ex = "(Sem exemplos — usa boas práticas)"
        reference_ids = [s.get("id") for s in raw.get("analysis_data", [])]

    detail_policy = _resolve_detail_policy(context, topic)
    policy = detail_policy.get("policy", "habitual")
    user_template = detail_policy.get("user_template")
    flow_context = _extract_flow_context_json(context)
    flow_mode = isinstance(flow_context, dict)
    flow_steps = flow_context.get("steps", []) if flow_mode else []
    flow_branches = flow_context.get("branches", []) if flow_mode else []

    try:
        requested_num_stories = max(1, int(num_stories))
    except Exception:
        requested_num_stories = 1
    effective_num_stories = requested_num_stories

    preferred_vocab = ", ".join(US_PREFERRED_VOCAB)
    canonical_template = (
        "TEMPLATE CANÓNICO MSE (default):\n"
        "Título: MSE | [Domínio] | [Jornada/Subárea] | [Fluxo/Step] | [Detalhe da Alteração]\n"
        "- 4 a 6 segmentos obrigatórios separados por ' | '\n"
        "- Se o domínio não for inferível, usar 'Transversal'\n\n"
        "Descrição:\n"
        "<div>Como cliente do banco com interesse em <b>[objetivo]</b>, quero <b>[ação ou ecrã desejado]</b>, para que <b>[benefício ou resultado esperado]</b>.</div>\n\n"
        "Secções obrigatórias (ordem fixa):\n"
        "1) <b>Proveniência</b> + <ul><li>Trajeto do utilizador até ao ecrã/funcionalidade.</li></ul>\n"
        "2) <b>Condições</b> + <ul><li>Pré-requisitos de acesso. Usar 'NA' se não houver.</li></ul>\n"
        "3) <b>Composição & Comportamento</b> + <ul><li>...</li></ul>\n"
        "   COMO PREENCHER:\n"
        "   - Enumerar TODOS os elementos visuais: H1, H2, body text, cards, CTAs, inputs, dropdowns, toggles, links, ícones, listas, tabelas, modais, toasts, badges, tabs, steppers.\n"
        "   - Para cada texto visível: versão PT e EN lado a lado — ex: 'Transferir' / 'Transfer'.\n"
        "   - Descrever hierarquia visual, estados condicionais e comportamento de cada elemento interativo.\n"
        "   - Incluir acessibilidade natural: ordem de leitura, foco visível, navegação por teclado e rótulos claros.\n"
        "4) <b>Critérios de Aceitação</b> + <ul><li>Criar critérios claros e mensuráveis com IDs CA-01, CA-02, ...</li></ul>\n"
        "5) <b>Cenários de Teste</b> + <ul><li>Criar cenários CT-01, CT-02, ... com categoria, pré-condições, dados de teste, passos Dado/Quando/Então e referência aos CAs cobertos.</li></ul>\n"
        "6) <b>Dados de Teste</b> + <ul><li>Montantes, IBAN, datas, sessão, conectividade e casos-limite relevantes ao contexto.</li></ul>\n"
        "7) <b>Observações, Assunções e Riscos</b> + <ul><li>Dependências, feature flags, restrições legais/comerciais e riscos conhecidos.</li></ul>\n"
        "8) <b>Mockup</b> + <ul><li>Mockup a confirmar com UX.</li></ul>\n"
    )
    common_rules = (
        "REGRAS OBRIGATÓRIAS:\n"
        "- PT-PT sempre.\n"
        "- Textos de interface sempre em PT e EN: 'Confirmar' / 'Confirm'.\n"
        "- Hierarquia visual explícita: H1, H2 e body em cada ecrã.\n"
        "- Acessibilidade natural: foco, teclado, rótulos e leitura lógica — sem citar normas.\n"
        "- Estados condicionais obrigatórios: dados em falta, serviço indisponível, sem resultados, sessão expirada, timeout, offline.\n"
        "- Não inventar endpoints, APIs, serviços de backoffice nem arquitetura técnica sem evidência explícita.\n"
        "- Quando faltar contexto de negócio, adicionar secção <b>Assunções</b> no final dos AC.\n"
        "- Vocabulário preferencial: " + preferred_vocab + ".\n"
        "- HTML limpo e não escapado (nunca produzir &lt;, &gt;, &amp; ou &quot;).\n"
        "- Prioridade da estrutura: template aplicável > WriterProfile histórico.\n"
        "- Critérios de Aceitação com IDs CA-XX e cenários com IDs CT-XX.\n"
        "- Cada cenário deve referir explicitamente os CAs cobertos.\n"
        "- Cobertura mínima: fluxo principal, validações, erros/estados vazios, acessibilidade, segurança/privacidade, internacionalização/formatação, navegação e desempenho/resiliência.\n"
        "\nPERGUNTAS DE CLARIFICAÇÃO (levantar se a entrada for incompleta):\n"
        "- Fluxos alternativos ou exceções?\n"
        "- Limites de montante, arredondamento, taxas?\n"
        "- Mensagens de erro — texto e posicionamento?\n"
        "- Offline? Indicação de progresso/erro de rede?\n"
        "- Privacidade (mascarar dados, timeouts, logout)?\n"
        "- Estados vazios — o que mostrar?\n"
        "- Formatação i18n (moeda, data, separadores)?\n"
        "- Dispositivos-alvo (mobile/tablet/desktop)?\n"
    )

    if policy == "user_template" and user_template:
        policy_block = (
            "POLÍTICA DE DETALHE: user_template\n"
            "Seguir estritamente o formato pedido pelo utilizador abaixo.\n"
            "Se o template do utilizador não definir secções de AC, usar fallback das 5 secções canónicas.\n"
            "TEMPLATE DO UTILIZADOR:\n"
            f"{user_template}\n"
        )
    else:
        policy_block = (
            "POLÍTICA DE DETALHE: habitual\n"
            "Usar o template canónico MSE por defeito.\n\n"
            f"{canonical_template}\n"
        )

    flow_context_block = ""
    flow_story_map = []
    flow_steps_detected = 0
    context_clean = str(context or "")
    if flow_mode:
        flow_steps_clean = []
        for raw_step in flow_steps:
            if not isinstance(raw_step, dict):
                continue
            try:
                step_idx = int(raw_step.get("step_index") or (len(flow_steps_clean) + 1))
            except Exception:
                step_idx = len(flow_steps_clean) + 1
            flow_steps_clean.append(
                {
                    "step_index": step_idx,
                    "node_id": str(raw_step.get("node_id", "") or "").strip(),
                    "node_name": str(raw_step.get("node_name", "") or "").strip(),
                    "ui_components": raw_step.get("ui_components", []) if isinstance(raw_step.get("ui_components", []), list) else [],
                    "inferred_action": str(raw_step.get("inferred_action", "") or "").strip(),
                }
            )

        flow_branches_clean = []
        for raw_branch in flow_branches:
            if not isinstance(raw_branch, dict):
                continue
            try:
                triggered_from = int(raw_branch.get("triggered_from_step") or 0)
            except Exception:
                triggered_from = 0
            flow_branches_clean.append(
                {
                    "node_id": str(raw_branch.get("node_id", "") or "").strip(),
                    "node_name": str(raw_branch.get("node_name", "") or "").strip(),
                    "branch_type": str(raw_branch.get("branch_type", "other") or "other").strip(),
                    "triggered_from_step": triggered_from,
                }
            )

        flow_steps_detected = len(flow_steps_clean)
        effective_num_stories = max(1, flow_steps_detected + len(flow_branches_clean))
        flow_story_map = [
            {"step_index": step.get("step_index", idx + 1), "story_index": idx}
            for idx, step in enumerate(flow_steps_clean)
        ]

        step_lines = []
        for idx, step in enumerate(flow_steps_clean):
            prev_label = "Início do fluxo"
            if idx > 0:
                prev = flow_steps_clean[idx - 1]
                prev_label = f"Step {prev.get('step_index', idx)} - {prev.get('node_name', '')}"
            comps = ", ".join(step.get("ui_components", [])[:8]) or "Sem componentes explícitos"
            step_lines.append(
                f"- Step {step.get('step_index')}: node_id={step.get('node_id')}, nome='{step.get('node_name')}', "
                f"ação='{step.get('inferred_action') or 'n/a'}', componentes=[{comps}], proveniência_base='{prev_label}'"
            )

        branch_lines = []
        for branch in flow_branches_clean:
            branch_lines.append(
                f"- Branch node_id={branch.get('node_id')}, nome='{branch.get('node_name')}', "
                f"tipo={branch.get('branch_type')}, triggered_from_step={branch.get('triggered_from_step') or 'n/a'}"
            )

        flow_context_block = (
            "MODO FLOW FIGMA ACTIVO:\n"
            f"- Gerar exatamente {effective_num_stories} US(s): 1 por step e 1 por branch relevante.\n"
            "- Para cada US de step: na secção Proveniência referir o step anterior.\n"
            "- Para cada US (step/branch): na secção Mockup referir node_id Figma.\n"
            "- Para branches: tratar como US separadas de exceção/fallback.\n"
            "STEPS DETECTADOS:\n"
            f"{chr(10).join(step_lines) if step_lines else '- Sem steps válidos'}\n"
        )
        if branch_lines:
            flow_context_block += "BRANCHES DETECTADOS:\n" + "\n".join(branch_lines) + "\n"

        marker_idx = context_clean.find("FLOW_CONTEXT_JSON:")
        if marker_idx >= 0:
            context_clean = context_clean[:marker_idx].strip()

    prompt = (
        f"Gerar {effective_num_stories} User Story(s) sobre: \"{topic}\"\n\n"
        f"{policy_block}\n"
        f"{common_rules}\n"
        f"{flow_context_block}\n"
        f"EXEMPLOS REAIS (few-shot):\n{ex}\n"
        f"{style_hint}\n"
        f"CONTEXTO ADICIONAL:\n{context_clean or 'Nenhum.'}\n\n"
        "OUTPUT:\n"
        "- Seguir o formato aplicável.\n"
        "- Entregar conteúdo pronto para uso em DevOps.\n"
        "- Não incluir explicações meta nem markdown extra fora do conteúdo da(s) US(s).\n"
    )
    sys_msg = (
        "És PO Sénior MSE. Segue estritamente o template aplicável e evita invenções técnicas. "
        "Prioriza consistência com backlog Revamp e exemplos reais. "
        "HTML limpo e não escapado."
    )
    try:
        gen = await llm_simple(f"{sys_msg}\n\n{prompt}", tier="pro", max_tokens=8000)
    except Exception as e:
        logging.error("[Tools] tool_generate_user_stories failed: %s", e)
        gen = f"Erro: {e}"

    gen_clean = _unescape_html_if_needed(gen)
    validation = _validate_us_output(gen_clean)
    if validation.get("cleaned_output") and validation["cleaned_output"] != gen_clean:
        logging.info("[Tools] US output auto-cleaned: %d issues", len(validation.get("issues", [])))
        gen_clean = validation["cleaned_output"]

    quality_flags = []
    if isinstance(gen_clean, str) and not gen_clean.startswith("Erro:"):
        stories = _split_generated_stories(gen_clean)
        if not stories:
            stories = [gen_clean]
        if effective_num_stories > 1:
            for idx, story in enumerate(stories, 1):
                title, ac_html = _extract_story_title_and_ac(story)
                story_flags = _collect_quality_flags(title, ac_html)
                quality_flags.extend([f"story_{idx}_{flag}" for flag in story_flags])
        else:
            title, ac_html = _extract_story_title_and_ac(stories[0])
            quality_flags = _collect_quality_flags(title, ac_html)

    result = {
        "generated_user_stories": gen_clean,
        "based_on_examples": raw.get("samples_returned", 0) if raw else 0,
        "reference_ids": reference_ids,
        "used_writer_profile": bool(style_profile),
        "topic": topic,
        "num_requested": num_stories,
        "quality_score": validation.get("score", 0.0),
        "quality_issues": validation.get("issues", []),
        "template_version": US_TEMPLATE_VERSION,
        "quality_flags": quality_flags,
        "detail_policy_applied": policy,
        "flow_mode": flow_mode,
        "flow_steps_detected": flow_steps_detected,
    }
    if flow_mode:
        result["flow_story_map"] = flow_story_map
    return result

async def tool_query_hierarchy(
    parent_id: Optional[int] = None,
    parent_type: str = "Epic",
    child_type: str = "User Story",
    area_path: Optional[str] = None,
    title_contains: Optional[str] = None,
    parent_title_hint: Optional[str] = None,
    user_sub: str = "",
) -> dict:
    try:
        safe_parent_type = _validate_workitem_type(parent_type, "Epic")
        safe_child_type = _validate_workitem_type(child_type, "User Story")
    except ValueError as e:
        return {"error": str(e)}

    canonical_area = _canonicalize_area_path(area_path) if area_path else ""
    safe_area = _safe_wiql_literal(canonical_area, 300) if canonical_area else ""
    parent_hint = str(parent_title_hint or "").strip()
    child_title_filter = str(title_contains or "").strip()

    headers = _devops_headers()
    resolved_meta = {"attempted": False}
    safe_parent_id = None
    if parent_id:
        try:
            safe_parent_id = int(parent_id)
        except (TypeError, ValueError):
            return {"error": "parent_id inválido: deve ser inteiro positivo"}
        if safe_parent_id <= 0:
            return {"error": "parent_id inválido: deve ser inteiro positivo"}
    elif parent_hint:
        resolved_parent_id, resolved_meta = await _resolve_parent_id_by_title_hint(
            headers,
            parent_type=safe_parent_type,
            area_path=safe_area,
            title_hint=parent_hint,
        )
        if not resolved_parent_id and safe_area:
            fallback_id, fallback_meta = await _resolve_parent_id_by_title_hint(
                headers,
                parent_type=safe_parent_type,
                area_path="",
                title_hint=parent_hint,
            )
            resolved_meta["fallback_without_area_attempted"] = True
            resolved_meta["fallback_without_area_meta"] = fallback_meta
            if fallback_id:
                resolved_parent_id = fallback_id
                resolved_meta["fallback_without_area_used"] = True
        if resolved_parent_id:
            safe_parent_id = int(resolved_parent_id)
            # Neste caminho, o hint foi usado para resolver o PAI e não para filtrar o TÍTULO dos filhos.
            child_title_filter = ""
        else:
            return {
                "error": (
                    f"Não foi possível identificar {safe_parent_type} com título '{parent_hint}'. "
                    "Indica o ID do parent para resultado exato."
                ),
                "total_count": 0,
                "items_returned": 0,
                "items": [],
                "parent_id": parent_id,
                "parent_type": safe_parent_type,
                "child_type": safe_child_type,
                "title_contains": child_title_filter,
                "parent_title_hint": parent_hint,
                "_parent_resolve": resolved_meta,
            }

    if safe_parent_id:
        af = f"AND ([Target].[System.AreaPath] UNDER '{safe_area}')" if safe_area else ""
        wiql = (
            "SELECT [System.Id] FROM WorkItemLinks WHERE "
            f"([Source].[System.Id] = {safe_parent_id}) "
            "AND ([System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward') "
            f"AND ([Target].[System.WorkItemType] = '{_safe_wiql_literal(safe_child_type, 80)}') "
            f"AND ([Target].[System.TeamProject] = '{_safe_wiql_literal(DEVOPS_PROJECT, 120)}') "
            f"{af} MODE (Recursive)"
        )
    else:
        source_af = f"AND [Source].[System.AreaPath] UNDER '{safe_area}'" if safe_area else ""
        target_af = f"AND [Target].[System.AreaPath] UNDER '{safe_area}'" if safe_area else ""
        wiql = (
            "SELECT [System.Id] FROM WorkItemLinks WHERE "
            f"([Source].[System.WorkItemType] = '{_safe_wiql_literal(safe_parent_type, 80)}' "
            f"{source_af} AND [Source].[System.TeamProject] = '{_safe_wiql_literal(DEVOPS_PROJECT, 120)}') "
            "AND ([System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward') "
            f"AND ([Target].[System.WorkItemType] = '{_safe_wiql_literal(safe_child_type, 80)}') "
            f"AND ([Target].[System.TeamProject] = '{_safe_wiql_literal(DEVOPS_PROJECT, 120)}') "
            f"{target_af} "
            "MODE (Recursive)"
        )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await devops_request_with_retry(
            "POST",
            _devops_url("wit/wiql?api-version=7.1"),
            headers,
            {"query": wiql},
            timeout=60,
            client=client,
        )
        if "error" in resp:
            return resp
        rels = resp.get("workItemRelations", [])
        tids = list(set(r["target"]["id"] for r in rels if r.get("target") and r.get("rel")))
        if not tids:
            tids = [wi["id"] for wi in resp.get("workItems", [])]
        total_raw = len(tids)
        if not tids:
            return {
                "total_count": 0,
                "total_raw_count": 0,
                "items_returned": 0,
                "items": [],
                "parent_id": safe_parent_id if safe_parent_id else parent_id,
                "parent_type": safe_parent_type,
                "child_type": safe_child_type,
                "title_contains": child_title_filter,
                "parent_title_hint": parent_hint,
            }
        flds = DEVOPS_FIELDS + ["System.Parent"]
        all_det, failed = await _fetch_workitem_details(
            tids,
            flds,
            headers,
            client=client,
            timeout=60,
            retry_individual_failures=False,
        )
    items = []
    for it in all_det:
        fi = _format_wi(it)
        fi["parent_id"] = it.get("fields", {}).get("System.Parent")
        items.append(fi)
    # Filtro defensivo final: garante tipo e área pedidos, mesmo se WIQL trouxer ruído.
    filtered_out = 0
    if safe_child_type or safe_area:
        expected_type = str(safe_child_type or "").strip().lower()
        expected_area = str(safe_area or "").strip().lower()
        filtered = []
        for item in items:
            item_type = str(item.get("type", "") or "").strip().lower()
            item_area = str(item.get("area", "") or "").strip().lower()
            type_ok = not expected_type or item_type == expected_type
            area_ok = not expected_area or item_area.startswith(expected_area)
            if type_ok and area_ok:
                filtered.append(item)
            else:
                filtered_out += 1
        items = filtered
    title_filter = _normalize_match_text(child_title_filter)
    if title_filter:
        terms = [t for t in title_filter.split(" ") if t]
        if terms:
            by_title = []
            for item in items:
                title_norm = _normalize_match_text(str(item.get("title", "") or ""))
                if all(term in title_norm for term in terms):
                    by_title.append(item)
                else:
                    filtered_out += 1
            items = by_title

    if failed and not items:
        items = [{"id":fid,"type":child_type,"title":"(rate limited)","state":"","url":f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_workitems/edit/{fid}"} for fid in failed]
    matched_count = len(items)
    result = {
        "total_count": matched_count,
        "total_raw_count": total_raw,
        "items_returned": matched_count,
        "parent_id": safe_parent_id if safe_parent_id else parent_id,
        "parent_type":safe_parent_type,
        "child_type":safe_child_type,
        "title_contains": child_title_filter,
        "parent_title_hint": parent_hint,
        "items":items,
    }
    await _attach_auto_csv_export(
        result,
        title_hint=f"hierarchy_{safe_parent_type}_{safe_child_type}_{(safe_parent_id if safe_parent_id else 'all')}",
        user_sub=user_sub,
    )
    if resolved_meta.get("attempted"):
        result["_parent_resolve"] = resolved_meta
    if filtered_out:
        result["_post_filtered_out"] = filtered_out
    if failed:
        result["_partial"] = True
        result["_failed_batch_count"] = len(failed)
    return result

async def tool_compute_kpi(wiql_where: str, group_by: Optional[str] = None, kpi_type: str = "count") -> dict:
    result = await tool_query_workitems(wiql_where=wiql_where, top=1000)
    if "error" in result: return result
    items = result.get("items",[]); total = result.get("total_count",len(items))
    kpi = {"total_count": total, "items_analyzed": len(items)}
    if group_by and items:
        fm = {"state":"state","estado":"state","type":"type","tipo":"type","assigned_to":"assigned_to","assignee":"assigned_to","created_by":"created_by","criador":"created_by","autor":"created_by","area":"area","area_path":"area"}
        fk = fm.get(group_by.lower(), group_by.lower())
        grps = {}
        for it in items: v=it.get(fk,"N/A") or "N/A"; grps[v]=grps.get(v,0)+1
        kpi["group_by"]=group_by; kpi["groups"]=[{"value":k,"count":v} for k,v in sorted(grps.items(),key=lambda x:x[1],reverse=True)]; kpi["unique_values"]=len(grps)
    if kpi_type=="timeline" and items:
        m={}
        for it in items:
            d=it.get("created_date","")
            if d: mo=d[:7]; m[mo]=m.get(mo,0)+1
        kpi["timeline"]=sorted(m.items())
    if kpi_type=="distribution" and items:
        st,tp = {},{}
        for it in items: s=it.get("state","?"); st[s]=st.get(s,0)+1; t=it.get("type","?"); tp[t]=tp.get(t,0)+1
        kpi["state_distribution"]=st; kpi["type_distribution"]=tp
    return kpi


async def create_workitem_in_devops(
    work_item_type: str = "User Story",
    title: str = "",
    description: str = "",
    acceptance_criteria: str = "",
    area_path: str = "",
    assigned_to: str = "",
    tags: str = "",
):
    """Cria um Work Item no Azure DevOps via JSON Patch."""
    normalized_type = (work_item_type or "User Story").strip().lower()
    allowed_types = {
        "user story": "User Story",
        "bug": "Bug",
        "task": "Task",
        "feature": "Feature",
    }
    resolved_type = allowed_types.get(normalized_type, "User Story")

    safe_title = (title or "").strip()[:250]
    safe_description = (description or "").strip()[:12000]
    safe_acceptance_criteria = (acceptance_criteria or "").strip()[:12000]
    safe_area_path = (area_path or "").strip()[:300]
    safe_assigned_to = (assigned_to or "").strip()[:200]
    safe_tags = (tags or "").strip()[:500]

    if not safe_title:
        return {"error": "Título é obrigatório"}

    patch_doc = [
        {"op": "add", "path": "/fields/System.Title", "value": safe_title},
    ]
    if safe_description:
        patch_doc.append({"op": "add", "path": "/fields/System.Description", "value": safe_description})
    if safe_acceptance_criteria:
        patch_doc.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": safe_acceptance_criteria})
    if safe_area_path:
        patch_doc.append({"op": "add", "path": "/fields/System.AreaPath", "value": safe_area_path})
    if safe_assigned_to:
        patch_doc.append({"op": "add", "path": "/fields/System.AssignedTo", "value": safe_assigned_to})
    if safe_tags:
        patch_doc.append({"op": "add", "path": "/fields/System.Tags", "value": safe_tags})

    wi_type_encoded = quote(resolved_type, safe="")
    url = _devops_url(f"wit/workitems/${wi_type_encoded}?api-version=7.1")
    headers = _devops_headers()
    headers["Content-Type"] = "application/json-patch+json"
    data = await devops_request_with_retry(
        "POST",
        url,
        headers,
        content_body=json.dumps(patch_doc),
        max_retries=3,
        timeout=30,
    )
    if "error" in data:
        return data

    wi_id = data.get("id")
    wi_url = data.get("_links", {}).get("html", {}).get("href", "")
    if not wi_url and wi_id:
        wi_url = f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_workitems/edit/{wi_id}"

    return {
        "created": True,
        "id": wi_id,
        "url": wi_url,
        "title": safe_title,
        "work_item_type": resolved_type,
        "area_path": safe_area_path or "(default)",
    }

async def tool_create_workitem(
    work_item_type: str = "User Story",
    title: str = "",
    description: str = "",
    acceptance_criteria: str = "",
    area_path: str = "",
    assigned_to: str = "",
    tags: str = "",
    confirmed: bool = False,
    confirmation_token: str = "",
    conv_id: str = "",
    user_sub: str = "",
):
    """Cria um Work Item no Azure DevOps via JSON Patch."""
    if not confirmed:
        return {"error": "Confirmação explícita necessária antes de criar work item."}
    if not consume_create_workitem_confirmation_token(
        confirmation_token,
        conv_id=str(conv_id or "").strip(),
        user_sub=str(user_sub or "").strip(),
    ):
        return {"error": "Confirmação expirada ou inválida. Pede nova confirmação explícita do utilizador."}

    _log(f"create_workitem: type={work_item_type}, title={str(title or '')[:60]}...")
    return await create_workitem_in_devops(
        work_item_type=work_item_type,
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        area_path=area_path,
        assigned_to=assigned_to,
        tags=tags,
    )

async def tool_refine_workitem(
    work_item_id: int = 0,
    refinement_request: str = "",
):
    """Refina uma US existente com base numa instrução curta, sem alterar DevOps."""
    try:
        safe_id = int(work_item_id)
    except (TypeError, ValueError):
        return {"error": "work_item_id inválido: deve ser inteiro positivo"}
    if safe_id <= 0:
        return {"error": "work_item_id inválido: deve ser inteiro positivo"}

    req = (refinement_request or "").strip()
    if not req:
        return {"error": "refinement_request é obrigatório"}

    fields = [
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AreaPath",
        "System.Description",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "System.Tags",
    ]
    fields_param = ",".join(fields)
    headers = _devops_headers()

    wi = await devops_request_with_retry(
        "GET",
        _devops_url(f"wit/workitems/{safe_id}?fields={fields_param}&api-version=7.1"),
        headers,
        max_retries=3,
        timeout=45,
    )
    if "error" in wi:
        return wi
    if not isinstance(wi, dict) or not wi.get("id"):
        return {"error": "Work item não encontrado"}

    f = wi.get("fields", {})
    original = {
        "id": wi.get("id"),
        "title": f.get("System.Title", ""),
        "state": f.get("System.State", ""),
        "type": f.get("System.WorkItemType", ""),
        "area": f.get("System.AreaPath", ""),
        "description_html": f.get("System.Description", "") or "",
        "acceptance_criteria_html": f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "",
        "tags": f.get("System.Tags", "") or "",
        "url": f"https://dev.azure.com/{DEVOPS_ORG}/{DEVOPS_PROJECT}/_workitems/edit/{safe_id}",
    }

    prompt = f"""És PO Sénior MSE.
Recebeste uma User Story existente e um pedido de refinamento.

US ORIGINAL:
- ID: {original['id']}
- Tipo: {original['type']}
- Título: {original['title']}
- Área: {original['area']}
- Descrição HTML: {original['description_html'][:6000]}
- AC HTML: {original['acceptance_criteria_html'][:6000]}
- Tags: {original['tags']}

PEDIDO DE REFINAMENTO:
{req}

Objetivo:
- Devolver uma versão revista, mantendo estilo MSE e estrutura testável.
- Aplicar apenas as mudanças pedidas.
- PT-PT.
- HTML limpo (div, b, ul, li, br).
- Estrutura oficial de AC: Proveniência, Condições, Composição, Comportamento, Mockup.
- Preservar a estrutura original e alterar apenas secções impactadas.
- Se a US original não seguir o template oficial, NÃO reformatar; aplicar apenas o refinamento pedido.
- NÃO forçar prefixo "MSE |" no título durante refino; manter título original salvo pedido explícito para mudar.
- Em change_summary, indicar as secções alteradas.

Responde APENAS em JSON válido neste formato:
{{
  "title": "Título revisto",
  "description_html": "<div>...</div>",
  "acceptance_criteria_html": "<ul><li>...</li></ul>",
  "change_summary": "Resumo curto das alterações"
}}"""

    try:
        llm_output = await llm_simple(prompt, tier="pro", max_tokens=2600)
    except Exception as e:
        return {"error": f"Falha LLM ao refinar work item: {str(e)}"}

    parsed = _extract_json_object(llm_output or "")
    if not parsed:
        return {
            "work_item_id": safe_id,
            "work_item_url": original["url"],
            "refinement_request": req,
            "original": original,
            "ready_to_apply": False,
            "error": "Não foi possível estruturar JSON da revisão. Repetir pedido com instrução mais objetiva.",
            "refined_raw": (llm_output or "")[:12000],
            "note": "Esta tool não altera o work item no DevOps; gera apenas proposta de revisão.",
        }

    refined = {
        "title": str(parsed.get("title", "")).strip() or original["title"],
        "description_html": _unescape_html_if_needed(str(parsed.get("description_html", "")).strip()),
        "acceptance_criteria_html": _unescape_html_if_needed(str(parsed.get("acceptance_criteria_html", "")).strip()),
        "change_summary": str(parsed.get("change_summary", "")).strip(),
    }

    return {
        "work_item_id": safe_id,
        "work_item_url": original["url"],
        "refinement_request": req,
        "original": original,
        "refined": refined,
        "ready_to_apply": True,
        "note": "Esta tool não altera o work item no DevOps; gera proposta para revisão DRAFT->REVIEW->FINAL.",
    }
