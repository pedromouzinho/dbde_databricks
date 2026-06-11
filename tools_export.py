# =============================================================================
# tools_export.py — File/chart generation and temporary file store
# =============================================================================

import json
import logging
import uuid

from config_databricks import (
    AGENT_TOOL_RESULT_MAX_SIZE,
    AGENT_TOOL_RESULT_KEEP_ITEMS,
    EXPORT_ASYNC_THRESHOLD_ROWS,
    EXPORT_FILE_ROW_CAP,
    EXPORT_FILE_ROW_CAP_MAX,
    PPTX_LEGACY_PLANNER_ENABLED,
    PPTX_VNEXT_ENABLED,
    PPTX_QA_ENABLED,
)
from export_engine import to_csv, to_xlsx, to_pdf, to_docx, to_html
from generated_files import (
    cleanup_generated_files as _cleanup_generated_files,
    generated_file_ttl_seconds,
    get_generated_file,
    store_generated_file as _store_generated_file,
)

logger = logging.getLogger(__name__)

_GENERATED_FILE_TTL_SECONDS = generated_file_ttl_seconds()
_AUTO_EXPORT_MIN_ROWS = 25
SUPPORTED_FILE_FORMATS = ("csv", "xlsx", "pdf", "docx", "html", "pptx")

async def _attach_auto_csv_export(
    result: dict,
    title_hint: str,
    min_rows: int = _AUTO_EXPORT_MIN_ROWS,
    *,
    user_sub: str = "",
    conversation_id: str = "",
) -> None:
    """Para resultados pesados, gera CSV completo automaticamente."""
    if not isinstance(result, dict):
        return
    items = result.get("items")
    if not isinstance(items, list):
        return
    total = int(result.get("total_count", len(items)) or 0)
    if total < min_rows or len(items) < min_rows:
        return
    if total >= max(100, EXPORT_ASYNC_THRESHOLD_ROWS):
        # Evita trabalho pesado inline; export pesado deve ir para worker assíncrono.
        result["_auto_export_deferred"] = True
        result["_auto_export_reason"] = "heavy_result_async_recommended"
        return
    if result.get("_auto_file_downloads"):
        return

    try:
        payload = {"items": items, "total_count": total}
        buf = to_csv(payload)
        content = buf.getvalue()
        if not content:
            return
        base_name = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in str(title_hint or "export_completo")).strip()
        base_name = (base_name or "export_completo")[:50]
        # Make the filename unique and self-describing: two auto-exports in the
        # same minute were colliding on the same name (e.g. two query_workitems).
        filename = f"{base_name}_{total}rows_{uuid.uuid4().hex[:6]}.csv"
        download_id = await _store_generated_file(
            content,
            "text/csv",
            filename,
            "csv",
            user_sub=user_sub,
            conversation_id=conversation_id,
            scope="auto_csv_export",
        )
        if not download_id:
            return
        result["_auto_file_downloads"] = [
            {
                "download_id": download_id,
                "endpoint": f"/api/download/{download_id}",
                "filename": filename,
                "format": "csv",
                "mime_type": "text/csv",
                "size_bytes": len(content),
                "expires_in_seconds": _GENERATED_FILE_TTL_SECONDS,
                "auto_generated": True,
                "scope": "full_result",
            }
        ]
    except Exception as e:
        logging.warning("[Tools] auto csv export skipped: %s", e)

