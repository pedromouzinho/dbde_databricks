# =============================================================================
# routes_validator.py — API do Validador SEPA C2B (PoC)
# =============================================================================
# POST /api/validate : recebe o ficheiro XML/TXT, corre as 3 camadas do
#   xml_validator e devolve o `report`. Se o agente estiver ligado, acrescenta
#   uma explicação em PT (executive summary + como corrigir) via LLM interno.
# =============================================================================

import logging
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

from config_databricks import (
    VALIDATOR_ENABLED,
    VALIDATOR_AGENT_ENABLED,
    VALIDATOR_PAIN001_BACK_DAYS,
    VALIDATOR_MAX_FILE_BYTES,
    VALIDATOR_HOLIDAYS,
    LLM_TIER_FAST,
)
from xml_validator import validate_document

logger = logging.getLogger(__name__)
router = APIRouter()


def _cfg() -> dict:
    holidays = set()
    for h in VALIDATOR_HOLIDAYS:
        try:
            holidays.add(datetime.strptime(h, "%Y-%m-%d").date())
        except Exception:
            pass
    return {"pain001_back_days": VALIDATOR_PAIN001_BACK_DAYS, "holidays": holidays}


_AGENT_SYSTEM = (
    "És um assistente que ajuda operacionais de empresas a corrigir ficheiros SEPA C2B "
    "(pain.001/pain.008) submetidos ao banco. Recebes um relatório de validação com erros "
    "técnicos. Explica em PT-PT, claro e conciso, o que está mal e COMO corrigir, agrupando "
    "erros semelhantes. Não inventes regras. Devolve markdown curto: um resumo de topo e uma "
    "lista de correções priorizadas. Não repitas o relatório todo."
)


async def _explain(report: dict) -> str:
    """Explicação em PT do agente a partir do report (LLM interno). Best effort."""
    errs = report.get("errors", [])
    if not errs:
        return ""
    lines = []
    for e in errs[:40]:
        lines.append(f"- [{e['severity']}/{e['layer']}] {e.get('tag','')}: {e['message']}"
                     + (f" (dica: {e['hint']})" if e.get("hint") else ""))
    prompt = (
        f"{_AGENT_SYSTEM}\n\nFicheiro: {report.get('filename','')}\n"
        f"Layout: {report.get('layout','?')} — {report.get('layout_label','')}\n"
        f"Total: {report['counts']['errors']} erros, {report['counts']['warnings']} avisos.\n\n"
        f"Erros:\n" + "\n".join(lines)
    )
    try:
        from llm_provider_databricks import llm_with_fallback
        resp = await llm_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            tier=LLM_TIER_FAST, max_tokens=900, temperature=0.2,
        )
        return (resp.content or "").strip()
    except Exception as e:
        logger.warning("[Validator] agent explanation failed: %s", e)
        return ""


@router.post("/validate")
async def validate(file: UploadFile = File(...), explain: bool = Form(True)):
    if not VALIDATOR_ENABLED:
        return JSONResponse(status_code=503, content={"error": "Validador desativado."})
    if not file.filename:
        return JSONResponse(status_code=400, content={"error": "Nenhum ficheiro."})
    data = await file.read()
    if len(data) > VALIDATOR_MAX_FILE_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"Ficheiro demasiado grande (máx {VALIDATOR_MAX_FILE_BYTES // 1048576} MB)."})

    try:
        report = validate_document(data, file.filename, cfg=_cfg())
    except Exception as e:
        logger.error("[Validator] validation crashed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": f"Erro na validação: {str(e)[:200]}"})

    if VALIDATOR_AGENT_ENABLED and explain and report.get("errors"):
        report["agent"] = await _explain(report)
    return report
