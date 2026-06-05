# =============================================================================
# tests/test_tool_registry.py — wiring tests for the tool registry
# =============================================================================
# Locks in the Fase 1.5 wiring fixes:
#   - search_uploaded_document is registered (was defined but never wired),
#   - conv_id/user_sub are injected only into tools that accept them,
#   - tools outside the injection set are not broken by an unexpected kwarg.
#
# Runs with pytest, or standalone:  python tests/test_tool_registry.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tool_registry_databricks as R


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_search_uploaded_document_is_registered():
    R.register_all_tools()
    assert "search_uploaded_document" in R.get_registered_tool_names()


def test_context_injected_into_declaring_tool():
    async def fake(query="", conv_id="", user_sub=""):
        return {"conv": conv_id, "user": user_sub}
    R.register_tool("search_uploaded_document", fake)
    out = _run(R.execute_tool("search_uploaded_document", {"query": "x"},
                              conv_id="C123", user_sub="U1"))
    assert out == {"conv": "C123", "user": "U1"}


def test_context_not_injected_into_other_tools():
    # generate_chart takes neither conv_id nor user_sub; injecting would break it.
    async def chart(chart_type="bar", title="t"):
        return {"ok": True}
    R.register_tool("generate_chart", chart)
    out = _run(R.execute_tool("generate_chart", {"chart_type": "bar", "title": "t"},
                              conv_id="C123", user_sub="U1"))
    assert out == {"ok": True}


def test_explicit_args_win_over_injection():
    async def fake(query="", conv_id="", user_sub=""):
        return {"conv": conv_id}
    R.register_tool("search_uploaded_document", fake)
    # an explicit conv_id in args must not be overwritten by the injected one
    out = _run(R.execute_tool("search_uploaded_document",
                              {"query": "x", "conv_id": "EXPLICIT"}, conv_id="C123"))
    assert out == {"conv": "EXPLICIT"}


def test_unknown_tool_returns_error():
    out = _run(R.execute_tool("does_not_exist", {}))
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
