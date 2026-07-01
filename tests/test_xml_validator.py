# =============================================================================
# tests/test_xml_validator.py — validador SEPA C2B (puro, sem rede)
# =============================================================================
# Cobre as 3 camadas: deteção de layout, estrutura (CRLF/um-só-tipo), e as regras
# de negócio (charset, datas + limites confirmados, montantes, IBAN/BIC/Ctry,
# códigos fixos, reconciliação). A camada de schema (xmlschema) é testada num
# caso end-to-end, com skip se a lib não estiver instalada.
#
# Runs with pytest, or standalone:  python tests/test_xml_validator.py
# =============================================================================

import os
import sys
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xml_validator as V

NS1 = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
NS8 = "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"
VALID_IBAN = "DE89370400440532013000"  # IBAN de teste válido (mod-97)


def _codes(errors):
    return {e["code"] for e in errors}


def _pain001(body: str) -> bytes:
    return (f'<Document xmlns="{NS1}"><CstmrCdtTrfInitn>{body}</CstmrCdtTrfInitn></Document>').encode()


def _pain008(body: str) -> bytes:
    return (f'<Document xmlns="{NS8}"><CstmrDrctDbtInitn>{body}</CstmrDrctDbtInitn></Document>').encode()


def _biz(data: bytes, **kw):
    det = V.detect_layout(data)
    assert det["ok"], det
    return V.validate_business(det, **kw)


# --- deteção de layout -------------------------------------------------------
def test_detect_pain001_and_pain008():
    assert V.detect_layout(_pain001("<GrpHdr/>"))["layout"]["pain"] == "pain.001.001.03"
    assert V.detect_layout(_pain008("<GrpHdr/>"))["layout"]["pain"] == "pain.008.001.02"


def test_detect_unsupported_and_malformed():
    bad_ns = '<Document xmlns="urn:x:y:z"><X/></Document>'.encode()
    assert V.detect_layout(bad_ns)["ok"] is False
    assert V.detect_layout(b"<not-xml <<<")["ok"] is False
    # root que não é Document
    assert V.detect_layout(f'<Foo xmlns="{NS1}"/>'.encode())["ok"] is False


# --- estrutura ---------------------------------------------------------------
def test_structure_crlf_is_warning():
    det = V.detect_layout(_pain001("<GrpHdr><MsgId>A</MsgId>\n<CreDtTm>2026-06-24T10:00:00</CreDtTm></GrpHdr>"))
    errs = V.validate_structure(_pain001("<GrpHdr>\n</GrpHdr>"), det)
    crlf = [e for e in errs if e["code"] == "STRUCT_CRLF"]
    assert crlf and crlf[0]["severity"] == "warning"


# --- charset (§3.3) ----------------------------------------------------------
def test_charset_rejects_bad_chars_and_slashes():
    errs = _biz(_pain001('<GrpHdr><MsgId>ABC*=DEF</MsgId></GrpHdr>'), now=datetime(2026, 7, 1))
    assert "BR_CHARSET" in _codes(errs)
    errs2 = _biz(_pain001('<GrpHdr><MsgId>/abc//x</MsgId></GrpHdr>'), now=datetime(2026, 7, 1))
    assert "BR_SLASH" in _codes(errs2)


# --- datas -------------------------------------------------------------------
def test_credttm_invalid_datetime():
    errs = _biz(_pain001('<GrpHdr><CreDtTm>202611-27T00:00:00</CreDtTm></GrpHdr>'), now=datetime(2026, 7, 1))
    assert "BR_CREDTTM" in _codes(errs)


def test_pain001_backdate_limit():
    now = datetime(2026, 7, 1, 11, 0)  # limite = 2026-06-24
    too_old = _biz(_pain001('<PmtInf><ReqdExctnDt>2025-11-27</ReqdExctnDt></PmtInf>'), now=now)
    assert "BR_EXCTN_BACKDATE" in _codes(too_old)
    ok = _biz(_pain001('<PmtInf><ReqdExctnDt>2026-06-30</ReqdExctnDt></PmtInf>'), now=now)
    assert "BR_EXCTN_BACKDATE" not in _codes(ok)


def test_min_collection_date_cutoff():
    # 2026-06-24 é uma quarta-feira (dia útil)
    before10 = datetime(2026, 6, 24, 9, 0)
    after10 = datetime(2026, 6, 24, 11, 0)
    assert V.min_collection_date(before10) == date(2026, 6, 25)   # dia útil seguinte
    assert V.min_collection_date(after10) == date(2026, 6, 26)    # D+2 dias úteis


def test_pain008_collection_too_early():
    now = datetime(2026, 6, 24, 11, 0)  # min = 2026-06-26
    early = _biz(_pain008('<PmtInf><ReqdColltnDt>2026-06-25</ReqdColltnDt></PmtInf>'), now=now)
    assert "BR_COLL_TOOEARLY" in _codes(early)
    ok = _biz(_pain008('<PmtInf><ReqdColltnDt>2026-06-26</ReqdColltnDt></PmtInf>'), now=now)
    assert "BR_COLL_TOOEARLY" not in _codes(ok)


