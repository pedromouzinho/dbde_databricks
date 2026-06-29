# =============================================================================
# tests/test_image_ingest_ocr.py — vision transcription of uploaded images
# =============================================================================
# Locks in the image-OCR-via-vision path (clean, no network — the vision call is
# monkeypatched):
#   - image filenames are detected,
#   - extract_image_text calls the vision endpoint and returns its text,
#   - it returns "" when disabled or when the payload is too large,
#   - ingest_upload routes images through extract_image_text (not _decode_text)
#     and indexes the resulting transcription.
#
# Runs with pytest, or standalone:  python tests/test_image_ingest_ocr.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import upload_ingest as U
import llm_provider_databricks as P


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, content):
        self.content = content


def test_is_image_filename():
    assert U._is_image_filename("a.PNG")
    assert U._is_image_filename("shot.jpeg")
    assert not U._is_image_filename("doc.pdf")
    assert not U._is_image_filename("data.csv")


def test_extract_image_text_calls_vision_and_returns_text():
    orig = P.llm_with_fallback
    captured = {}

    async def fake_llm(messages=None, tier=None, **kw):
        captured["tier"] = tier
        captured["messages"] = messages
        return _Resp("Transcrição: ecrã de login")

    P.llm_with_fallback = fake_llm
    try:
        out = _run(U.extract_image_text(b"PNGDATA", "login.png"))
    finally:
        P.llm_with_fallback = orig
    assert out == "Transcrição: ecrã de login"
    assert captured["tier"] == U.LLM_TIER_VISION
    blocks = captured["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_image_text_disabled_returns_empty():
    orig_flag = U.IMAGE_INGEST_OCR_ENABLED
    U.IMAGE_INGEST_OCR_ENABLED = False
    try:
        assert _run(U.extract_image_text(b"X", "a.png")) == ""
    finally:
        U.IMAGE_INGEST_OCR_ENABLED = orig_flag


def test_extract_image_text_oversized_returns_empty():
    big = b"x" * (U._OCR_MAX_BYTES + 1)
    assert _run(U.extract_image_text(big, "a.png")) == ""


def test_ingest_upload_routes_images_through_vision():
    orig_extract_img = U.extract_image_text
    orig_extract_txt = U.extract_text
    orig_embed = U.get_embedding
    orig_blob = U.blob_upload_json
    orig_tbl = U.table_insert
    calls = {"image": 0, "text": 0}

    async def fake_extract_image(data, filename):
        calls["image"] += 1
        return "texto transcrito do ecrã " * 50

    def fake_extract_text(data, filename):
        calls["text"] += 1
        return "should-not-be-used"

    async def fake_embed(text):
        return [0.0] * 8

    async def fake_blob(container, name, obj):
        return f"{container}/{name}"

    async def fake_tbl(table, row):
        return None

    U.extract_image_text = fake_extract_image
    U.extract_text = fake_extract_text
    U.get_embedding = fake_embed
    U.blob_upload_json = fake_blob
    U.table_insert = fake_tbl
    try:
        out = _run(U.ingest_upload("conv1", "up1", "mockup.png", b"PNGDATA"))
    finally:
        U.extract_image_text = orig_extract_img
        U.extract_text = orig_extract_txt
        U.get_embedding = orig_embed
        U.blob_upload_json = orig_blob
        U.table_insert = orig_tbl

    assert calls["image"] == 1 and calls["text"] == 0
    assert out["indexed"] is True
    assert out["chunks"] >= 1


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