async def tool_generate_chart(
    chart_type: str = "bar",
    title: str = "Chart",
    x_values: list = None,
    y_values: list = None,
    labels: list = None,
    values: list = None,
    series: list = None,
    x_label: str = "",
    y_label: str = "",
):
    """Gera um chart spec para Plotly.js. Retorna _chart no resultado."""
    chart_type = (chart_type or "bar").lower().strip()
    supported = ["bar", "pie", "line", "scatter", "histogram", "hbar"]
    if chart_type not in supported:
        chart_type = "bar"

    def _normalize_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return []

    def _is_non_empty_list(value):
        return isinstance(value, list) and len(value) > 0

    data = []
    layout = {
        "title": {"text": title, "font": {"size": 16}},
        "font": {"family": "Montserrat, sans-serif"},
    }

    # Multi-series via 'series' param
    if series and isinstance(series, list):
        valid_series = []
        for s in series:
            if not isinstance(s, dict):
                continue
            trace = {"type": s.get("type", chart_type), "name": s.get("name", "")}
            sx = _normalize_list(s.get("x"))
            sy = _normalize_list(s.get("y"))
            sl = _normalize_list(s.get("labels"))
            sv = _normalize_list(s.get("values"))
            stype = (trace.get("type") or chart_type).lower().strip()
            if stype == "pie":
                if not _is_non_empty_list(sl) or not _is_non_empty_list(sv) or len(sl) != len(sv):
                    continue
                trace["type"] = "pie"
                trace["labels"] = sl
                trace["values"] = sv
            elif stype == "histogram":
                src = sx or sy
                if not _is_non_empty_list(src):
                    continue
                trace["type"] = "histogram"
                trace["x"] = src
            else:
                if not _is_non_empty_list(sx) or not _is_non_empty_list(sy) or len(sx) != len(sy):
                    continue
                trace["type"] = stype if stype in supported else chart_type
                trace["x"] = sx
                trace["y"] = sy
            valid_series.append(trace)
        data.extend(valid_series)
        if not data:
            return {
                "error": "generate_chart: input inválido. Fornece séries com dados válidos (x/y ou labels/values).",
                "chart_generated": False,
            }
    elif chart_type == "pie":
        pie_labels = _normalize_list(labels or x_values)
        pie_values = _normalize_list(values or y_values)
        if not _is_non_empty_list(pie_labels) or not _is_non_empty_list(pie_values) or len(pie_labels) != len(pie_values):
            return {
                "error": "generate_chart: pie requer labels e values não vazios e com o mesmo tamanho.",
                "chart_generated": False,
            }
        data.append({
            "type": "pie",
            "labels": pie_labels,
            "values": pie_values,
            "textinfo": "label+percent",
            "hole": 0.3,
        })
    elif chart_type == "hbar":
        hx = _normalize_list(x_values)
        hy = _normalize_list(y_values)
        if not _is_non_empty_list(hx) or not _is_non_empty_list(hy) or len(hx) != len(hy):
            return {
                "error": "generate_chart: hbar requer x_values e y_values não vazios e com o mesmo tamanho.",
                "chart_generated": False,
            }
        data.append({
            "type": "bar",
            "y": hx,
            "x": hy,
            "orientation": "h",
            "name": title,
        })
        layout["yaxis"] = {"title": x_label, "automargin": True}
        layout["xaxis"] = {"title": y_label}
    elif chart_type == "histogram":
        hist_values = _normalize_list(x_values or y_values)
        if not _is_non_empty_list(hist_values):
            return {
                "error": "generate_chart: histogram requer x_values (ou y_values) com dados.",
                "chart_generated": False,
            }
        data.append({
            "type": "histogram",
            "x": hist_values,
            "name": title,
        })
        layout["xaxis"] = {"title": x_label}
        layout["yaxis"] = {"title": y_label or "Frequência"}
    else:
        # bar, line, scatter
        x_clean = _normalize_list(x_values)
        y_clean = _normalize_list(y_values)
        if not _is_non_empty_list(x_clean) or not _is_non_empty_list(y_clean) or len(x_clean) != len(y_clean):
            return {
                "error": "generate_chart: chart requer x_values e y_values não vazios e com o mesmo tamanho.",
                "chart_generated": False,
            }
        data.append({
            "type": chart_type if chart_type != "bar" else "bar",
            "x": x_clean,
            "y": y_clean,
            "name": title,
        })
        if x_label: layout["xaxis"] = {"title": x_label}
        if y_label: layout["yaxis"] = {"title": y_label}

    chart_spec = {"data": data, "layout": layout, "config": {"responsive": True}}

    return {
        "chart_generated": True,
        "chart_type": chart_type,
        "title": title,
        "data_points": len((x_values or labels or values or [])),
        "_chart": chart_spec,
    }

