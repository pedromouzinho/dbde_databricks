# =============================================================================
# tools_knowledge.py — Search, embeddings and rerank utilities
# =============================================================================

import json
import math
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

from azure_auth import build_search_auth_headers
from config_databricks import (
    SEARCH_SERVICE,
    SEARCH_KEY,
    API_VERSION_SEARCH,
    DEVOPS_INDEX,
    OMNI_INDEX,
    RERANK_ENABLED,
    RERANK_ENDPOINT,
    RERANK_API_KEY,
    RERANK_MODEL,
    RERANK_TOP_N,
    RERANK_TIMEOUT_SECONDS,
    RERANK_AUTH_MODE,
    WEB_SEARCH_ENABLED,
    WEB_SEARCH_API_KEY,
    WEB_SEARCH_ENDPOINT,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MARKET,
    WEB_ANSWERS_ENABLED,
    WEB_ANSWERS_API_KEY,
    WEB_ANSWERS_ENDPOINT,
    WEB_ANSWERS_MODEL,
    WEB_ANSWERS_TIMEOUT_SECONDS,
)
from llm_provider_databricks import get_embedding_provider, get_embedding as _provider_get_embedding
from http_helpers import _sanitize_error_response, search_request_with_retry
from pii_shield import PIIMaskingContext, _regex_pre_mask

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None
_LEGACY_INDEX_AVAILABILITY = {"devops": None, "omni": None}

if RERANK_ENABLED:
    logger.info("[RAG] Reranking enabled: model=%s, top_n=%s", RERANK_MODEL, RERANK_TOP_N)


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=max(25, int(RERANK_TIMEOUT_SECONDS or 25)))
    return _http_client


async def _close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _build_rerank_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = str(RERANK_API_KEY or "").strip()
    mode = str(RERANK_AUTH_MODE or "").strip().lower()
    if token:
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif mode == "api-key":
            headers["api-key"] = token
    return headers


async def _search_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    headers.update(await build_search_auth_headers(api_key=SEARCH_KEY, service_name=SEARCH_SERVICE))
    return headers


def _mark_legacy_index_availability(index_key: str, *, available: bool) -> None:
    if index_key in _LEGACY_INDEX_AVAILABILITY:
        _LEGACY_INDEX_AVAILABILITY[index_key] = bool(available)


def _legacy_index_known_unavailable(index_key: str) -> bool:
    return _LEGACY_INDEX_AVAILABILITY.get(index_key) is False


def _looks_like_missing_index(error_message: str) -> bool:
    text = str(error_message or "").strip().lower()
    if not text:
        return False
    return "404" in text or "not found" in text or "no such index" in text


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", folded).strip()


