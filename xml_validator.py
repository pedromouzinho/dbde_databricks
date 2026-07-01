# =============================================================================
# xml_validator.py — Validador de ficheiros SEPA C2B (ISO 20022), 3 camadas
# =============================================================================
# Camada 1 (estrutura): well-formed, root <Document>, layout detetável, um só
#   tipo de mensagem, fim de linha CRLF (aviso).
# Camada 2 (schema): validação contra o XSD pain.XXX (via `xmlschema`).
# Camada 3 (regras de negócio): regras de conteúdo derivadas do manual do BdP
#   (C2B XML SEPA v03.01) — charset, datas, montantes, IBAN/BIC/Ctry, códigos
#   fixos, reconciliação CtrlSum/NbOfTxs, tamanhos.
#
# Puro e testável (sem rede). As explicações em PT do agente vivem noutra
# camada (routes_validator) — este módulo devolve um `report` estruturado.
#
# Fonte das regras: docs/sepa_c2b_ruleset.md
# =============================================================================

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "schemas")

# namespace do <Document> -> metadados do layout
LAYOUTS: Dict[str, Dict[str, str]] = {
    "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03": {
        "pain": "pain.001.001.03",
        "kind": "credit_transfer",
        "label": "Transferências a Crédito",
        "xsd": os.path.join(_SCHEMA_DIR, "2009", "CustomerCreditTransferInitiationV03.xsd"),
        "root": "CstmrCdtTrfInitn",
        "tx": "CdtTrfTxInf",
        "pmtmtd": "TRF",
    },
    "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02": {
        "pain": "pain.008.001.02",
        "kind": "direct_debit",
        "label": "Cobranças / Débitos Diretos",
        "xsd": os.path.join(_SCHEMA_DIR, "2009", "pain.008.001.02.xsd"),
        "root": "CstmrDrctDbtInitn",
        "tx": "DrctDbtTxInf",
        "pmtmtd": "DD",
    },
}

# §3.3 do manual — conjunto de caracteres SEPA admitido
SEPA_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "/-?:().,'+ "
)

_AMOUNT_MAX = Decimal("999999999.99")
_BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_CTRY_RE = re.compile(r"^[A-Z]{2}$")

# Campos com limite de 35 (Max35Text) que faz sentido validar como regra de conteúdo.
_LEN35_TAGS = {"MsgId", "PmtInfId", "EndToEndId", "MndtId", "OrgnlMndtId", "PmtMtd"}


# =============================================================================
# Helpers de parsing / namespaces
# =============================================================================

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _err(layer: str, code: str, message: str, *, tag: str = "", value: str = "",
         line: Optional[int] = None, hint: str = "", severity: str = "error") -> Dict[str, Any]:
    return {
        "layer": layer, "code": code, "severity": severity,
        "tag": tag, "value": value, "line": line,
        "message": message, "hint": hint,
    }


# =============================================================================
# Deteção de layout
# =============================================================================

def detect_layout(data: bytes) -> Dict[str, Any]:
    """Deteta o layout SEPA a partir da namespace do <Document>. Aceita .txt/.xml.
    Devolve {ok, namespace, layout|None, error?}."""
    text = _decode(data)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return {"ok": False, "error": f"XML mal formado: {e}", "namespace": "", "layout": None}
    ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if _localname(root.tag) != "Document":
        return {"ok": False, "error": "O elemento raiz não é <Document>.", "namespace": ns, "layout": None}
    layout = LAYOUTS.get(ns)
    if not layout:
        return {"ok": False, "error": f"Namespace/layout não suportado: {ns or '(vazia)'}",
                "namespace": ns, "layout": None}
    return {"ok": True, "namespace": ns, "layout": layout, "root": root}


# =============================================================================
# Camada 1 — Estrutura
# =============================================================================

