# =============================================================================
# tests/test_chat_vision_message.py — native vision attachments (pure, no network)
# =============================================================================
# Locks in the chat attachment contract:
#   - kind/mime detection for images vs video vs docs,
#   - _build_user_content turns image_ids into a multimodal user message
#     (text block + image_url blocks) and parallel display refs, capped,
#   - _strip_inline_images downgrades the turn back to text before persist
#     (so images are NOT re-sent on later turns) while keeping `_images`,
#   - the provider strips `_images` but preserves multimodal list content.
#
# Runs with pytest, or standalone:  python tests/test_chat_vision_message.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_chat_databricks as R
import llm_provider_databricks as P


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _put_upload(uid, data=b"IMGBYTES", ct="image/png", name="shot.png"):
    R._uploaded_files[uid] = {"bytes": data, "content_type": ct, "filename": name}


def test_attachment_kind_detection():
    assert R._attachment_kind("a.png", "") == "image"
    assert R._attachment_kind("noext", "image/jpeg") == "image"
    assert R._attachment_kind("clip.mp4", "") == "video"
    assert R._attachment_kind("x", "video/webm") == "video"
    assert R._attachment_kind("doc.pdf", "application/pdf") == "file"


def test_image_mime_prefers_valid_content_type_then_ext():
    assert R._image_mime("x", "image/webp") == "image/webp"
    assert R._image_mime("photo.JPG", "application/octet-stream") == "image/jpeg"
    assert R._image_mime("noext", "") == "image/png"  # safe default


def test_build_user_content_no_images_is_plain_text():
    content, refs = _run(R._build_user_content("ola", []))
    assert content == "ola"
    assert refs == []


def test_build_user_content_builds_multimodal_blocks():
    _put_upload("u1", b"AAA", "image/png", "a.png")
    _put_upload("u2", b"BBB", "image/jpeg", "b.jpg")
    content, refs = _run(R._build_user_content("descreve", ["u1", "u2"]))
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "descreve"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert [r["id"] for r in refs] == ["u1", "u2"]
    assert all(r["kind"] == "image" and r["url"] == f"/api/attachment/{r['id']}" for r in refs)


def test_build_user_content_skips_unresolvable_ids():
    _put_upload("ok1")
    content, refs = _run(R._build_user_content("t", ["ok1", "ghost"]))
    assert isinstance(content, list) and len(content) == 2  # text + 1 image only
    assert [r["id"] for r in refs] == ["ok1"]


def test_build_user_content_caps_image_count():
    ids = []
    for i in range(R.CHAT_ATTACH_MAX_IMAGES + 5):
        uid = f"cap{i}"
        _put_upload(uid)
        ids.append(uid)
    content, refs = _run(R._build_user_content("t", ids))
    # text block + at most CHAT_ATTACH_MAX_IMAGES image blocks
    assert len(content) == R.CHAT_ATTACH_MAX_IMAGES + 1
    assert len(refs) == R.CHAT_ATTACH_MAX_IMAGES


def test_strip_inline_images_downgrades_and_keeps_refs():
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "o que é isto?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
        "_images": [{"id": "u1", "kind": "image"}],
    }]
    R._strip_inline_images(msgs)
    assert msgs[0]["content"] == "o que é isto?"
    assert msgs[0]["_images"] == [{"id": "u1", "kind": "image"}]
    # idempotent
    R._strip_inline_images(msgs)
    assert msgs[0]["content"] == "o que é isto?"


def test_display_content_handles_multimodal_list():
    assert R._display_content({"content": "plain"}) == "plain"
    assert R._display_content({"content": [
        {"type": "text", "text": "abc"},
        {"type": "image_url", "image_url": {"url": "x"}},
    ]}) == "abc"


def test_provider_clean_messages_strips_internal_keys_keeps_content():
    cleaned = P._clean_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "t"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ], "_images": [{"id": "u1"}]},
        {"role": "assistant", "content": "ok"},
    ])
    assert "_images" not in cleaned[0]
    assert isinstance(cleaned[0]["content"], list)  # multimodal preserved
    assert cleaned[0]["content"][1]["type"] == "image_url"
    assert cleaned[1] == {"role": "assistant", "content": "ok"}


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
