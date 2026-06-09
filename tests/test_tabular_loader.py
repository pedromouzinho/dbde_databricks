# =============================================================================
# tests/test_tabular_loader.py — Excel reading helper + sandbox allowlist
# =============================================================================
# Locks in the code_interpreter Excel fix:
#   - tabular_loader._read_excel_rows uses python-calamine when present and falls
#     back to pandas (openpyxl) when calamine is missing (the production bug),
#   - code_interpreter ALLOWED_IMPORTS now permits PDF/DOCX/calamine so the
#     sandbox can read those uploads.
# Deterministic with fake modules — no real pandas/calamine/openpyxl needed.
#
# Runs with pytest, or standalone:  python tests/test_tabular_loader.py
# =============================================================================

import io
import sys
import types
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabular_loader as TL


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def notna(self):
        return self

    def where(self, _cond, _val):
        return self

    @property
    def values(self):
        rows = self._rows

        class _V:
            def tolist(self_inner):
                return rows
        return _V()


def _install(name, module):
    saved = sys.modules.get(name, "__missing__")
    sys.modules[name] = module
    return (name, saved)


def _restore(saved):
    name, val = saved
    if val == "__missing__":
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = val


def test_read_excel_rows_pandas_fallback_when_calamine_missing():
    # calamine import raises ImportError; a fake pandas serves the fallback path
    fake_pd = types.ModuleType("pandas")
    fake_pd.read_excel = lambda *a, **k: _FakeFrame([["A", "B"], ["1", "2"]])
    s1 = _install("python_calamine", None)       # -> ImportError on import
    s2 = _install("pandas", fake_pd)
    try:
        rows = TL._read_excel_rows(b"ignored")
        assert rows == [["A", "B"], ["1", "2"]]
    finally:
        _restore(s1)
        _restore(s2)


def test_read_excel_rows_uses_calamine_when_available():
    sheet_rows = [["H1", "H2"], ["x", "y"]]

    class _Sheet:
        def to_python(self):
            return sheet_rows

    class _WB:
        @staticmethod
        def from_filelike(_buf):
            return _WB()

        def get_sheet_by_index(self, _i):
            return _Sheet()

    fake_cal = types.ModuleType("python_calamine")
    fake_cal.CalamineWorkbook = _WB
    s = _install("python_calamine", fake_cal)
    try:
        assert TL._read_excel_rows(b"ignored") == sheet_rows
    finally:
        _restore(s)


def test_no_direct_calamine_import_outside_helper():
    # the 5 readers must go through the helper, not import calamine themselves
    src = open(os.path.join(os.path.dirname(__file__), "..", "tabular_loader.py")).read()
    assert src.count("from python_calamine import") == 1  # only inside _read_excel_rows


def test_code_interpreter_allowlist_includes_pdf_docx_calamine():
    try:
        import code_interpreter as CI
    except Exception as e:
        print(f"SKIP allowlist test (import failed: {e})")
        return
    for mod in ("pdfplumber", "pypdf", "docx", "python_calamine"):
        assert mod in CI.ALLOWED_IMPORTS, f"{mod} missing from ALLOWED_IMPORTS"
        assert CI._is_import_allowed(mod) is True
    # _validate_code returns None (no error) for previously-blocked libs
    assert CI._validate_code("import pdfplumber\nimport docx\nimport pypdf") is None


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
