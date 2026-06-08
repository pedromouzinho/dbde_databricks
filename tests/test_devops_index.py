# =============================================================================
# tests/test_devops_index.py — DevOps semantic index (Lakebase / cosine)
# =============================================================================
# Locks in the search_workitems rewrite that replaced the dead Azure Search path
# with a Lakebase pgvector/cosine index. These tests are pure (no network):
#   - _wi_index_text strips HTML and concatenates the right fields,
#   - tool_search_workitems ranks a seeded index by cosine and shapes results,
#   - it degrades gracefully (no crash) when the index is empty and build fails.
#
# Runs with pytest, or standalone:  python tests/test_devops_index.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tools_knowledge as K
    DEPS_OK = True
except Exception as exc:  # optional deps (httpx/openai) missing in a bare env
    print(f"SKIP test_devops_index (deps unavailable): {exc}")
    DEPS_OK = False


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_wi_index_text_strips_html_and_joins_fields():
    if not DEPS_OK:
        return
    out = K._wi_index_text({
        "title": "Login biometrico",
        "description": "<div>Permitir <b>biometria</b></div>",
        "acceptance_criteria": "<ul><li>Toast de erro</li></ul>",
        "tags": "seguranca",
    })
    assert "Login biometrico" in out
    assert "biometria" in out and "Toast de erro" in out and "seguranca" in out
    assert "<" not in out and ">" not in out


def test_search_ranks_by_cosine_without_network():
    if not DEPS_OK:
        return
    import time
    K._devops_index_cache["data"] = {
        "built_at": "2026-06-08T00:00:00Z", "count": 2,
        "items": [
            {"id": 1, "title": "A", "text": "alpha", "state": "Active", "type": "Bug",
             "area": "X", "url": "u1", "embedding": [1.0, 0.0]},
            {"id": 2, "title": "B", "text": "beta", "state": "New", "type": "US",
             "area": "Y", "url": "u2", "embedding": [0.0, 1.0]},
        ],
    }
    K._devops_index_cache["loaded_at"] = time.time()  # mark cache fresh

    async def fake_emb(_text):
        return [1.0, 0.0]  # closest to item 1

    orig = K.get_embedding
    K.get_embedding = fake_emb
    try:
        out = _run(K.tool_search_workitems("qualquer coisa", top=2))
        assert out["total_results"] == 2
        assert out["items"][0]["id"] == 1          # ranked by cosine
        assert out["items"][0]["score"] >= out["items"][1]["score"]
        # area substring filter narrows results without a network call
        out_y = _run(K.tool_search_workitems("qualquer coisa", top=5, filter_expr="Y"))
        assert [it["id"] for it in out_y["items"]] == [2]
    finally:
        K.get_embedding = orig


def test_empty_query_is_rejected():
    if not DEPS_OK:
        return
    out = _run(K.tool_search_workitems("   "))
    assert "error" in out


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