async def tool_generate_file(
    format: str = "csv",
    title: str = "Export",
    data: list = None,
    columns: list = None,
    conv_id: str = "",
    user_sub: str = "",
):
    """Gera ficheiro em memória (CSV/XLSX/PDF/DOCX/HTML) e devolve metadados de download."""
    fmt = (format or "csv").strip().lower()
    if fmt not in SUPPORTED_FILE_FORMATS:
        return {"error": f"Formato inválido. Suportados: {', '.join(SUPPORTED_FILE_FORMATS)}"}

    if not isinstance(data, list):
        return {"error": "Campo 'data' deve ser array com pelo menos uma linha"}
    total_rows = len(data)
    if total_rows == 0:
        return {"error": "Dados vazios — nada para exportar.", "format": fmt}

    if columns is None:
        first = data[0]
        if isinstance(first, dict):
            columns = list(first.keys())
        elif isinstance(first, (list, tuple)):
            columns = [f"col_{i+1}" for i in range(len(first))]
        else:
            return {"error": "Não foi possível inferir colunas. Envia 'columns' explicitamente."}

    if not isinstance(columns, list) or len(columns) == 0:
        return {"error": "Campo 'columns' deve ser array de strings"}

    clean_columns = [str(c).strip() for c in columns if str(c).strip()]
    if not clean_columns:
        return {"error": "Sem colunas válidas para gerar ficheiro"}

    row_cap = max(1, min(int(EXPORT_FILE_ROW_CAP or 5000), int(EXPORT_FILE_ROW_CAP_MAX or 100000)))
    effective_data = data[:row_cap]
    was_capped = total_rows > row_cap

    items = []
    for row in effective_data:
        if isinstance(row, dict):
            item = {c: row.get(c, "") for c in clean_columns}
        elif isinstance(row, (list, tuple)):
            item = {c: (row[idx] if idx < len(row) else "") for idx, c in enumerate(clean_columns)}
        else:
            continue
        items.append(item)

    if not items:
        return {"error": "Sem linhas válidas para gerar ficheiro"}

    payload = {"items": items, "total_count": len(items)}
    safe_title = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in (title or "Export")).strip()[:40] or "Export"

    try:
        if fmt == "csv":
            mime_type = "text/csv"
            buf = to_csv(payload)
        elif fmt == "xlsx":
            mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            buf = to_xlsx(payload, safe_title)
        elif fmt == "pdf":
            mime_type = "application/pdf"
            buf = to_pdf(payload, safe_title)
        elif fmt == "docx":
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            buf = to_docx(payload, safe_title)
        else:
            mime_type = "text/html"
            buf = to_html(payload, safe_title)
    except Exception as e:
        logging.error("[Tools] tool_generate_file failed (%s): %s", fmt, e)
        return {"error": f"Erro ao gerar ficheiro {fmt}: {str(e)}"}

    content = buf.getvalue()
    if not content:
        return {"error": "Ficheiro gerado está vazio"}

    filename = f"{safe_title}.{fmt}"
    download_id = await _store_generated_file(
        content,
        mime_type,
        filename,
        fmt,
        user_sub=str(user_sub or "").strip(),
        conversation_id=str(conv_id or "").strip(),
        scope="generate_file",
    )
    if not download_id:
        return {"error": "Ficheiro demasiado grande para armazenamento temporário no servidor"}

    result = {
        "file_generated": True,
        "format": fmt,
        "title": safe_title,
        "rows": len(items),
        "rows_total": total_rows,
        "rows_capped": was_capped,
        "columns": clean_columns,
        "_file_download": {
            "download_id": download_id,
            "endpoint": f"/api/download/{download_id}",
            "filename": filename,
            "format": fmt,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "expires_in_seconds": _GENERATED_FILE_TTL_SECONDS,
        },
    }
    if was_capped:
        result["cap_warning"] = (
            f"Dados truncados a {row_cap} de {total_rows} linhas. "
            "Para ficheiro completo, usar /api/export."
        )
    return result