def _expand_identifier_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokenize(value: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", _normalize_text(_expand_identifier_text(value)))
    return {token for token in normalized.split() if len(token) >= 3}


_PRODUCT_COMPONENT_LABELS = {
    "cta": "call to action principal",
    "primary cta": "call to action principal",
    "card": "bloco resumo",
    "input": "campo de preenchimento",
    "dropdown": "lista de seleção",
    "stepper": "progressão por passos",
    "modal": "janela de confirmação",
    "tab": "separador",
    "header": "cabeçalho",
    "toast": "mensagem breve de sucesso ou erro",
    "sidebar": "navegação lateral",
    "bloco": "bloco de conteúdo",
}

_BUSINESS_LABEL_OVERRIDES = {
    "accounts": "contas",
    "beneficiary confirmation": "confirmação de beneficiário",
    "change credentials": "alteração de credenciais",
    "charges": "cobranças",
    "day to day": "dia a dia",
    "digital documents": "documentos digitais",
    "digital documents list": "lista de documentos digitais",
    "digital signature authorized operation": "assinatura digital da operação",
    "digital signature confirmation": "confirmação da assinatura digital",
    "digital signature confirmation page": "confirmação da assinatura digital",
    "digital signature documents": "documentos para assinatura digital",
    "digital signature documents page": "documentos para assinatura digital",
    "digital signature error page": "erro na assinatura digital",
    "digital signature landing page": "entrada da assinatura digital",
    "digital signature register operation": "registo da operação para assinatura",
    "digital signature register operation page": "registo da operação para assinatura",
    "documents": "documentos",
    "european funds": "fundos europeus",
    "file consult": "consulta de ficheiros",
    "file upload": "carregamento de documentos",
    "flow decision": "decisão do fluxo",
    "global position": "posição global",
    "integrated solutions authorized operation": "operação autorizada",
    "integrated solutions pending ops": "operações pendentes",
    "integrated solutions pending ops landing page": "entrada de operações pendentes",
    "integrated solutions register operation": "registo de operação pendente",
    "login": "entrada",
    "mobis pending ops": "operações pendentes",
    "movements": "movimentos",
    "on boarding": "onboarding",
    "pay salary": "pagamento salarial",
    "pay supplier": "pagamento a fornecedor",
    "receivables": "recebíveis",
    "register sibs confirm user data page": "confirmação de dados de adesão",
    "register sibs error page": "erro no processo de adesão",
    "register sibs finish process page": "conclusão do processo de adesão",
    "register sibs landing page": "entrada do processo de adesão",
    "register sibs missing requirements page": "requisitos em falta para adesão",
    "register sibs user data selection page": "seleção de dados do utilizador",
    "salary": "salários",
    "spin": "serviço SPIN",
    "spin cancellation": "cancelamento do serviço SPIN",
    "spin management": "gestão do serviço SPIN",
    "spin subscription": "adesão ao serviço SPIN",
    "spin transfer": "transferência SPIN",
    "spin transfers": "transferências SPIN",
    "state payments": "pagamentos ao Estado",
    "state transfers": "transferências ao Estado",
    "supplier": "fornecedores",
    "transfers": "transferências",
    "your enterprise": "empresa",
}

_BUSINESS_WORD_MAP = {
    "accounts": "contas",
    "authorized": "autorizada",
    "beneficiary": "beneficiário",
    "beneficiaries": "beneficiários",
    "cancellation": "cancelamento",
    "change": "alteração",
    "charges": "cobranças",
    "confirmation": "confirmação",
    "credentials": "credenciais",
    "day": "dia",
    "decision": "decisão",
    "digital": "digital",
    "documents": "documentos",
    "document": "documento",
    "enterprise": "empresa",
    "european": "europeus",
    "error": "erro",
    "file": "ficheiro",
    "finish": "conclusão",
    "flow": "fluxo",
    "funds": "fundos",
    "global": "global",
    "integrated": "integradas",
    "landing": "entrada",
    "login": "entrada",
    "management": "gestão",
    "missing": "em falta",
    "movements": "movimentos",
    "onboarding": "onboarding",
    "operation": "operação",
    "operations": "operações",
    "ops": "operações",
    "page": "",
    "payments": "pagamentos",
    "pending": "pendentes",
    "position": "posição",
    "receivables": "recebíveis",
    "register": "adesão",
    "requirements": "requisitos",
    "salary": "salário",
    "selection": "seleção",
    "service": "serviço",
    "signature": "assinatura",
    "sibs": "",
    "spin": "SPIN",
    "state": "estado",
    "subscription": "adesão",
    "supplier": "fornecedor",
    "transfers": "transferências",
    "transfer": "transferência",
    "upload": "carregamento",
    "user": "utilizador",
}

_CTA_QUERY_MARKERS = (
    "cta",
    "call to action",
    "botao",
    "botão",
    "texto do botao",
    "texto do botão",
    "acao principal",
    "ação principal",
)
_PLACEMENT_QUERY_MARKERS = (
    "dashboard",
    "pagina",
    "página",
    "home",
    "overview",
    "encaixa",
    "encaixar",
    "posicionar",
    "posicionamento",
    "placement",
    "onde colocar",
    "onde meto",
    "widget",
    "card",
    "bloco",
)
_GROWTH_QUERY_MARKERS = (
    "crescer",
    "crescimento",
    "adocao",
    "adoção",
    "conversao",
    "conversão",
    "melhorar",
    "melhoria",
    "oportunidade",
    "oportunidades",
    "engagement",
    "retencao",
    "retenção",
    "valor",
)
_FLOW_QUERY_MARKERS = (
    "como faço",
    "como funciona",
    "como e que",
    "como é que",
    "explica",
    "percurso",
    "fluxo",
    "jornada",
    "passos",
)


def _website_item_signature(item: dict) -> str:
    parts = [
        str(item.get("origin", "") or ""),
        str(item.get("id", "") or ""),
        str(item.get("url", "") or ""),
        str(item.get("title", "") or ""),
        str(item.get("tag", "") or ""),
        str(item.get("domain", "") or ""),
        str(item.get("journey", "") or ""),
        str(item.get("flow", "") or ""),
    ]
    raw = "|".join(part.strip() for part in parts if str(part or "").strip())
    return _normalize_text(raw)


def _website_item_search_text(item: dict) -> str:
    return " ".join(
        part
        for part in [
            str(item.get("title", "") or ""),
            str(item.get("content", "") or ""),
            str(item.get("business_title", "") or ""),
            str(item.get("business_summary", "") or ""),
            str(item.get("tag", "") or ""),
            str(item.get("domain", "") or ""),
            str(item.get("journey", "") or ""),
            str(item.get("flow", "") or ""),
            str(item.get("site_placement", "") or ""),
            str(item.get("routing_note", "") or ""),
            " ".join(str(item) for item in item.get("ui_components", []) or [] if item),
            " ".join(str(item) for item in item.get("ux_terms", []) or [] if item),
        ]
        if part
    )


def _businessize_label(value: str, *, domain: str = "") -> str:
    raw = str(value or "").strip()
    if not raw:
        return str(domain or "").strip()
    expanded = _expand_identifier_text(raw)
    normalized = _normalize_text(expanded)
    if normalized in _BUSINESS_LABEL_OVERRIDES:
        return _BUSINESS_LABEL_OVERRIDES[normalized]
    for key, label in sorted(_BUSINESS_LABEL_OVERRIDES.items(), key=lambda item: len(item[0]), reverse=True):
        if key and key in normalized:
            return label
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", expanded) if token]
    translated: list[str] = []
    for token in tokens:
        normalized_token = _normalize_text(token)
        mapped = _BUSINESS_WORD_MAP.get(normalized_token, token)
        if mapped:
            translated.append(mapped)
    if not translated:
        translated = [expanded]
    text = " ".join(translated)
    text = re.sub(r"\s+", " ", text).strip(" -|")
    if not text:
        text = expanded
    if text.upper() == text:
        return text
    return text[:1].upper() + text[1:]


def _businessize_components(components: list[str] | tuple[str, ...] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in components or []:
        normalized = _normalize_text(raw)
        label = _PRODUCT_COMPONENT_LABELS.get(normalized, _businessize_label(str(raw or "")))
        clean = str(label or "").strip()
        if not clean:
            continue
        signature = _normalize_text(clean)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(clean)
    return result[:6]


def _businessize_surface(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = text.replace("MSE/", "").replace("MSE\\", "").replace("MSE,", "")
    if cleaned == text and "/" not in text and ">" not in text and "," not in text:
        return text
    segments = [segment.strip(" .") for segment in re.split(r"[>/,]", cleaned) if segment.strip(" .")]
    if not segments:
        return text
    translated = [_businessize_label(segment) for segment in segments if segment]
    if len(translated) == 1:
        return f"área de {translated[0]} do portal"
    return " > ".join(translated)


def _detect_product_query_intents(query: str) -> list[str]:
    normalized = _normalize_text(query)
    intents: list[str] = []
    if any(marker in normalized for marker in _CTA_QUERY_MARKERS):
        intents.append("cta_guidance")
    if any(marker in normalized for marker in _PLACEMENT_QUERY_MARKERS):
        intents.append("placement_guidance")
    if any(marker in normalized for marker in _GROWTH_QUERY_MARKERS):
        intents.append("growth_opportunity")
    if any(marker in normalized for marker in _FLOW_QUERY_MARKERS):
        intents.append("business_flow")
    if not intents:
        intents.append("business_flow")
    return intents


def _suggest_primary_cta(query: str, *, domain: str = "", flow: str = "") -> list[str]:
    normalized_query = _normalize_text(query)
    normalized_domain = _normalize_text(domain)
    normalized_flow = _normalize_text(_expand_identifier_text(flow))
    suggestions: list[str] = []

    if "cancel" in normalized_query or "cancel" in normalized_flow:
        suggestions.append("Cancelar serviço")
    if any(token in normalized_query for token in ("assinatura", "autorizar", "autorizacao", "autorização")) or "signature" in normalized_flow:
        suggestions.extend(["Autorizar operação", "Assinar e confirmar"])
    if any(token in normalized_query for token in ("upload", "documento", "documentos", "ficheiro", "ficheiros")) or normalized_domain == "documentos":
        suggestions.extend(["Carregar documento", "Submeter documento"])
    if any(token in normalized_query for token in ("login", "acesso", "credenciais", "autenticacao", "autenticação")) or normalized_domain == "autenticacao":
        suggestions.extend(["Entrar", "Continuar"])
    if any(token in normalized_query for token in ("onboarding", "adesao", "adesão", "contas", "fundos europeus")) or normalized_domain == "onboarding":
        suggestions.extend(["Continuar adesão", "Concluir adesão"])
    if "spin" in normalized_query and normalized_domain == "recebiveis":
        suggestions.extend(["Gerir serviço", "Aderir ao serviço"])
    if "spin" in normalized_query and normalized_domain == "transferencias":
        suggestions.extend(["Continuar transferência", "Confirmar transferência"])
    if any(token in normalized_query for token in ("transferencia", "transferências", "transferencia", "pagamento", "pagamentos")) or normalized_domain in {"transferencias", "pagamentos"}:
        suggestions.extend(["Continuar", "Confirmar"])

    if not suggestions:
        suggestions = ["Continuar", "Confirmar"]
    return merge_sources(suggestions)[:3]


def _domain_growth_opportunities(domain: str, query: str) -> list[str]:
    normalized_domain = _normalize_text(domain)
    normalized_query = _normalize_text(query)
    suggestions = [
        "Mede o impacto com clique no CTA principal, taxa de início e taxa de conclusão.",
        "Expõe pré-requisitos e estado atual antes da ação para reduzir abandono.",
    ]
    if "dashboard" in normalized_query or normalized_domain == "dashboard":
        suggestions.insert(0, "Promove a ação como próxima melhor ação no dashboard, com contexto e benefício imediato.")
    elif normalized_domain in {"transferencias", "pagamentos"}:
        suggestions.insert(0, "Cria uma entrada rápida a partir da home ou do dashboard para a ação mais recorrente.")
    elif normalized_domain == "documentos":
        suggestions.insert(0, "Torna visível o estado dos documentos e o que falta fazer antes de pedir nova ação.")
    elif normalized_domain == "operacoes":
        suggestions.insert(0, "Dá prioridade a operações pendentes com urgência, prazo ou impacto visível.")
    elif normalized_domain == "recebiveis":
        suggestions.insert(0, "Explica claramente o estado do serviço e oferece a ação certa para adesão, gestão ou cancelamento.")
    elif normalized_domain == "onboarding":
        suggestions.insert(0, "Mostra progresso e próximos passos para reduzir desistência a meio da jornada.")
    return merge_sources(suggestions)[:3]


def _sanitize_business_note(note: str) -> str:
    text = str(note or "").strip()
    normalized = _normalize_text(text)
    if not text:
        return ""
    if "github main" in normalized or "source of truth" in normalized or "repo atlas" in normalized:
        return ""
    if normalized.startswith("preferir este handoff"):
        return "Há evidência mais forte para este contexto funcional do que para alternativas genéricas."
    return text


def _build_business_title(item: dict) -> str:
    domain = str(item.get("domain", "") or "").strip()
    flow = str(item.get("flow", "") or "").strip()
    journey = str(item.get("journey", "") or "").strip()
    seed = flow or journey or str(item.get("title", "") or "").strip()
    label = _businessize_label(seed, domain=domain)
    if domain and _normalize_text(label) != _normalize_text(domain):
        return f"{label} | {domain}"
    return label or domain or str(item.get("title", "") or "").strip()


def _build_business_summary(item: dict) -> str:
    domain = str(item.get("domain", "") or "").strip()
    flow_label = _businessize_label(str(item.get("flow", "") or item.get("journey", "") or ""), domain=domain)
    placement = _businessize_surface(str(item.get("site_placement", "") or "")).rstrip(". ")
    components = _businessize_components(item.get("ui_components", []) or item.get("ux_terms", []) or [])
    note = _sanitize_business_note(str(item.get("routing_note", "") or ""))
    parts = []
    if domain:
        parts.append(f"Aponta para a área de {domain.lower()} do portal.")
    if flow_label and _normalize_text(flow_label) != _normalize_text(domain):
        parts.append(f"O indício mais forte é um percurso ligado a {flow_label.lower()}.")
    if placement:
        parts.append(f"Este tema surge associado a {placement}.")
    if components:
        parts.append(f"Os padrões de interface mais recorrentes aqui são {', '.join(components[:4])}.")
    if note:
        parts.append(note)
    return " ".join(part for part in parts if part).strip()


def _build_product_brief(query: str, items: list[dict], local_story: Optional[dict] = None) -> dict:
    intents = _detect_product_query_intents(query)
    dominant_domain = str(
        (local_story or {}).get("dominant_domain", "")
        or ((items[0] if items else {}).get("domain", "") if items else "")
        or ""
    ).strip()
    lead_item = items[0] if items else {}
    lead_flow_item = next(
        (
            item
            for item in items
            if str(item.get("flow", "") or "").strip()
            and (
                not dominant_domain
                or _normalize_text(str(item.get("domain", "") or "")) == _normalize_text(dominant_domain)
            )
        ),
        lead_item,
    )
    placement = _businessize_surface(str(lead_item.get("site_placement", "") or "")).rstrip(". ")
    profile_components = list((((local_story or {}).get("profile", {}) or {}).get("preferred_lexicon", []) or []))
    ui_patterns = merge_sources(
        _businessize_components((lead_item.get("ui_components", []) or []))
        + _businessize_components(profile_components)
    )[:6]
    flow_label = _businessize_label(
        str(lead_flow_item.get("flow", "") or lead_flow_item.get("journey", "") or ""),
        domain=dominant_domain,
    )
    summary_parts = []
    if dominant_domain:
        summary_parts.append(f"A pergunta cai sobretudo em {dominant_domain}.")
    if flow_label and _normalize_text(flow_label) != _normalize_text(dominant_domain):
        summary_parts.append(f"A melhor evidência aponta para um percurso de {flow_label.lower()}.")
    if placement:
        summary_parts.append(f"Este tipo de ação aparece associado a {placement}.")
    response_shape = [
        "Responder em linguagem de negócio.",
        "Evitar nomes de repos, camelCase e labels técnicas.",
    ]
    if "cta_guidance" in intents:
        response_shape.append("Propor CTA claro, orientado ao resultado e coerente com o momento da jornada.")
    if "placement_guidance" in intents:
        response_shape.append("Explicar onde a ação encaixa na página e porquê.")
    if "growth_opportunity" in intents:
        response_shape.append("Sugerir oportunidades de crescimento, adoção ou descoberta.")

    cta_guidance: list[str] = []
    if "cta_guidance" in intents:
        suggestions = _suggest_primary_cta(query, domain=dominant_domain, flow=str(lead_flow_item.get("flow", "") or ""))
        if suggestions:
            cta_guidance.append(f"Usa um CTA principal orientado ao resultado, por exemplo: {', '.join(suggestions[:2])}.")
        cta_guidance.append("Reserva 'Confirmar' para o momento de compromisso final; nos passos intermédios prefere 'Continuar'.")
        cta_guidance.append("Mantém o CTA principal no mesmo bloco onde o utilizador revê os dados ou pré-requisitos.")

    placement_guidance: list[str] = []
    if "placement_guidance" in intents:
        if "dashboard" in _normalize_text(query):
            placement_guidance.append("No dashboard, trata isto como próxima melhor ação: um card acionável com contexto, estado e benefício imediato.")
            placement_guidance.append("Coloca-o acima da dobra ou na zona de resumo operacional quando a ação for frequente ou urgente.")
        else:
            placement_guidance.append("Coloca a ação no ponto da jornada em que o utilizador toma a decisão, não escondida em navegação secundária.")
        placement_guidance.append("Se houver pré-requisitos, mostra estado, pendências e CTA no mesmo módulo para reduzir fricção.")
        placement_guidance.append("Em páginas longas, repete o CTA no fecho apenas se o utilizador tiver de percorrer muito conteúdo.")

    growth_opportunities: list[str] = []
    if "growth_opportunity" in intents or "placement_guidance" in intents or "cta_guidance" in intents:
        growth_opportunities = _domain_growth_opportunities(dominant_domain, query)

    domains_seen = merge_sources([str(item.get("domain", "") or "") for item in items if item.get("domain")])
    ambiguity_note = ""
    if "spin" in _normalize_text(query) and len(domains_seen) > 1:
        ambiguity_note = "SPIN surge em mais do que um contexto de negócio; convém distinguir entre envio de dinheiro e gestão do serviço antes de fechar a recomendação."

    evidence = merge_sources(
        [
            str(lead_item.get("business_summary", "") or ""),
            _sanitize_business_note(str(lead_item.get("routing_note", "") or "")),
            _sanitize_business_note(
                str((local_story or {}).get("notes", [""])[0] or "") if isinstance((local_story or {}).get("notes"), list) else ""
            ),
        ]
    )[:3]

    return {
        "audience": "product_management",
        "response_mode": "business_first",
        "dominant_domain": dominant_domain,
        "intents": intents,
        "business_summary": " ".join(part for part in summary_parts if part).strip(),
        "ui_patterns": ui_patterns,
        "cta_guidance": cta_guidance[:3],
        "placement_guidance": placement_guidance[:3],
        "growth_opportunities": growth_opportunities[:3],
        "ambiguity_note": ambiguity_note,
        "response_shape": response_shape,
        "evidence": evidence,
    }


def _enrich_website_item_for_business(item: dict) -> dict:
    enriched = dict(item or {})
    business_title = _build_business_title(enriched)
    business_summary = _build_business_summary(enriched)
    if business_title:
        enriched["business_title"] = business_title
    if business_summary:
        enriched["business_summary"] = business_summary
    site_placement = str(enriched.get("site_placement", "") or "").strip()
    if site_placement:
        enriched["site_placement_business"] = _businessize_surface(site_placement)
    ui_patterns = _businessize_components(enriched.get("ui_components", []) or enriched.get("ux_terms", []) or [])
    if ui_patterns:
        enriched["ui_patterns"] = ui_patterns
    return enriched


def _with_business_website_context(query: str, payload: dict, local_story: Optional[dict] = None) -> dict:
    if not isinstance(payload, dict):
        return payload
    items = list(payload.get("items", []) or [])
    enriched_items = [_enrich_website_item_for_business(item) for item in items]
    result = dict(payload)
    result["items"] = enriched_items
    result["_product_brief"] = _build_product_brief(query, enriched_items, local_story=local_story)
    return result


def _rank_website_items(query: str, items: list[dict]) -> list[dict]:
    query_tokens = _tokenize(query)
    ranked: list[dict] = []
    for item in items:
        base_score = float(item.get("score", 0.0) or 0.0)
        search_tokens = _tokenize(_website_item_search_text(item))
        overlap = (len(query_tokens & search_tokens) / max(1, len(query_tokens))) if query_tokens and search_tokens else 0.0
        origin = str(item.get("origin", "") or "")
        source_bias = 0.0
        if origin == "local_story_context":
            source_bias += 0.08
        elif origin == "azure_ai_search_story_knowledge":
            source_bias += 0.04
        if str(item.get("flow", "") or "").strip():
            source_bias += 0.04
        elif str(item.get("journey", "") or "").strip():
            source_bias += 0.02
        hybrid_score = round(base_score + (overlap * 0.45) + source_bias, 4)
        ranked.append({**item, "hybrid_score": hybrid_score, "query_overlap": round(overlap, 4)})
    ranked.sort(
        key=lambda item: (
            float(item.get("hybrid_score", 0.0) or 0.0),
            float(item.get("score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    return ranked


def _dedupe_website_items(items: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for item in items:
        signature = _website_item_signature(item)
        if not signature:
            continue
        current = deduped.get(signature)
        current_score = float(current.get("hybrid_score", current.get("score", 0.0)) or 0.0) if current else -1.0
        candidate_score = float(item.get("hybrid_score", item.get("score", 0.0)) or 0.0)
        if current is None or candidate_score > current_score:
            deduped[signature] = item
    return list(deduped.values())


def _profile_to_website_item(profile: dict) -> dict:
    domain = str(profile.get("domain", "") or "").strip()
    journeys = [str(item) for item in profile.get("top_journeys", []) if item]
    flows = [str(item) for item in profile.get("top_flows", []) if item]
    routing_notes = [str(item) for item in profile.get("routing_notes", []) if item]
    preferred_lexicon = [str(item) for item in profile.get("preferred_lexicon", []) if item]
    content = " ".join(
        part
        for part in [
            f"Domínio {domain}." if domain else "",
            f"Journeys: {', '.join(journeys[:4])}." if journeys else "",
            f"Flows: {', '.join(flows[:6])}." if flows else "",
            " ".join(routing_notes[:3]),
        ]
        if part
    ).strip()
    base_score = max(
        float(profile.get("score", 0.0) or 0.0),
        0.45 + min(0.2, float(profile.get("production_confidence", 0.0) or 0.0) * 0.2) + min(0.15, float(profile.get("coverage_score", 0.0) or 0.0) * 0.15),
    )
    return {
        "id": f"profile:{_normalize_text(domain).replace(' ', '_')}",
        "title": f"Domain Profile | {domain}" if domain else "Domain Profile",
        "content": content[:500],
        "url": str(profile.get("design_file_url", "") or ""),
        "tag": "Story domain profile",
        "score": round(base_score, 4),
        "origin": "local_story_context",
        "domain": domain,
        "journey": journeys[0] if journeys else "",
        "flow": "",
        "routing_note": routing_notes[0] if routing_notes else "",
        "ui_components": preferred_lexicon[:6],
        "ux_terms": preferred_lexicon[:6],
    }


def _serialize_local_story_context(query: str, top: int) -> dict:
    try:
        from figma_story_map import search_story_design_map, serialize_design_match
        from story_domain_profiles import select_story_domain_profile
        from story_flow_map import search_story_flow_map, serialize_story_flow_match
        from story_policy_packs import select_story_policy_pack
    except Exception as exc:
        logger.warning("[Tools] local story context unavailable: %s", exc)
        return {"items": [], "dominant_domain": "", "sources": []}

    expanded_query = _expanded_story_query_context(query)

    flow_result = search_story_flow_map(
        objective=query,
        context=expanded_query,
        dominant_domain="",
        top=max(1, min(int(top or 3), 4)),
    )
    dominant_domain = str(flow_result.get("dominant_domain", "") or "").strip()

    profile = select_story_domain_profile(
        objective=query,
        context=expanded_query,
        dominant_domain=dominant_domain,
    )
    if not dominant_domain:
        dominant_domain = str(profile.get("domain", "") or "").strip()

    pack = select_story_policy_pack(
        objective=query,
        context=expanded_query,
        dominant_domain=dominant_domain,
    )
    if not dominant_domain:
        dominant_domain = str(pack.get("domain", "") or "").strip()

    design_result = search_story_design_map(
        objective=query,
        context=expanded_query,
        top=min(2, max(1, int(top or 3))),
    )
    if not dominant_domain:
        dominant_domain = str(design_result.get("dominant_domain", "") or "").strip()

    items: list[dict] = []
    collected_ui_terms: list[str] = []
    collected_notes: list[str] = []
    for entry in flow_result.get("matches", [])[: max(1, min(int(top or 3), 4))]:
        serialized = serialize_story_flow_match(entry)
        site_placement = str(entry.get("site_placement", "") or "")
        ui_components = list(serialized.get("ui_components", []) or entry.get("ui_components", []) or [])
        ux_terms = list(entry.get("ux_terms", []) or [])
        routing_note = str(entry.get("routing_note", "") or "")
        collected_ui_terms.extend(str(term) for term in ux_terms if term)
        if routing_note:
            collected_notes.append(routing_note)
        items.append(
            {
                "id": str(serialized.get("key", "") or entry.get("id", "") or ""),
                "title": str(serialized.get("title", "") or entry.get("title", "") or ""),
                "content": str(serialized.get("snippet", "") or entry.get("detail", "") or "")[:500],
                "url": str(serialized.get("url", "") or entry.get("url", "") or ""),
                "tag": "Story flow map",
                "score": round(float(serialized.get("score", entry.get("score", 0.0)) or 0.0), 4),
                "origin": "local_story_context",
                "domain": str(serialized.get("domain", "") or entry.get("domain", "") or ""),
                "journey": str(serialized.get("page_name", "") or entry.get("journey", "") or ""),
                "flow": str(serialized.get("frame_name", "") or entry.get("flow", "") or ""),
                "site_placement": site_placement,
                "routing_note": routing_note,
                "ui_components": ui_components[:6],
                "ux_terms": ux_terms[:8],
            }
        )

    for entry in design_result.get("matches", [])[:2]:
        serialized = serialize_design_match(entry)
        journeys = list(entry.get("journeys", []) or [])
        ux_terms = list(entry.get("ux_terms", []) or [])
        routing_note = str(entry.get("routing_note", "") or "")
        site_placement = str(entry.get("site_placement", "") or "")
        collected_ui_terms.extend(str(term) for term in ux_terms if term)
        if routing_note:
            collected_notes.append(routing_note)
        items.append(
            {
                "id": str(serialized.get("key", "") or entry.get("file_key", "") or ""),
                "title": str(serialized.get("title", "") or entry.get("title", "") or ""),
                "content": str(serialized.get("snippet", "") or entry.get("routing_note", "") or "")[:500],
                "url": str(serialized.get("url", "") or entry.get("url", "") or ""),
                "tag": "Figma handoff",
                "score": round(float(serialized.get("score", entry.get("score", 0.0)) or 0.0), 4),
                "origin": "local_story_context",
                "domain": str(serialized.get("domain", "") or entry.get("domain", "") or ""),
                "journey": str((journeys or [""])[0] or ""),
                "flow": str((journeys or [""])[-1] or ""),
                "site_placement": site_placement,
                "routing_note": routing_note,
                "ui_components": ux_terms[:6],
                "ux_terms": ux_terms[:8],
            }
        )

    if profile:
        collected_ui_terms.extend(str(term) for term in profile.get("preferred_lexicon", []) or [] if term)
        collected_notes.extend(str(note) for note in profile.get("routing_notes", []) or [] if note)
        items.append(_profile_to_website_item(profile))

    deduped = _dedupe_website_items(_rank_website_items(query, items))
    deduped.sort(
        key=lambda item: (
            float(item.get("hybrid_score", item.get("score", 0.0)) or 0.0),
            float(item.get("score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    return {
        "items": deduped[: max(1, min(int(top or 3), 4))],
        "dominant_domain": dominant_domain,
        "sources": merge_sources(["design_map" if design_result.get("matches") else "", "flow_map" if flow_result.get("matches") else "", "domain_profile" if profile else "", "policy_pack" if pack else ""]),
        "ux_terms": merge_sources(collected_ui_terms),
        "notes": merge_sources(collected_notes + list(flow_result.get("notes", []) or []) + list(design_result.get("notes", []) or [])),
        "profile": profile if isinstance(profile, dict) else {},
        "policy_pack": pack if isinstance(pack, dict) else {},
    }


def merge_sources(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _expanded_story_query_context(query: str) -> str:
    normalized = _normalize_text(query)
    hints: list[str] = []
    if "assinatura digital" in normalized or ("operacao" in normalized and "pendente" in normalized):
        hints.append("Fluxo de operações pendentes e assinatura digital.")
    if any(token in normalized for token in ("credenciais", "login", "acesso", "autenticacao")):
        hints.append("Fluxo de autenticação, login e gestão de credenciais.")
    if any(token in normalized for token in ("documento", "documentos", "upload", "ficheiro", "ficheiros")):
        hints.append("Fluxo documental e documentos digitais com upload e consulta.")
    if any(token in normalized for token in ("recebiveis", "recebíveis", "spin", "cobrancas", "cobranças")):
        hints.append("Fluxo de recebíveis, gestão SPIN, subscrição e cancelamento.")
    if any(token in normalized for token in ("onboarding", "fundos europeus", "contas", "posicao global", "posição global")):
        hints.append("Fluxo de onboarding com contas, dia a dia, posição global e fundos europeus.")
    if any(token in normalized for token in ("beneficiario", "beneficiários", "beneficiarios")):
        hints.append("Fluxo de beneficiários com criação, edição e importação.")
    if any(token in normalized for token in _CTA_QUERY_MARKERS):
        hints.append("Intenção de UX: copy do CTA principal, hierarquia de ação, próxima melhor ação e momento de confirmação.")
    if any(token in normalized for token in _PLACEMENT_QUERY_MARKERS):
        hints.append("Intenção de produto/UX: placement em página, dashboard, card, bloco resumo, entrada rápida e priorização visual.")
    if any(token in normalized for token in _GROWTH_QUERY_MARKERS):
        hints.append("Intenção de crescimento: adoção, conversão, descoberta, engagement e oportunidades de melhoria da jornada.")
    if not hints:
        return query
    return " | ".join([str(query or "").strip()] + hints)


async def _fallback_story_devops_search(query: str, top: int, *, reason: str, filter_expr: str | None = None) -> Optional[dict]:
    try:
        from story_devops_index import search_story_devops_index

        result = await search_story_devops_index(query_text=query, top=top)
    except Exception as exc:
        logger.warning("[Tools] story devops fallback failed: %s", exc)
        return None

    items = []
    for item in list(result.get("items", []) or [])[: max(1, int(top or 30))]:
        items.append(
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "content": item.get("content", "")[:500],
                "status": item.get("state", ""),
                "url": item.get("url", ""),
                "score": round(float(item.get("score", 0.0) or 0.0), 4),
                "type": item.get("type", ""),
                "area": item.get("area", ""),
                "origin": item.get("origin", "azure_ai_search_story_devops"),
            }
        )

    if not items:
        return None

    fallback_meta = {
        "reason": reason,
        "source": result.get("source", "azure_ai_search_story_devops"),
    }
    if filter_expr:
        fallback_meta["filter_expr_ignored"] = True

    return {
        "total_results": int(result.get("total_results", len(items)) or len(items)),
        "items": items,
        "_fallback": fallback_meta,
    }


async def _fallback_story_knowledge_search(query: str, top: int, *, reason: str, dominant_domain: str = "") -> Optional[dict]:
    return await _fallback_story_knowledge_search_with_local(
        query,
        top,
        reason=reason,
        dominant_domain=dominant_domain,
        local_story=None,
    )


def _story_knowledge_item_to_website_item(item: dict) -> dict:
    return {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "content": str(item.get("content", "") or "")[:500],
        "url": item.get("url", ""),
        "tag": item.get("tag", ""),
        "score": round(float(item.get("score", 0.0) or 0.0), 4),
        "origin": item.get("origin", "azure_ai_search_story_knowledge"),
        "domain": item.get("domain", ""),
        "journey": item.get("journey", ""),
        "flow": item.get("flow", ""),
    }


async def _fallback_story_knowledge_search_with_local(
    query: str,
    top: int,
    *,
    reason: str,
    dominant_domain: str = "",
    local_story: Optional[dict] = None,
) -> Optional[dict]:
    try:
        from story_knowledge_index import search_story_knowledge_index

        result = await search_story_knowledge_index(query_text=query, dominant_domain=dominant_domain, top=top)
    except Exception as exc:
        logger.warning("[Tools] story knowledge fallback failed: %s", exc)
        return None

    story_items = [
        _story_knowledge_item_to_website_item(item)
        for item in list(result.get("items", []) or [])[: max(1, int(top or 10))]
    ]
    local_items = list((local_story or {}).get("items", []) or [])[: max(1, int(top or 10))]
    merged = _dedupe_website_items(_rank_website_items(query, story_items + local_items))
    merged.sort(
        key=lambda item: (
            float(item.get("hybrid_score", item.get("score", 0.0)) or 0.0),
            float(item.get("score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    if not merged:
        return None

    fallback_source = result.get("source", "azure_ai_search_story_knowledge") if story_items else "local_story_context"

    response = {
        "total_results": max(int(result.get("total_results", len(story_items)) or len(story_items)), len(merged)),
        "items": merged[: max(1, int(top or 10))],
        "_fallback": {
            "reason": reason,
            "source": fallback_source,
            "dominant_domain": dominant_domain,
        },
    }
    if story_items and local_items:
        response["_hybrid"] = {
            "story_knowledge_items": len(story_items),
            "local_story_items": len(local_items),
            "dominant_domain": dominant_domain,
            "local_sources": list((local_story or {}).get("sources", []) or []),
        }
    return response

def _rerank_document_from_item(item: dict) -> str:
    parts = []
    for key in ("title", "content", "tag", "status", "type", "state", "area"):
        val = str((item or {}).get(key, "") or "").strip()
        if val:
            parts.append(val)
    return "\n".join(parts)[:8000]

async def _rerank_items_post_retrieval(query: str, items: list) -> tuple[list, dict]:
    if not isinstance(items, list):
        return items, {"applied": False, "reason": "invalid_items"}
    if len(items) < 2:
        return items, {"applied": False, "reason": "too_few_items"}
    if not RERANK_ENABLED:
        return items, {"applied": False, "reason": "disabled"}
    if not RERANK_ENDPOINT:
        return items, {"applied": False, "reason": "missing_endpoint"}
    if str(RERANK_AUTH_MODE or "").strip().lower() in ("api-key", "bearer") and not RERANK_API_KEY:
        return items, {"applied": False, "reason": "missing_api_key"}

    top_n = max(1, min(int(RERANK_TOP_N or len(items)), len(items)))
    documents = [_rerank_document_from_item(item) for item in items]
    payload = {
        "model": RERANK_MODEL,
        "query": str(query or "")[:2000],
        "documents": documents,
        "top_n": top_n,
    }
    headers = _build_rerank_headers()

    try:
        client = _get_http_client()
        resp = await client.post(RERANK_ENDPOINT, headers=headers, json=payload)
        if resp.status_code >= 400:
            logging.warning(
                "[Tools] rerank HTTP %s: %s",
                resp.status_code,
                _sanitize_error_response(resp.text, 300),
            )
            return items, {"applied": False, "reason": f"http_{resp.status_code}"}

        data = resp.json()
    except Exception as e:
        logging.warning("[Tools] rerank request failed: %s", e)
        return items, {"applied": False, "reason": "request_failed"}

    ranked_rows = data.get("results")
    if not isinstance(ranked_rows, list):
        ranked_rows = data.get("data")
    if not isinstance(ranked_rows, list):
        return items, {"applied": False, "reason": "invalid_response"}

    ranked_items = []
    used_indexes = set()
    for row in ranked_rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int):
            continue
        if idx < 0 or idx >= len(items):
            continue
        if idx in used_indexes:
            continue
        cloned = dict(items[idx])
        score = row.get("relevance_score", row.get("score"))
        try:
            if score is not None:
                cloned["rerank_score"] = round(float(score), 6)
        except Exception:
            pass
        ranked_items.append(cloned)
        used_indexes.add(idx)

    if not ranked_items:
        return items, {"applied": False, "reason": "empty_results"}

    for idx, item in enumerate(items):
        if idx not in used_indexes:
            ranked_items.append(item)

    return ranked_items, {
        "applied": True,
        "model": RERANK_MODEL,
        "input_count": len(items),
        "ranked_count": len(ranked_rows),
        "top_n": top_n,
    }

async def get_embedding(text):
    try:
        return await _provider_get_embedding(text[:8000].strip() or " ")
    except Exception as e:
        logging.error("[Tools] get_embedding failed: %s", e)
        return None

def _cosine_similarity(vec_a, vec_b):
    if not isinstance(vec_a, list) or not isinstance(vec_b, list):
        return -1.0
    if not vec_a or not vec_b:
        return -1.0
    size = min(len(vec_a), len(vec_b))
    if size <= 0:
        return -1.0
    a = vec_a[:size]
    b = vec_b[:size]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return dot / (norm_a * norm_b)

# =============================================================================
# DevOps semantic index (Lakebase / pgvector cosine) — replaces Azure Search.
# One JSON blob holds {built_at, count, items:[{id,title,text,...,embedding}]}.
# reindex_devops() builds it (run as a Databricks notebook/job); search loads it
# and ranks by cosine. Lazy-builds on first search so it is never simply "empty".
# =============================================================================
_DEVOPS_INDEX_CONTAINER = "knowledge"
_DEVOPS_INDEX_BLOB = "devops_index.json"
_devops_index_cache = {"data": None, "loaded_at": 0.0}

# Accessible DevOps area paths (validated against live DevOps). Single source of
# truth — imported by the startup reindex and the admin reindex endpoint.
_DEVOPS_AREA_PATHS = [
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\RevampFEE MVP2",
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\MSE",
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\MDSE",
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\CDEmpresa",
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\IZIBIZI",
    r"IT.DIT\DIT\ADMChannels\DBKS\AM24\OnbordingMoove",
]


def _wi_index_text(item: dict) -> str:
    """Embeddable text for a work item: title + description + AC + tags, HTML stripped."""
    parts = [item.get("title", ""), item.get("description", ""),
             item.get("acceptance_criteria", ""), item.get("tags", "")]
    text = "\n".join(str(p) for p in parts if p)
    text = re.sub(r"<[^>]+>", " ", text)          # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:6000]


async def reindex_devops(area_path="", top: int = 1000) -> dict:
    """Build/refresh the DevOps work-item semantic index in Lakebase.

    `area_path` may be a single path or a LIST of paths (work items are pulled
    per area and de-duplicated by id). Embeds title+description+acceptance
    criteria into one JSON blob. Run as a Databricks notebook/job (and schedule
    it) to keep search fresh:

        from storage_databricks import init_pool
        from tools_knowledge import reindex_devops
        await init_pool()
        await reindex_devops(area_path=[
            "ADM.Channels.DBKS.AM24.RevampFEE-MVP2",
            "ADM.Channels.DBKS.AM24.MSE",
            "ADM.Channels.DBKS.AM24.MDSE",
            "ADM.Channels.DBKS.AM24.CDEmpresa",
            "ADM.Channels.DBKS.AM24.IZIBIZI",
            "ADM.Channels.DBKS.AM24.OnbordingMoove",
        ])

    Note: `UNDER` matches against the real System.AreaPath. If an area returns
    count 0, try the backslash form (e.g. 'ADM\\\\Channels\\\\...') or the full
    path including the project — validate first with query_workitems.
    """
    from tools_devops import tool_query_workitems
    from config_databricks import DEVOPS_FIELDS
    areas = area_path if isinstance(area_path, (list, tuple)) else [area_path]
    areas = [str(a).strip() for a in areas if str(a).strip()] or [""]  # [""] = broad default
    fields = list(DEVOPS_FIELDS) + ["System.Description",
                                    "Microsoft.VSTS.Common.AcceptanceCriteria", "System.Tags"]
    seen: dict = {}
    errors = []
    total_found = 0
    for area in areas:
        where = (f"[System.AreaPath] UNDER '{area}'" if area
                 else "[System.ChangedDate] >= @today - 3650")
        res = await tool_query_workitems(wiql_where=where, fields=fields, top=top)
        if "error" in res:
            errors.append({"area": area, "error": res["error"]})
            continue
        total_found += res.get("total_count", 0)
        for it in res.get("items", []):
            wid = it.get("id")
            if wid in seen:
                continue
            text = _wi_index_text(it)
            if not text:
                continue
            emb = await get_embedding(text)
            if not emb:
                continue
            seen[wid] = {
                "id": wid, "title": it.get("title", ""), "text": text[:1500],
                "state": it.get("state", ""), "type": it.get("type", ""),
                "area": it.get("area", ""), "url": it.get("url", ""), "embedding": emb,
            }
    records = list(seen.values())
    if not records:
        return {"indexed": False, "count": 0, "areas": areas,
                "error": (errors[0]["error"] if errors else "0 work items found"),
                **({"errors": errors} if errors else {})}
    payload = {"built_at": datetime.now(timezone.utc).isoformat(),
               "count": len(records), "areas": areas, "items": records}
    try:
        from storage_databricks import blob_upload_json
        await blob_upload_json(_DEVOPS_INDEX_CONTAINER, _DEVOPS_INDEX_BLOB, payload)
    except Exception as e:
        return {"indexed": False, "error": f"store failed: {str(e)[:200]}", "count": len(records)}
    payload["_from_lakebase"] = True  # just persisted to Lakebase; keep status accurate
    _devops_index_cache["data"] = payload
    _devops_index_cache["loaded_at"] = time.time()
    out = {"indexed": True, "count": len(records), "areas": areas, "total_found": total_found}
    if errors:
        out["errors"] = errors
    return out


async def _load_devops_index(max_age_s: float = 300.0):
    """Return the cached index if fresh, else load it from Lakebase or a local file.

    Lakebase is the source of truth when reachable (e.g. reindex run inside the
    app runtime). A notebook-built index can't write to Lakebase, so we also fall
    back to a local devops_index.json bundled in the deploy snapshot.
    """
    now = time.time()
    cached = _devops_index_cache.get("data")
    if cached and (now - _devops_index_cache.get("loaded_at", 0.0)) < max_age_s:
        return cached
    data = None
    from_lakebase = False
    # 1) Lakebase (works in the app runtime; None from notebooks without the binding)
    try:
        from storage_databricks import blob_download_json
        data = await blob_download_json(_DEVOPS_INDEX_CONTAINER, _DEVOPS_INDEX_BLOB)
        from_lakebase = bool(data)
    except Exception:
        data = None
    # 2) Local file bundled in the deploy snapshot (notebook-built index fallback)
    if not data:
        try:
            import pathlib
            local_path = pathlib.Path(__file__).parent / "devops_index.json"
            if local_path.exists():
                with open(local_path, "r") as f:
                    data = json.load(f)
                logger.info("[Search] Loaded devops index from local file (%d items)", data.get("count", 0))
        except Exception:
            data = None
    if data:
        data["_from_lakebase"] = from_lakebase
        _devops_index_cache["data"] = data
        _devops_index_cache["loaded_at"] = now
    return data


async def tool_search_workitems(query, top=30, filter_expr=None):
    """Semantic search over DevOps work items using the Lakebase index.

    Embeds the query and ranks indexed items by cosine similarity. If the index
    is missing/empty it lazily builds it once (best effort). filter_expr, when
    given, is treated as an area substring filter.
    """
    q = str(query or "").strip()
    if not q:
        return {"error": "query vazia"}
    emb = await get_embedding(q)
    if not emb:
        return {"error": "Falha embedding"}

    index = await _load_devops_index()
    if not index or not index.get("items"):
        try:
            built = await reindex_devops()
        except Exception as e:
            built = {"indexed": False, "error": str(e)[:200]}
        index = await _load_devops_index(max_age_s=1e9) if built.get("indexed") else None
        if not index or not index.get("items"):
            return {"total_results": 0, "items": [],
                    "_index": {"status": "empty", "hint": "corre reindex_devops() num notebook",
                               **({"error": built.get("error")} if built.get("error") else {})}}

    area_filter = str(filter_expr or "").strip().lower()
    scored = []
    for rec in index.get("items", []):
        e = rec.get("embedding")
        if not e:
            continue
        if area_filter and area_filter not in str(rec.get("area", "")).lower():
            continue
        scored.append((_cosine_similarity(emb, e), rec))
    scored.sort(key=lambda x: x[0], reverse=True)

    items = []
    for score, rec in scored[: max(1, int(top or 30))]:
        items.append({
            "id": rec.get("id", ""), "title": rec.get("title", ""),
            "content": str(rec.get("text", ""))[:500], "status": rec.get("state", ""),
            "state": rec.get("state", ""), "type": rec.get("type", ""),
            "area": rec.get("area", ""), "url": rec.get("url", ""),
            "score": round(float(score), 4),
        })
    try:
        items, rerank_meta = await _rerank_items_post_retrieval(q, items)
    except Exception:
        rerank_meta = {"applied": False}
    result = {"total_results": len(items), "items": items,
              "_index": {"built_at": index.get("built_at"), "size": index.get("count")}}
    if rerank_meta.get("applied"):
        result["_rerank"] = rerank_meta
    return result

async def tool_search_website(query, top=10):
    local_story = _serialize_local_story_context(query, top=max(1, min(int(top or 10), 4)))
    dominant_domain = str(local_story.get("dominant_domain", "") or "").strip()
    emb = await get_embedding(query)
    if _legacy_index_known_unavailable("omni"):
        fallback = await _fallback_story_knowledge_search_with_local(
            query,
            top,
            reason=f"legacy_index_unavailable:{OMNI_INDEX}",
            dominant_domain=dominant_domain,
            local_story=local_story,
        )
        if fallback:
            return _with_business_website_context(query, fallback, local_story=local_story)
        local_items = list(local_story.get("items", []) or [])[: max(1, int(top or 10))]
        if local_items:
            return _with_business_website_context(query, {
                "total_results": len(local_items),
                "items": local_items,
                "_fallback": {
                    "reason": f"legacy_index_unavailable:{OMNI_INDEX}",
                    "source": "local_story_context",
                    "dominant_domain": dominant_domain,
                },
            }, local_story=local_story)
    body = {"select":"id,content,url,tag","top":top, "search": str(query or "").strip() or "*"}
    if emb:
        body["vectorQueries"] = [{"kind":"vector","vector":emb,"fields":"content_vector","k":top}]
    url = f"https://{SEARCH_SERVICE}.search.windows.net/indexes/{OMNI_INDEX}/docs/search?api-version={API_VERSION_SEARCH}"
    data = await search_request_with_retry(
        url=url,
        headers=await _search_headers(),
        json_body=body,
        max_retries=3,
    )
    if "error" in data:
        if _looks_like_missing_index(data.get("error")):
            _mark_legacy_index_availability("omni", available=False)
            fallback = await _fallback_story_knowledge_search_with_local(
                query,
                top,
                reason=f"missing_legacy_index:{OMNI_INDEX}",
                dominant_domain=dominant_domain,
                local_story=local_story,
            )
            if fallback:
                return _with_business_website_context(query, fallback, local_story=local_story)
            local_items = list(local_story.get("items", []) or [])[: max(1, int(top or 10))]
            if local_items:
                return _with_business_website_context(query, {
                    "total_results": len(local_items),
                    "items": local_items,
                    "_fallback": {
                        "reason": f"missing_legacy_index:{OMNI_INDEX}",
                        "source": "local_story_context",
                    "dominant_domain": dominant_domain,
                    },
                }, local_story=local_story)
        fallback = await _fallback_story_knowledge_search_with_local(
            query,
            top,
            reason="legacy_search_error",
            dominant_domain=dominant_domain,
            local_story=local_story,
        )
        if fallback:
            fallback.setdefault("_fallback", {})
            fallback["_fallback"]["legacy_error"] = data["error"]
            return _with_business_website_context(query, fallback, local_story=local_story)
        local_items = list(local_story.get("items", []) or [])[: max(1, int(top or 10))]
        if local_items:
            return _with_business_website_context(query, {
                "total_results": len(local_items),
                "items": local_items,
                "_fallback": {
                    "reason": "legacy_search_error",
                    "source": "local_story_context",
                    "dominant_domain": dominant_domain,
                    "legacy_error": data["error"],
                },
            }, local_story=local_story)
        return {"error": data["error"]}
    _mark_legacy_index_availability("omni", available=True)
    legacy_items = [
        {
            "id": d.get("id", ""),
            "title": d.get("tag", "") or d.get("content", "")[:120],
            "content": d.get("content", "")[:500],
            "url": d.get("url", ""),
            "tag": d.get("tag", ""),
            "score": round(d.get("@search.score", 0), 4),
            "origin": "azure_ai_search_omni",
        }
        for d in data.get("value", [])
    ]
    merged_items = _dedupe_website_items(
        _rank_website_items(
            query,
            legacy_items + list(local_story.get("items", []) or []),
        )
    )
    merged_items.sort(
        key=lambda item: (
            float(item.get("hybrid_score", item.get("score", 0.0)) or 0.0),
            float(item.get("score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    items, rerank_meta = await _rerank_items_post_retrieval(query, merged_items[: max(1, int(top or 10) * 2)])
    items = items[: max(1, int(top or 10))]
    result = {
        "total_results": len(items),
        "items": items,
    }
    if local_story.get("items"):
        result["_hybrid"] = {
            "legacy_items": len(legacy_items),
            "local_story_items": len(list(local_story.get("items", []) or [])),
            "dominant_domain": dominant_domain,
            "local_sources": list(local_story.get("sources", []) or []),
        }
    if rerank_meta.get("applied"):
        result["_rerank"] = rerank_meta
    if not emb and local_story.get("items"):
        result.setdefault("_fallback", {})
        result["_fallback"].update(
            {
                "reason": "embedding_unavailable",
                "source": "local_story_context",
                "dominant_domain": dominant_domain,
            }
        )
    return _with_business_website_context(query, result, local_story=local_story)


async def tool_search_web(query: str, top: int = 5) -> dict:
    """Pesquisa web via Brave Search API. Retorna snippets relevantes."""
    if not WEB_SEARCH_ENABLED or not WEB_SEARCH_API_KEY:
        return {"error": "Pesquisa web não está configurada. Contactar administrador."}

    query = str(query or "").strip()[:200]
    if not query:
        return {"error": "Query de pesquisa vazia."}

    original_query = query
    pii_ctx = PIIMaskingContext()
    query = _regex_pre_mask(query, pii_ctx)
    if pii_ctx.mappings:
        logging.warning(
            "[WebSearch] PII stripped from query before Brave API: %d patterns masked",
            len(pii_ctx.mappings),
        )

    safe_max = max(1, int(WEB_SEARCH_MAX_RESULTS or 5))
    top = min(max(1, int(top or 5)), safe_max)
    logging.info(
        json.dumps(
            {
                "event": "web_search_query",
                "query": query[:200],
                "top": top,
                "source": "brave",
            },
            ensure_ascii=False,
        )
    )

    headers = {
        "X-Subscription-Token": WEB_SEARCH_API_KEY,
        "Accept": "application/json",
    }

    market_parts = str(WEB_SEARCH_MARKET or "pt-PT").split("-")
    country = market_parts[1] if len(market_parts) > 1 else "PT"
    country = str(country or "PT").lower()

    params = {
        "q": query,
        "count": top,
        "country": country,
        "text_decorations": "false",
        "result_filter": "web",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(WEB_SEARCH_ENDPOINT, headers=headers, params=params)
    except Exception as e:
        return {"error": f"Pesquisa web falhou: {str(e)}"}

    if resp.status_code != 200:
        return {
            "error": (
                f"Brave Search API {resp.status_code}: "
                f"{_sanitize_error_response(resp.text, 200)}"
            )
        }

    try:
        data = resp.json()
    except Exception:
        return {"error": "Resposta inválida da Brave Search API."}

    web_results = (data.get("web") or {}).get("results") or []
    results = []
    for item in web_results[:top]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title", "") or ""),
                "url": str(item.get("url", "") or ""),
                "snippet": str(item.get("description", "") or "")[:500],
            }
        )

    result = {
        "query": original_query,
        "total_estimated": len(web_results),
        "results": results,
        "results_count": len(results),
    }

    # Optional: enrich with Brave Answers when explicitly configured.
    if WEB_ANSWERS_ENABLED and WEB_ANSWERS_API_KEY:
        answer_headers = {
            "X-Subscription-Token": WEB_ANSWERS_API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        answer_payload = {
            "messages": [{"role": "user", "content": query}],
            "model": WEB_ANSWERS_MODEL,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=max(5.0, float(WEB_ANSWERS_TIMEOUT_SECONDS or 20))) as client:
                answer_resp = await client.post(
                    WEB_ANSWERS_ENDPOINT,
                    headers=answer_headers,
                    json=answer_payload,
                )
            if answer_resp.status_code == 200:
                answer_data = answer_resp.json()
                answer_text = (
                    (answer_data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if isinstance(answer_text, str) and answer_text.strip():
                    result["answer"] = answer_text.strip()[:4000]
            else:
                logging.warning(
                    "[WebSearch] Brave Answers HTTP %s: %s",
                    answer_resp.status_code,
                    _sanitize_error_response(answer_resp.text, 200),
                )
        except Exception as e:
            logging.warning("[WebSearch] Brave Answers failed: %s", e)

    return result