def validate_structure(data: bytes, detected: Dict[str, Any]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    if not detected.get("ok"):
        errors.append(_err("structure", "STRUCT_LAYOUT",
                           detected.get("error", "Estrutura inválida."),
                           hint="Garante um XML SEPA válido com <Document> e a namespace pain.001/pain.008."))
        return errors
    text = _decode(data)
    # Um só tipo de mensagem por ficheiro (§3.4): só um root de mensagem esperado.
    root = detected["root"]
    msg_roots = [c for c in list(root) if _localname(c.tag) in (
        "CstmrCdtTrfInitn", "CstmrDrctDbtInitn")]
    if len(msg_roots) != 1:
        errors.append(_err("structure", "STRUCT_ONE_MSG",
                           "O ficheiro deve conter exatamente um tipo de mensagem (§3.4).",
                           hint="Não misturar pain.001 e pain.008 nem duplicar mensagens."))
    # CRLF recomendado (§3.4) — aviso, não bloqueia.
    if "\n" in text and "\r\n" not in text:
        errors.append(_err("structure", "STRUCT_CRLF",
                           "Fins de linha não são CRLF (§3.4, recomendado).",
                           hint="Gravar o ficheiro com fins de linha Windows (CRLF).",
                           severity="warning"))
    return errors


# =============================================================================
# Camada 2 — Schema (XSD)
# =============================================================================

_schema_cache: Dict[str, Any] = {}


def _load_schema(xsd_path: str):
    if xsd_path not in _schema_cache:
        import xmlschema  # import tardio: só quando há validação de schema
        _schema_cache[xsd_path] = xmlschema.XMLSchema(xsd_path)
    return _schema_cache[xsd_path]


def validate_schema(data: bytes, layout: Dict[str, str], *, max_errors: int = 100) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    try:
        schema = _load_schema(layout["xsd"])
    except Exception as e:
        errors.append(_err("schema", "SCHEMA_LOAD", f"Falha ao carregar o XSD: {e}"))
        return errors
    text = _decode(data)
    try:
        for i, e in enumerate(schema.iter_errors(text)):
            if i >= max_errors:
                errors.append(_err("schema", "SCHEMA_TRUNC",
                                   f"Mais de {max_errors} erros de schema — mostrados os primeiros.",
                                   severity="warning"))
                break
            path = getattr(e, "path", "") or ""
            tag = _localname(path.rstrip("/").split("/")[-1]) if path else ""
            value = ""
            try:
                if e.obj is not None and not list(getattr(e.obj, "__iter__", lambda: [])()):
                    value = (e.obj.text or "") if hasattr(e.obj, "text") else str(e.obj)
            except Exception:
                value = ""
            errors.append(_err("schema", "SCHEMA_INVALID",
                               (e.reason or str(e)).split("\n")[0][:300],
                               tag=tag, value=(value or "")[:120],
                               line=getattr(e, "sourceline", None) or None))
    except Exception as e:
        errors.append(_err("schema", "SCHEMA_ERR", f"Erro na validação de schema: {str(e)[:200]}"))
    return errors


# =============================================================================
# Camada 3 — Regras de negócio (conteúdo)
# =============================================================================

def _iban_valid(iban: str) -> bool:
    s = re.sub(r"\s+", "", iban).upper()
    if not re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$", s) or len(s) > 34:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)  # A->10 ... Z->35
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _is_business_day(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def _add_business_days(d: date, n: int, holidays: set) -> date:
    while n > 0:
        d += timedelta(days=1)
        if _is_business_day(d, holidays):
            n -= 1
    return d


def min_collection_date(now: datetime, holidays: Optional[set] = None) -> date:
    """Regra confirmada (Pedro): submissão ≤10h de dia útil → dia útil seguinte;
    >10h de dia útil → D+2 dias úteis. Submissão em dia não-útil → próximo dia útil."""
    holidays = holidays or set()
    today = now.date()
    if _is_business_day(today, holidays):
        return _add_business_days(today, 1 if now.hour < 10 else 2, holidays)
    return _add_business_days(today, 1, holidays)


def _parse_iso_date(v: str) -> Optional[date]:
    try:
        return datetime.strptime(v.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_iso_datetime(v: str) -> Optional[datetime]:
    s = v.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s.split("+")[0].rstrip("Z"), fmt)
        except Exception:
            continue
    return None


def validate_business(detected: Dict[str, Any], *, now: Optional[datetime] = None,
                      cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    now = now or datetime.now()
    cfg = cfg or {}
    holidays = cfg.get("holidays") or set()
    pain001_backdays = int(cfg.get("pain001_back_days", 7))
    errors: List[Dict[str, Any]] = []
    if not detected.get("ok"):
        return errors
    root = detected["root"]
    layout = detected["layout"]
    kind = layout["kind"]

    def texts_of(local: str):
        return [(el, (el.text or "").strip()) for el in root.iter() if _localname(el.tag) == local]

    # --- charset (§3.3) em todos os elementos-folha com texto ---
    for el in root.iter():
        if len(list(el)) > 0:
            continue  # não-folha
        val = (el.text or "").strip()
        if not val:
            continue
        bad = sorted({c for c in val if c not in SEPA_CHARS})
        tag = _localname(el.tag)
        if bad:
            errors.append(_err("business", "BR_CHARSET",
                               f"Caracteres não permitidos em <{tag}>: {' '.join(bad)}",
                               tag=tag, value=val[:80],
                               hint="Só são admitidos a-z A-Z 0-9 / - ? : ( ) . , ' + e espaço (§3.3)."))
        if val.startswith("/") or val.endswith("/") or "//" in val:
            errors.append(_err("business", "BR_SLASH",
                               f"<{tag}> não pode começar/acabar em '/' nem conter '//'.",
                               tag=tag, value=val[:80]))

    # --- datas ---
    for el, v in texts_of("CreDtTm"):
        if v and _parse_iso_datetime(v) is None:
            errors.append(_err("business", "BR_CREDTTM",
                               f"CreDtTm inválido (esperado ISO DateTime): '{v}'.",
                               tag="CreDtTm", value=v,
                               hint="Formato correto: AAAA-MM-DDThh:mm:ss (ex. 2026-06-24T09:30:00)."))
    for el, v in texts_of("DtOfSgntr"):
        if v and _parse_iso_date(v) is None:
            errors.append(_err("business", "BR_DTSGNTR",
                               f"DtOfSgntr inválido (esperado ISO Date): '{v}'.",
                               tag="DtOfSgntr", value=v, hint="Formato AAAA-MM-DD."))

    if kind == "credit_transfer":
        min_dt = now.date() - timedelta(days=pain001_backdays)
        for el, v in texts_of("ReqdExctnDt"):
            d = _parse_iso_date(v)
            if d is None:
                errors.append(_err("business", "BR_REQDEXCTNDT",
                                   f"ReqdExctnDt inválido (esperado ISO Date): '{v}'.",
                                   tag="ReqdExctnDt", value=v, hint="Formato AAAA-MM-DD."))
            elif d < min_dt:
                errors.append(_err("business", "BR_EXCTN_BACKDATE",
                                   f"Data de lançamento {d.isoformat()} anterior ao limite "
                                   f"(mínimo {min_dt.isoformat()}, até {pain001_backdays} dias atrás).",
                                   tag="ReqdExctnDt", value=v,
                                   hint=f"Usar uma data ≥ {min_dt.isoformat()}."))
    else:  # direct_debit
        min_col = min_collection_date(now, holidays)
        for el, v in texts_of("ReqdColltnDt"):
            d = _parse_iso_date(v)
            if d is None:
                errors.append(_err("business", "BR_REQDCOLLTNDT",
                                   f"ReqdColltnDt inválido (esperado ISO Date): '{v}'.",
                                   tag="ReqdColltnDt", value=v, hint="Formato AAAA-MM-DD."))
            elif d < min_col:
                errors.append(_err("business", "BR_COLL_TOOEARLY",
                                   f"Data de cobrança {d.isoformat()} antes do mínimo permitido "
                                   f"({min_col.isoformat()}).",
                                   tag="ReqdColltnDt", value=v,
                                   hint=f"Submissão {'≤' if now.hour < 10 else '>'}10h → data ≥ {min_col.isoformat()}."))

    # --- montantes ---
    for el, v in texts_of("InstdAmt"):
        ccy = el.get("Ccy", "")
        if ccy and ccy != "EUR":
            errors.append(_err("business", "BR_CCY", f"Moeda não permitida em InstdAmt: '{ccy}'.",
                               tag="InstdAmt", value=f"{v} {ccy}", hint="Só é permitido EUR."))
        amt = _to_decimal(v)
        if amt is None:
            errors.append(_err("business", "BR_AMT_FMT", f"Montante inválido em InstdAmt: '{v}'.",
                               tag="InstdAmt", value=v))
        else:
            if amt <= 0 or amt > _AMOUNT_MAX:
                errors.append(_err("business", "BR_AMT_RANGE",
                                   f"Montante fora do intervalo (0 < x ≤ 999999999.99): '{v}'.",
                                   tag="InstdAmt", value=v))
            if _decimals(v) > 2:
                errors.append(_err("business", "BR_AMT_DEC",
                                   f"Montante com mais de 2 casas decimais: '{v}'.",
                                   tag="InstdAmt", value=v))
    for el, v in texts_of("CtrlSum"):
        if v and _decimals(v) > 2:
            errors.append(_err("business", "BR_CTRLSUM_DEC",
                               f"CtrlSum com mais de 2 casas decimais: '{v}'.",
                               tag="CtrlSum", value=v))

    # --- IBAN / BIC / Ctry ---
    for el, v in texts_of("IBAN"):
        if v and not _iban_valid(v):
            errors.append(_err("business", "BR_IBAN", f"IBAN inválido (ISO 13616): '{v}'.",
                               tag="IBAN", value=v, hint="Verificar dígitos de controlo e país (ISO 3166)."))
    for el, v in texts_of("BIC"):
        if v and not _BIC_RE.match(v):
            errors.append(_err("business", "BR_BIC", f"BIC com formato inválido: '{v}'.",
                               tag="BIC", value=v, hint="8 ou 11 caracteres: 6 letras + 2 alfanum + 3 opcionais."))
    for el, v in texts_of("Ctry"):
        if v and not _CTRY_RE.match(v):
            errors.append(_err("business", "BR_CTRY",
                               f"Código de país inválido: '{v}' (2 letras maiúsculas ISO 3166).",
                               tag="Ctry", value=v,
                               hint=f"Usar maiúsculas, ex. '{v.upper()[:2]}'."))

    # --- códigos fixos ---
    errors += _fixed_codes(root, layout)

    # --- tamanhos (35) ---
    for local in _LEN35_TAGS:
        for el, v in texts_of(local):
            if v and len(v) > 35:
                errors.append(_err("business", "BR_LEN35",
                                   f"<{local}> excede 35 caracteres ({len(v)}).",
                                   tag=local, value=v[:60]))
    for el, v in texts_of("Nm"):
        if v and len(v) > 70:
            errors.append(_err("business", "BR_NM70",
                               f"<Nm> usa mais de 70 posições ({len(v)}) — só 70 são úteis.",
                               tag="Nm", value=v[:80], severity="warning"))

    # --- reconciliação CtrlSum / NbOfTxs (confirmado sim/sim) ---
    errors += _reconcile(root, layout)
    return errors


def _fixed_codes(root, layout) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    kind = layout["kind"]
    for el in root.iter():
        if _localname(el.tag) == "PmtMtd":
            v = (el.text or "").strip()
            exp = layout["pmtmtd"]
            if v and v != exp:
                out.append(_err("business", "BR_PMTMTD",
                                f"PmtMtd '{v}' inválido para {layout['pain']} (esperado '{exp}').",
                                tag="PmtMtd", value=v))
    if kind == "direct_debit":
        for el in root.iter():
            ln = _localname(el.tag)
            v = (el.text or "").strip()
            parent_local = _parent_local(root, el)
            if ln == "Cd" and parent_local == "SvcLvl" and v and v != "SEPA":
                out.append(_err("business", "BR_SVCLVL", f"SvcLvl/Cd '{v}' inválido (só 'SEPA').",
                                tag="SvcLvl/Cd", value=v))
            if ln == "Cd" and parent_local == "LclInstrm" and v and v not in ("CORE", "B2B"):
                out.append(_err("business", "BR_LCLINSTRM", f"LclInstrm/Cd '{v}' inválido ('CORE' ou 'B2B').",
                                tag="LclInstrm/Cd", value=v))
            if ln == "SeqTp" and v and v not in ("FRST", "OOFF", "RCUR", "FNAL"):
                out.append(_err("business", "BR_SEQTP",
                                f"SeqTp '{v}' inválido (FRST/OOFF/RCUR/FNAL).", tag="SeqTp", value=v))
    return out


def _reconcile(root, layout) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    tx_local = layout["tx"]

    def sum_amounts(scope) -> Decimal:
        total = Decimal("0")
        for el in scope.iter():
            if _localname(el.tag) == "InstdAmt":
                d = _to_decimal((el.text or "").strip())
                if d is not None:
                    total += d
        return total

    def count_tx(scope) -> int:
        return sum(1 for el in scope.iter() if _localname(el.tag) == tx_local)

    # nível de mensagem (GrpHdr)
    grphdr = _first(root, "GrpHdr")
    if grphdr is not None:
        _check_pair(out, grphdr, root, sum_amounts, count_tx, "mensagem")
    # nível de grupo (cada PmtInf)
    for pmtinf in [el for el in root.iter() if _localname(el.tag) == "PmtInf"]:
        _check_pair(out, pmtinf, pmtinf, sum_amounts, count_tx, "grupo PmtInf")
    return out


def _check_pair(out, hdr_scope, sum_scope, sum_amounts, count_tx, level_label):
    ctrlsum_el = _first(hdr_scope, "CtrlSum")
    nbtx_el = _first(hdr_scope, "NbOfTxs")
    if ctrlsum_el is not None:
        declared = _to_decimal((ctrlsum_el.text or "").strip())
        actual = sum_amounts(sum_scope)
        if declared is not None and declared != actual:
            out.append(_err("business", "BR_CTRLSUM",
                            f"CtrlSum ({declared}) ≠ soma dos montantes ({actual}) ao nível da {level_label}.",
                            tag="CtrlSum", value=str(declared),
                            hint=f"Acertar CtrlSum para {actual}."))
    if nbtx_el is not None:
        declared_n = (nbtx_el.text or "").strip()
        actual_n = count_tx(sum_scope)
        if declared_n.isdigit() and int(declared_n) != actual_n:
            out.append(_err("business", "BR_NBOFTXS",
                            f"NbOfTxs ({declared_n}) ≠ nº de transações ({actual_n}) ao nível da {level_label}.",
                            tag="NbOfTxs", value=declared_n,
                            hint=f"Acertar NbOfTxs para {actual_n}."))


# --- pequenos helpers ---
def _first(scope, local):
    for el in scope.iter():
        if _localname(el.tag) == local:
            return el
    return None


def _parent_local(root, target) -> str:
    for parent in root.iter():
        for child in list(parent):
            if child is target:
                return _localname(parent.tag)
    return ""


def _to_decimal(v: str) -> Optional[Decimal]:
    try:
        return Decimal(v)
    except (InvalidOperation, TypeError):
        return None


def _decimals(v: str) -> int:
    v = (v or "").strip()
    return len(v.split(".", 1)[1]) if "." in v else 0


# =============================================================================
# Orquestração
# =============================================================================

def validate_document(data: bytes, filename: str = "", *,
                      now: Optional[datetime] = None,
                      cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Valida um ficheiro SEPA C2B pelas 3 camadas. Devolve um `report`."""
    detected = detect_layout(data)
    errors: List[Dict[str, Any]] = []
    errors += validate_structure(data, detected)
    if detected.get("ok"):
        errors += validate_schema(data, detected["layout"])
        errors += validate_business(detected, now=now, cfg=cfg)

    hard = [e for e in errors if e.get("severity") != "warning"]
    layout = detected.get("layout") or {}
    return {
        "filename": filename,
        "ok": detected.get("ok", False) and not hard,
        "layout": layout.get("pain", ""),
        "layout_label": layout.get("label", ""),
        "namespace": detected.get("namespace", ""),
        "counts": {
            "total": len(errors),
            "errors": len(hard),
            "warnings": len(errors) - len(hard),
            "structure": sum(1 for e in errors if e["layer"] == "structure"),
            "schema": sum(1 for e in errors if e["layer"] == "schema"),
            "business": sum(1 for e in errors if e["layer"] == "business"),
        },
        "errors": errors,
    }
