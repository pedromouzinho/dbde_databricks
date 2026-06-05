# =============================================================================
# tests/test_artifacts.py — artifact surfacing from tool results
# =============================================================================
# Tools embed client-facing artifacts (download links, Plotly chart specs) in
# their result dict. _extract_artifacts pulls them out so the agent loop can
# surface them to the UI (SSE 'artifact' event + ChatResponse.artifacts).
#
# Runs with pytest, or standalone:  python tests/test_artifacts.py
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_chat_databricks as rc


def test_file_download_artifact():
    r = {"_file_download": {"download_id": "d1", "endpoint": "/api/download/d1",
                            "filename": "x.xlsx", "format": "xlsx", "size_bytes": 2048}}
    arts = rc._extract_artifacts(r)
    assert len(arts) == 1
    assert arts[0]["kind"] == "file"
    assert arts[0]["endpoint"] == "/api/download/d1"
    assert arts[0]["filename"] == "x.xlsx"


def test_chart_artifact():
    r = {"title": "Vendas", "_chart": {"data": [{"type": "bar", "x": [1], "y": [2]}], "layout": {}}}
    arts = rc._extract_artifacts(r)
    assert len(arts) == 1
    assert arts[0]["kind"] == "chart"
    assert arts[0]["title"] == "Vendas"
    assert arts[0]["spec"]["data"]


def test_auto_file_downloads_artifact():
    r = {"_auto_file_downloads": [
        {"endpoint": "/api/download/d2", "filename": "full.csv", "format": "csv", "size_bytes": 100},
    ]}
    arts = rc._extract_artifacts(r)
    assert len(arts) == 1 and arts[0]["kind"] == "file" and arts[0]["filename"] == "full.csv"


def test_multiple_artifacts_in_one_result():
    r = {
        "_file_download": {"endpoint": "/api/download/d1", "filename": "a.xlsx"},
        "_chart": {"data": [{"type": "pie"}], "layout": {}},
    }
    arts = rc._extract_artifacts(r)
    kinds = sorted(a["kind"] for a in arts)
    assert kinds == ["chart", "file"]


def test_no_artifacts():
    assert rc._extract_artifacts("just text") == []
    assert rc._extract_artifacts({"foo": 1}) == []
    assert rc._extract_artifacts({"_chart": {"layout": {}}}) == []   # no data -> skip
    assert rc._extract_artifacts({"_file_download": {"filename": "x"}}) == []  # no endpoint -> skip


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