# --- montantes ---------------------------------------------------------------
def test_amount_ccy_range_decimals():
    errs = _codes(_biz(_pain001(
        '<PmtInf><CdtTrfTxInf><Amt><InstdAmt Ccy="USD">50.999</InstdAmt></Amt></CdtTrfTxInf></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert "BR_CCY" in errs and "BR_AMT_DEC" in errs
    over = _codes(_biz(_pain001(
        '<PmtInf><CdtTrfTxInf><Amt><InstdAmt Ccy="EUR">1000000000.00</InstdAmt></Amt></CdtTrfTxInf></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert "BR_AMT_RANGE" in over


# --- IBAN / BIC / Ctry -------------------------------------------------------
def test_iban_algorithm():
    assert V._iban_valid(VALID_IBAN) is True
    assert V._iban_valid("PT50000000000000000000000") is False
    assert V._iban_valid("XX00") is False


def test_iban_bic_ctry_rules():
    errs = _codes(_biz(_pain001(
        f'<PmtInf><Dbtr><PstlAdr><Ctry>de</Ctry></PstlAdr></Dbtr>'
        f'<DbtrAcct><Id><IBAN>PT50000000000000000000000</IBAN></Id></DbtrAcct>'
        f'<DbtrAgt><FinInstnId><BIC>BADbic</BIC></FinInstnId></DbtrAgt></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert {"BR_CTRY", "BR_IBAN", "BR_BIC"} <= errs
    # IBAN válido não dispara
    ok = _codes(_biz(_pain001(
        f'<PmtInf><DbtrAcct><Id><IBAN>{VALID_IBAN}</IBAN></Id></DbtrAcct></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert "BR_IBAN" not in ok


# --- códigos fixos -----------------------------------------------------------
def test_fixed_codes():
    p1 = _codes(_biz(_pain001('<PmtInf><PmtMtd>DD</PmtMtd></PmtInf>'), now=datetime(2026, 7, 1)))
    assert "BR_PMTMTD" in p1  # DD inválido em pain.001
    p8 = _codes(_biz(_pain008(
        '<PmtInf><PmtTpInf><SvcLvl><Cd>XPTO</Cd></SvcLvl>'
        '<LclInstrm><Cd>ZZ</Cd></LclInstrm><SeqTp>WRONG</SeqTp></PmtTpInf></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert {"BR_SVCLVL", "BR_LCLINSTRM", "BR_SEQTP"} <= p8


# --- reconciliação -----------------------------------------------------------
def test_reconciliation():
    bad = _codes(_biz(_pain001(
        '<GrpHdr><NbOfTxs>2</NbOfTxs><CtrlSum>100.00</CtrlSum></GrpHdr>'
        '<PmtInf><NbOfTxs>1</NbOfTxs><CtrlSum>100.00</CtrlSum>'
        '<CdtTrfTxInf><Amt><InstdAmt Ccy="EUR">50.00</InstdAmt></Amt></CdtTrfTxInf></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert "BR_CTRLSUM" in bad and "BR_NBOFTXS" in bad
    good = _codes(_biz(_pain001(
        '<GrpHdr><NbOfTxs>1</NbOfTxs><CtrlSum>50.00</CtrlSum></GrpHdr>'
        '<PmtInf><NbOfTxs>1</NbOfTxs><CtrlSum>50.00</CtrlSum>'
        '<CdtTrfTxInf><Amt><InstdAmt Ccy="EUR">50.00</InstdAmt></Amt></CdtTrfTxInf></PmtInf>'),
        now=datetime(2026, 7, 1)))
    assert "BR_CTRLSUM" not in good and "BR_NBOFTXS" not in good


# --- end-to-end (inclui schema; skip se xmlschema indisponível) --------------
def test_validate_document_end_to_end():
    try:
        import xmlschema  # noqa: F401
    except Exception:
        print("SKIP end-to-end (xmlschema indisponível)")
        return
    doc = _pain001(
        '<GrpHdr><MsgId>M1</MsgId><CreDtTm>202611-27T00:00:00</CreDtTm>'
        '<NbOfTxs>1</NbOfTxs><CtrlSum>50.00</CtrlSum></GrpHdr>'
        '<PmtInf><PmtInfId>P1</PmtInfId><PmtMtd>TRF</PmtMtd>'
        '<ReqdExctnDt>2025-11-27</ReqdExctnDt>'
        '<Dbtr><PstlAdr><Ctry>de</Ctry></PstlAdr></Dbtr>'
        '<CdtTrfTxInf><Amt><InstdAmt Ccy="EUR">50.00</InstdAmt></Amt></CdtTrfTxInf></PmtInf>')
    rep = V.validate_document(doc, "IML.xml", now=datetime(2026, 7, 1, 11, 0))
    assert rep["ok"] is False
    assert rep["layout"] == "pain.001.001.03"
    codes = _codes(rep["errors"])
    assert {"BR_CREDTTM", "BR_CTRY", "BR_EXCTN_BACKDATE"} <= codes
    assert rep["counts"]["schema"] >= 1  # camada XSD também detetou algo


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
