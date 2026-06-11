# =============================================================================
# tests/test_presentation_qa.py — presentation critic + numeric grounding (pure)
# =============================================================================
# Locks in the vnext-equivalent QA layer: numbers in KPIs/charts must trace to the
# source (no hallucinated metrics), plus structural checks (dense text, monotonous
# layout, missing visuals). No network.
#
# Runs with pytest, or standalone:  python tests/test_presentation_qa.py
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presentation_qa import (
    extract_supported_numbers, review_slides, build_repair_instructions, _digits,
)


def test_extract_supported_numbers():
    nums = extract_supported_numbers("45 user stories, cobertura 82% e 1,234 itens", "gap de 12%")
    assert "45" in nums and "82" in nums and "12" in nums and "1234" in nums


def test_digits_core():
    assert _digits("82%") == "82"
    assert _digits("R$ 1.234") == "1234"
    assert _digits("Active") == ""


def test_ungrounded_number_flagged():
    source = extract_supported_numbers("temos 45 user stories e 12 bugs")
    slides = [
        {"type": "kpi", "kpis": [{"value": "45", "label": "US"}, {"value": "99", "label": "Inventado"}]},
        {"type": "chart", "series": [{"name": "x", "values": [12, 500]}]},  # 500 ungrounded
    ]
    review = review_slides(slides, source)
    assert review["approved"] is False
    assert set(review["unsupported_numbers"]) == {"99", "500"}
    assert all(f["code"] != "ungrounded_number" or "45" not in f["message"] for f in review["findings"])


def test_grounded_deck_with_visual_is_approved():
    source = extract_supported_numbers("45 stories, 82% cobertura")
    slides = [
        {"type": "section", "title": "Intro"},
        {"type": "kpi", "kpis": [{"value": "45", "label": "US"}, {"value": "82%", "label": "Cob"}]},
        {"type": "content", "title": "Notas", "bullets": ["a", "b"]},
    ]
    review = review_slides(slides, source)
    assert review["approved"] is True
    assert review["findings"] == []


def test_dense_and_monotonous_and_no_visuals():
    slides = [{"type": "content", "bullets": [str(i) for i in range(8)]}]  # dense
    slides += [{"type": "content", "bullets": ["x"]} for _ in range(3)]    # monotonous run
    slides += [{"type": "content", "bullets": ["y"]} for _ in range(3)]    # pushes deck >6, no visuals
    review = review_slides(slides, set())
    codes = {f["code"] for f in review["findings"]}
    assert "dense_text" in codes
    assert "monotonous" in codes
    assert "no_visuals" in codes


def test_repair_instructions_list_unsupported():
    review = {"findings": [{"code": "ungrounded_number", "message": "Slide 1: '99' não existe."}],
              "unsupported_numbers": ["99", "500"]}
    txt = build_repair_instructions(review)
    assert "99" in txt and "500" in txt and "fontes" in txt.lower()


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