async def tool_generate_presentation(
    title: str = "Apresentação",
    slides: list = None,
    subtitle: str = "",
    badge_text: str = "DBDE",
    content: str = "",
    context: str = "",
    conv_id: str = "",
    user_sub: str = "",
):
    """Gera apresentação PPTX branded Millennium BCP e devolve metadados de download.

    Dois modos:
    1. Structured: passa 'slides' com array de slide specs pré-estruturados
    2. AI-planned: passa 'content' com texto livre — Claude Opus 4.6 planeia
       os slides profissionalmente antes de renderizar
    """
    # ── Mode detection ──
    has_slides = isinstance(slides, list) and len(slides) > 0
    has_content = bool(content and content.strip())

    if not has_slides and not has_content:
        return {"error": "Fornece 'slides' (array de specs) ou 'content' (texto para o Opus planear)."}

    if has_slides and len(slides) > 50:
        return {"error": "Máximo 50 slides por apresentação."}

    planning_used = False
    planning_model = "structured_input"
    planning_note = ""
    planning_fallback_reason = ""
    planning_diagnostics = {}
    if has_content:
        if PPTX_VNEXT_ENABLED:
            try:
                vnext_result = await _plan_presentation_with_vnext(
                    title=title,
                    content=content,
                    context=context,
                    conv_id=conv_id,
                )
                vnext_slides, vnext_note, vnext_diag = _normalize_vnext_result(vnext_result)
            except Exception as e:
                logging.warning("[Tools] VNext PPTX planning failed: %s", e)
                vnext_slides, vnext_note = None, ""
                vnext_diag = {
                    "status": "exception",
                    "fallback_reason": "vnext_exception",
                    "error": str(e)[:200],
                }
            planning_diagnostics["vnext"] = vnext_diag
            if vnext_slides:
                slides = vnext_slides
                planning_used = True
                planning_model = "vnext:model_first"
                planning_note = vnext_note
            else:
                planning_fallback_reason = str(
                    vnext_diag.get("fallback_reason")
                    or vnext_diag.get("status")
                    or "vnext_returned_no_slides"
                )
        else:
            planning_diagnostics["vnext"] = {
                "status": "disabled",
                "fallback_reason": "pptx_vnext_disabled",
            }
            planning_fallback_reason = "pptx_vnext_disabled"

        if not planning_used:
            if PPTX_LEGACY_PLANNER_ENABLED:
                try:
                    from pptx_engine import plan_slides_with_opus

                    planned_slides = await plan_slides_with_opus(
                        content, title=title, context=context, tier="pro",
                    )
                    if planned_slides:
                        slides = planned_slides
                        planning_used = True
                        planning_model = "claude-opus-4.6"
                        planning_note = "Slides planeados pelo lane legado (Opus) por fallback."
                        planning_diagnostics["legacy_planner"] = {
                            "status": "approved",
                            "fallback_reason": planning_fallback_reason,
                        }
                    else:
                        planning_diagnostics["legacy_planner"] = {
                            "status": "empty",
                            "fallback_reason": "legacy_planner_returned_empty",
                        }
                        if not planning_fallback_reason:
                            planning_fallback_reason = "legacy_planner_returned_empty"
                except Exception as e:
                    logging.warning("[Tools] Legacy PPTX planner failed: %s", e)
                    planning_diagnostics["legacy_planner"] = {
                        "status": "exception",
                        "fallback_reason": "legacy_planner_exception",
                        "error": str(e)[:200],
                    }
                    if not planning_fallback_reason:
                        planning_fallback_reason = "legacy_planner_exception"
            else:
                planning_diagnostics["legacy_planner"] = {
                    "status": "disabled",
                    "fallback_reason": "pptx_legacy_planner_disabled",
                }
                if not planning_fallback_reason:
                    planning_fallback_reason = "pptx_legacy_planner_disabled"

        if not planning_used and has_content:
            try:
                # Fallback to deterministic executive content planning when
                # rich content exists, even if the agent also passed slides.
                from pptx_engine import _fallback_slides_from_content, _review_and_rebalance_slide_plan
                slides = _fallback_slides_from_content(content, title)
                slides = _review_and_rebalance_slide_plan(
                    slides, title=title, content=content, context=context,
                )
                if not slides:
                    return {"error": "Erro no planeamento de slides: fallback sem slides utilizáveis"}
                planning_used = True
                planning_model = "legacy_fallback"
                planning_note = "Planeamento caiu no fallback determinístico legado."
                if planning_fallback_reason:
                    planning_note += f" Motivo: {planning_fallback_reason}."
                planning_diagnostics["deterministic_fallback"] = {
                    "status": "used",
                    "fallback_reason": planning_fallback_reason or "content_planning_failed",
                }
            except Exception as e:
                logging.error("[Tools] Deterministic PPTX fallback failed: %s", e)
                return {"error": f"Erro no planeamento de slides: {str(e)[:200]}"}

    # ── QA: review the plan (structure + numeric grounding) and, if it fails,
    #    run ONE repair pass. Prevents hallucinated metrics / walls of text. ──
    qa_diagnostics = None
    if PPTX_QA_ENABLED and has_content and isinstance(slides, list) and slides:
        try:
            from presentation_qa import (
                extract_supported_numbers, review_slides, build_repair_instructions,
            )
            supported = extract_supported_numbers(content, context)
            review = review_slides(slides, supported)
            qa_diagnostics = {
                "approved": review["approved"],
                "findings": review["findings"][:10],
                "unsupported_numbers": review["unsupported_numbers"],
            }
            if not review["approved"] and PPTX_LEGACY_PLANNER_ENABLED:
                from pptx_engine import plan_slides_with_opus
                repair = build_repair_instructions(review)
                try:
                    repaired = await plan_slides_with_opus(
                        content, title=title,
                        context=(context + "\n\n" + repair).strip(), tier="pro",
                    )
                except Exception as e:
                    logging.warning("[Tools] PPTX QA repair plan failed: %s", e)
                    repaired = None
                if repaired:
                    review2 = review_slides(repaired, supported)
                    if len(review2["findings"]) < len(review["findings"]):
                        slides = repaired
                        qa_diagnostics = {
                            "approved": review2["approved"],
                            "findings": review2["findings"][:10],
                            "unsupported_numbers": review2["unsupported_numbers"],
                            "repaired": True,
                        }
        except Exception as e:
            logging.warning("[Tools] PPTX QA skipped: %s", e)

    safe_title = "".join(
        ch if ch.isalnum() or ch in " _-" else "_"
        for ch in (title or "Apresentacao")
    ).strip()[:50] or "Apresentacao"

    try:
        from pptx_engine import generate_presentation
        buf = generate_presentation(
            title,
            slides,
            subtitle=subtitle,
            badge_text=badge_text or "DBDE",
        )
    except ImportError:
        return {"error": "python-pptx não disponível no servidor."}
    except Exception as e:
        logging.error("[Tools] tool_generate_presentation failed: %s", e)
        return {"error": f"Erro ao gerar apresentação: {str(e)[:200]}"}

    file_content = buf.getvalue()
    if not file_content:
        return {"error": "Apresentação gerada está vazia"}

    mime_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    filename = f"{safe_title}.pptx"

    download_id = await _store_generated_file(
        file_content,
        mime_type,
        filename,
        "pptx",
        user_sub=str(user_sub or "").strip(),
        conversation_id=str(conv_id or "").strip(),
        scope="generate_presentation",
    )
    if not download_id:
        return {"error": "Ficheiro demasiado grande para armazenamento temporário"}

    result = {
        "presentation_generated": True,
        "format": "pptx",
        "title": safe_title,
        "total_slides": len(slides) + 2,  # +title +closing auto-added
        "planning_model": planning_model,
        "_file_download": {
            "download_id": download_id,
            "endpoint": f"/api/download/{download_id}",
            "filename": filename,
            "format": "pptx",
            "mime_type": mime_type,
            "size_bytes": len(file_content),
            "expires_in_seconds": _GENERATED_FILE_TTL_SECONDS,
            "primary": True,
            "label": f"📥 Download {filename}",
        },
    }
    if planning_note:
        result["planning_note"] = planning_note
    if qa_diagnostics:
        result["qa"] = qa_diagnostics
    if planning_fallback_reason:
        result["planning_fallback_reason"] = planning_fallback_reason
    if planning_diagnostics:
        result["planning_diagnostics"] = planning_diagnostics
        logger.info(
            "[PPTX] planning title=%s model=%s fallback_reason=%s diagnostics=%s",
            safe_title,
            planning_model,
            planning_fallback_reason or "-",
            json.dumps(planning_diagnostics, ensure_ascii=False, default=str),
        )
    return result


