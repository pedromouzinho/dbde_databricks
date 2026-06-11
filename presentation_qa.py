"""Presentation QA — deterministic critic + numeric grounding for generated decks.

Clean-room implementation of the vnext "review/repair" idea (concept only),
Databricks-native: pure-Python checks over the planned slide specs plus a numeric
grounding gate that flags any chart/KPI number not present in the source material.
tool_generate_presentation uses this to run a single repair pass before rendering,
so board-level decks don't ship hallucinated metrics or walls of text.

No external calls — operates on the planned specs and the source text only.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

_VISUAL_TYPES = {"chart", "kpi", "kpis", "metrics", "table", "stat_chart", "comparison", "process", "timeline"}
_CONTENT_TYPES = {"content", "bullets"}
_NUM_RE = re.compile(r"\d[\d.,]*%?")


def _digits(value: Any) -> str:
    """Digit core of a value: '82%' -> '82', '1,234' -> '1234', 'R$ 45' -> '45'."""
    return re.sub(r"\D", "", str(value if value is not None else ""))


def extract_supported_numbers(*texts: str) -> Set[str]:
    """Digit-cores of every number appearing in the source material (content/context)."""
    out: Set[str] = set()
    for t in texts:
        for tok in _NUM_RE.findall(str(t or "")):
            d = _digits(tok)
            if d:
                out.add(d)
    return out


def _slide_numeric_values(spec: Dict[str, Any]) -> List[str]:
    """Numbers a slide asserts as data (KPI values, chart series, stat value)."""
    vals: List[str] = []
    for kpi in spec.get("kpis") or []:
        if isinstance(kpi, dict) and kpi.get("value") is not None:
            vals.append(str(kpi["value"]))
    for series in spec.get("series") or []:
        if isinstance(series, dict):
            for v in series.get("values") or []:
                vals.append(str(v))
    if spec.get("stat_value") is not None:
        vals.append(str(spec["stat_value"]))
    return vals


def review_slides(slides: List[Dict[str, Any]], supported_numbers: Set[str]) -> Dict[str, Any]:
    """Deterministic critic. Returns {approved, findings:[{code,message}], unsupported_numbers}.

    Checks: dense text, monotonous layout, no visuals in a long deck, and — the key
    one for credibility — numeric grounding (every chart/KPI number must trace to
    the source).
    """
    findings: List[Dict[str, str]] = []
    unsupported: List[str] = []
    types = [str(s.get("type", "content")).lower().strip() for s in slides if isinstance(s, dict)]

    # 1) numeric grounding
    for idx, spec in enumerate(slides):
        if not isinstance(spec, dict):
            continue
        for raw in _slide_numeric_values(spec):
            d = _digits(raw)
            if not d:
                continue
            if d not in supported_numbers:
                unsupported.append(raw)
                findings.append({
                    "code": "ungrounded_number",
                    "message": f"Slide {idx + 1} ({spec.get('type')}): o valor '{raw}' não existe nas fontes.",
                })

    # 2) dense text
    for idx, spec in enumerate(slides):
        if not isinstance(spec, dict):
            continue
        bullets = spec.get("bullets") or []
        if str(spec.get("type", "")).lower() in _CONTENT_TYPES and len(bullets) > 6:
            findings.append({"code": "dense_text", "message": f"Slide {idx + 1}: {len(bullets)} bullets (>6)."})

    # 3) monotonous layout (3+ consecutive content slides)
    run = 0
    for t in types:
        run = run + 1 if t in _CONTENT_TYPES else 0
        if run >= 3:
            findings.append({"code": "monotonous", "message": "3+ slides de conteúdo seguidos — quebrar com visual."})
            break

    # 4) no visuals in a non-trivial deck
    if len(types) >= 6 and not any(t in _VISUAL_TYPES for t in types):
        findings.append({"code": "no_visuals", "message": "Deck sem nenhum visual (chart/kpi/table)."})

    return {
        "approved": not findings,
        "findings": findings,
        "unsupported_numbers": sorted(set(unsupported)),
    }


def build_repair_instructions(review: Dict[str, Any]) -> str:
    """Turn critic findings into concise repair instructions for the planner."""
    lines = ["O plano de slides anterior tem problemas. Corrige-os mantendo as melhores ideias:"]
    seen = set()
    for f in review.get("findings", []):
        msg = f.get("message", "")
        if msg and msg not in seen:
            seen.add(msg)
            lines.append(f"- {msg}")
    if review.get("unsupported_numbers"):
        lines.append(
            "REGRA CRÍTICA: usa APENAS números que existam nas fontes. "
            "Remove ou corrige estes valores não fundamentados: "
            + ", ".join(review["unsupported_numbers"][:20]) + "."
        )
    lines.append("Mantém ≤6 bullets por slide, alterna layouts e inclui pelo menos um visual.")
    return "\n".join(lines)
