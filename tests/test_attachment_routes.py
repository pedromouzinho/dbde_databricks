# =============================================================================
# tests/test_attachment_routes.py — upload attachment serving (pure, no network)
# =============================================================================
# Locks in the attachment retrieval path used for thumbnails/playback:
#   - _resolve_attachment reads bytes from the in-memory cache,
#   - GET /attachment/{id} serves the bytes with the right media type inline,
#   - a missing id returns 404 (blob fallback is a no-op without a DB pool).
#
# Runs with pytest, or standalone:  python tests/test_attachment_routes.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_chat_databricks as R


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_resolve_attachment_from_memory():
    R._uploaded_files["m1"] = {"bytes": b"DATA", "content_type": "image/png", "filename": "a.png"}
    got = _run(R._resolve_attachment("m1"))
    assert got is not None
    data, ct, name = got
    assert data == b"DATA" and ct == "image/png" and name == "a.png"


def test_resolve_attachment_missing_returns_none():
    # No DB pool configured in tests -> blob fallback yields None.
    assert _run(R._resolve_attachment("does-not-exist")) is None
    assert _run(R._resolve_attachment("")) is None


def test_get_attachment_serves_inline_with_media_type():
    R._uploaded_files["v1"] = {"bytes": b"MP4BYTES", "content_type": "video/mp4", "filename": "clip.mp4"}
    resp = _run(R.get_attachment("v1"))
    assert resp.status_code == 200
    assert resp.media_type == "video/mp4"
    assert resp.body == b"MP4BYTES"
    assert resp.headers.get("content-disposition") == "inline"


def test_get_attachment_404_when_missing():
    resp = _run(R.get_attachment("nope-404"))
    assert resp.status_code == 404


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
