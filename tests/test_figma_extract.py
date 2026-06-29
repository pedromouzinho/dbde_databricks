# =============================================================================
# tests/test_figma_extract.py — deep Figma frame extraction (pure, no network)
# =============================================================================
# Locks in the fix for "só metadados, faltam frames":
#   - _extract_frame_content pulls TEXT.characters in reading order + components,
#     with caps,
#   - analyze_figma_flow auto-enumerates a SECTION's child frames as screens,
#     ordered by layout, in a SINGLE API call, with per-frame content,
#   - pagination (total_frames / truncated / remaining_frames) when > max_steps,
#   - search_figma lists a section's child frames with a text preview.
#
# The Figma HTTP layer (_figma_get) is monkeypatched — no network.
#
# Runs with pytest, or standalone:  python tests/test_figma_extract.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools_figma as F


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- canned Figma node tree: a handoff SECTION with 3 child frames -----------
def _text(nid, chars, y, x=0):
    return {"id": nid, "name": "label", "type": "TEXT", "characters": chars,
            "absoluteBoundingBox": {"x": x, "y": y}}


def _instance(nid, name):
    return {"id": nid, "name": name, "type": "INSTANCE"}


def _frame(nid, name, y, children):
    return {"id": nid, "name": name, "type": "FRAME",
            "absoluteBoundingBox": {"x": 0, "y": y}, "children": children}


FRAME_A = _frame("10:1", "1a Onboarding", y=100, children=[
    _text("t1", "Bem-vindo ao Open Banking", y=120),
    _text("t2", "Continuar", y=300),
    _instance("i1", "Button / Continuar"),
])
FRAME_B = _frame("10:2", "2 Selecao banco", y=50, children=[   # y=50 -> sorts first
    _text("t3", "Escolhe o teu banco", y=60),
    _instance("i2", "Dropdown / Bancos"),
])
FRAME_C = _frame("10:3", "7 Sucesso", y=200, children=[
    _text("t4", "Conta associada com sucesso", y=210),
])
SECTION = {"id": "4359:26678", "name": "12-06-2026", "type": "SECTION",
           "children": [FRAME_A, FRAME_B, FRAME_C]}
ALL_FRAMES = {f["id"]: f for f in (FRAME_A, FRAME_B, FRAME_C)}


def _install_fake_figma_get():
    """Return (restore_fn, calls) and patch F._figma_get to serve the canned tree."""
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, dict(params or {})))
        ids = (params or {}).get("ids", "")
        nodes = {}
        for nid in str(ids).split(","):
            nid = nid.strip()
            if nid == SECTION["id"]:
                nodes[nid] = {"document": SECTION}
            elif nid in ALL_FRAMES:
                nodes[nid] = {"document": ALL_FRAMES[nid]}
        return {"name": "MSE | Open Banking", "nodes": nodes,
                "thumbnailUrl": "", "lastModified": ""}

    orig = F._figma_get
    F._figma_get = fake_get
    F._figma_cache.clear()
    return (lambda: setattr(F, "_figma_get", orig)), calls


# --- _extract_frame_content --------------------------------------------------
def test_extract_frame_content_text_reading_order_and_components():
    out = F._extract_frame_content(FRAME_A)
    assert out["texts"] == ["Bem-vindo ao Open Banking", "Continuar"]  # ordered by y
    assert "Button / Continuar" in out["components"]


def test_extract_frame_content_respects_caps():
    many = _frame("z", "big", 0, [_text(f"x{i}", f"linha {i}", y=i) for i in range(40)])
    out = F._extract_frame_content(many, max_texts=5)
    assert len(out["texts"]) == 5
    assert out["texts"][0] == "linha 0"  # reading order preserved


# --- analyze_figma_flow: section enumeration ---------------------------------
def test_analyze_enumerates_section_frames_single_call():
    restore, calls = _install_fake_figma_get()
    try:
        out = _run(F.tool_analyze_figma_flow(file_key="FK", start_node_id=SECTION["id"]))
    finally:
        restore()
    assert out["ordering_mode"] == "section_layout"
    assert out["total_frames"] == 3
    assert out["section_name"] == "12-06-2026"
    assert out["total_steps"] == 3
    # ordered by layout y: B(50) -> A(100) -> C(200)
    assert [s["node_id"] for s in out["steps"]] == ["10:2", "10:1", "10:3"]
    # real per-frame content, not inferred
    a = next(s for s in out["steps"] if s["node_id"] == "10:1")
    assert a["screen_texts"] == ["Bem-vindo ao Open Banking", "Continuar"]
    assert "Button / Continuar" in a["ui_components"]
    # a single /nodes fetch served the whole section
    assert len(calls) == 1


def test_analyze_paginates_when_over_max_steps():
    restore, calls = _install_fake_figma_get()
    try:
        out = _run(F.tool_analyze_figma_flow(file_key="FK", start_node_id=SECTION["id"], max_steps=2))
    finally:
        restore()
    assert out["total_frames"] == 3
    assert out["total_steps"] == 2
    assert out["truncated"] is True
    assert [r["node_id"] for r in out["remaining_frames"]] == ["10:3"]  # C left over


# --- search_figma: child-frame listing ---------------------------------------
def test_search_lists_section_child_frames_with_preview():
    restore, calls = _install_fake_figma_get()
    orig_tok = F._get_figma_token
    F._get_figma_token = lambda: "tok"
    try:
        out = _run(F.tool_search_figma(file_key="FK", node_id=SECTION["id"]))
    finally:
        F._get_figma_token = orig_tok
        restore()
    ids = [it["id"] for it in out["items"]]
    # the section itself + its 3 child frames
    assert SECTION["id"] in ids
    assert {"10:1", "10:2", "10:3"} <= set(ids)
    frame_a = next(it for it in out["items"] if it["id"] == "10:1")
    assert frame_a["parent_section"] == "12-06-2026"
    assert "Bem-vindo ao Open Banking" in frame_a["text_preview"]


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