async def _plan_presentation_with_vnext(
    *,
    title: str,
    content: str,
    context: str,
    conv_id: str = "",
):
    from presentation_orchestrator import PresentationRequest
    from presentation_pipeline import prepare_presentation_run
    from presentation_runtime import run_planning_loop
    from presentation_sources import load_conversation_presentation_sources

    sources = await load_conversation_presentation_sources(
        conv_id=conv_id,
        content=content,
        context=context,
    )
    request = PresentationRequest(
        title=title,
        prompt=context or title or "Gerar apresentação premium grounded.",
        content=content,
        context=context,
        source_names=tuple(src.name for src in sources),
    )
    prepared = prepare_presentation_run(request, sources)
    loop_result = await run_planning_loop(
        prepared,
        planner_tier="pro",
        repair_tier="pro",
        max_repairs=1,
    )
    review_codes = [finding.code for finding in loop_result.evaluation.review.findings]
    diagnostics = {
        "status": "critic_rejected",
        "fallback_reason": (
            "critic_rejected:" + ",".join(review_codes)
            if review_codes
            else "critic_rejected"
        ),
        "attempts": loop_result.attempts,
        "capability": prepared.brief.capability.value,
        "selected_source_count": len(prepared.selected_sources),
        "source_names": [src.name for src in prepared.selected_sources[:4]],
        "review_codes": review_codes,
    }
    if not loop_result.evaluation.approved:
        return {"slides": None, "note": "", "diagnostics": diagnostics}

    renderable_specs = [
        spec for spec in loop_result.slide_specs
        if str(spec.get("type") or "").strip().lower() not in {"title", "closing"}
    ]
    if not renderable_specs:
        diagnostics["status"] = "empty_renderable_specs"
        diagnostics["fallback_reason"] = "vnext_returned_no_renderable_specs"
        return {"slides": None, "note": "", "diagnostics": diagnostics}

    note = (
        "Slides planeados pelo lane novo grounded/model-first "
        f"(attempts={loop_result.attempts}, sources={len(prepared.selected_sources)})."
    )
    diagnostics.update(
        {
            "status": "approved",
            "fallback_reason": "",
            "body_slide_count": len(renderable_specs),
        }
    )
    return {"slides": renderable_specs, "note": note, "diagnostics": diagnostics}


def _normalize_vnext_result(result):
    if isinstance(result, tuple):
        slides = result[0] if len(result) > 0 else None
        note = result[1] if len(result) > 1 else ""
        diagnostics = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}
        return slides, note, diagnostics
    if isinstance(result, dict):
        return result.get("slides"), str(result.get("note") or ""), result.get("diagnostics") or {}
    return None, "", {}


async def tool_generate_spreadsheet(
    title: str = "Relatório",
    sheets: list = None,
    content: str = "",
    context: str = "",
    include_summary: bool = True,
    include_charts: bool = True,
    conv_id: str = "",
    user_sub: str = "",
):
    """Gera Excel avançado (multi-sheet, fórmulas, gráficos) via xlsx_engine.

    Dois modos:
    1. Structured: passa 'sheets' com array de sheet specs pré-estruturados
    2. AI-planned: passa 'content' com texto/dados — Claude Opus planeia
    """
    has_sheets = isinstance(sheets, list) and len(sheets) > 0
    has_content = bool(content and content.strip())

    if not has_sheets and not has_content:
        return {"error": "Fornece 'sheets' (array de specs) ou 'content' (texto/dados para o Opus planear)."}

    planning_used = False
    workbook_spec = None

    if has_content:
        try:
            from xlsx_engine import plan_workbook_with_opus
            workbook_spec = await plan_workbook_with_opus(
                content, title=title, context=context, tier="pro",
            )
            if workbook_spec:
                planning_used = True
        except Exception as e:
            logging.warning("[Tools] Opus workbook planning failed: %s", e)

    if not workbook_spec:
        if has_sheets:
            # Build spec from provided sheets
            workbook_spec = {"sheets": sheets, "summary": None, "charts": []}
        elif has_content:
            # Fallback: try to parse content as JSON data
            from xlsx_engine import _fallback_workbook_from_data
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    workbook_spec = _fallback_workbook_from_data(parsed, title=title)
                elif isinstance(parsed, dict) and "items" in parsed:
                    workbook_spec = _fallback_workbook_from_data(
                        parsed["items"], title=title,
                        columns=parsed.get("columns"),
                    )
                else:
                    return {"error": "Content não é JSON válido (array ou {items: [...]})."}
            except json.JSONDecodeError:
                return {"error": "Sem dados estruturados. Passa 'sheets' ou 'content' com dados JSON."}

    if not workbook_spec:
        return {"error": "Não foi possível criar estrutura do workbook."}

    safe_title = "".join(
        ch if ch.isalnum() or ch in " _-" else "_"
        for ch in (title or "Relatorio")
    ).strip()[:50] or "Relatorio"

    try:
        from xlsx_engine import generate_workbook
        buf = generate_workbook(workbook_spec)
    except ImportError:
        return {"error": "openpyxl não disponível no servidor."}
    except Exception as e:
        logging.error("[Tools] tool_generate_spreadsheet failed: %s", e)
        return {"error": f"Erro ao gerar Excel avançado: {str(e)[:200]}"}

    file_content = buf.getvalue()
    if not file_content:
        return {"error": "Excel gerado está vazio"}

    mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    filename = f"{safe_title}.xlsx"

    download_id = await _store_generated_file(
        file_content,
        mime_type,
        filename,
        "xlsx",
        user_sub=str(user_sub or "").strip(),
        conversation_id=str(conv_id or "").strip(),
        scope="generate_spreadsheet",
    )
    if not download_id:
        return {"error": "Ficheiro demasiado grande para armazenamento temporário"}

    total_sheets = len(workbook_spec.get("sheets", []))
    total_rows = sum(len(s.get("data", [])) for s in workbook_spec.get("sheets", []))

    result = {
        "spreadsheet_generated": True,
        "format": "xlsx",
        "title": safe_title,
        "total_sheets": total_sheets,
        "total_rows": total_rows,
        "has_summary": workbook_spec.get("summary") is not None,
        "has_formulas": True,
        "planning_model": "claude-opus-4.6" if planning_used else "structured_input",
        "_file_download": {
            "download_id": download_id,
            "endpoint": f"/api/download/{download_id}",
            "filename": filename,
            "format": "xlsx",
            "mime_type": mime_type,
            "size_bytes": len(file_content),
            "expires_in_seconds": _GENERATED_FILE_TTL_SECONDS,
            "primary": True,
            "label": f"📥 Download {filename}",
        },
    }
    if planning_used:
        result["planning_note"] = "Workbook planeado por Claude Opus 4.6 para máxima qualidade"
    return result


def truncate_tool_result(result_str):
    if len(result_str) <= AGENT_TOOL_RESULT_MAX_SIZE: return result_str
    try:
        data = json.loads(result_str)
        if isinstance(data, dict) and "items" in data:
            original_items = len(data.get("items", []) or [])
            data["items"] = (data.get("items") or [])[:AGENT_TOOL_RESULT_KEEP_ITEMS]
            data["_truncated"] = True
            data["_original_items"] = original_items
            data["items_returned"] = len(data.get("items", []))
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        logging.warning("[Tools] truncate_tool_result fallback: %s", e)
    return result_str[:AGENT_TOOL_RESULT_MAX_SIZE] + "\n...(truncado)"
