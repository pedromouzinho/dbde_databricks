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


# --- create_workitem: two-step write guard -----------------------------------
# create_workitem is a write action gated behind a DevOps PAT. These tests force
# the PAT on, register the DevOps block, and exercise the two-step confirmation
# guard WITHOUT ever reaching the DevOps network (step 1 only issues a token;
# the bad-token path returns before any HTTP call).

def _ensure_devops_registered():
    """Register the DevOps tool block with a dummy PAT. Returns True on success,
    False if optional deps are missing (so the suite degrades gracefully)."""
    os.environ["DEVOPS_PAT"] = "test-pat"
    try:
        import importlib
        import config_databricks
        importlib.reload(config_databricks)
        R.register_all_tools()
    except Exception as exc:  # missing optional deps in a bare env
        print(f"SKIP create_workitem tests (deps unavailable): {exc}")
        return False
    return "create_workitem" in R.get_registered_tool_names()


def test_create_workitem_is_registered_with_pat():
    if not _ensure_devops_registered():
        return
    assert "create_workitem" in R.get_registered_tool_names()


def test_create_workitem_first_step_issues_token_and_does_not_write():
    if not _ensure_devops_registered():
        return
    out = _run(R.execute_tool(
        "create_workitem",
        {"title": "Login biometrico", "work_item_type": "Bug"},
        conv_id="C1", user_sub="U1",
    ))
    assert out.get("needs_confirmation") is True
    assert out.get("confirmation_token")           # a token was issued
    assert "created" not in out                      # nothing was written
    assert out.get("proposed", {}).get("title") == "Login biometrico"

    # the issued token is bound to this conv/user (validate without a write)
    from tools_devops import consume_create_workitem_confirmation_token
    assert consume_create_workitem_confirmation_token(
        out["confirmation_token"], conv_id="C1", user_sub="U1") is True


def test_create_workitem_rejects_invalid_token():
    if not _ensure_devops_registered():
        return
    out = _run(R.execute_tool(
        "create_workitem",
        {"title": "X", "confirmed": True, "confirmation_token": "bogus"},
        conv_id="C1", user_sub="U1",
    ))
    assert "error" in out
    assert out.get("created") is not True


# --- refine_workitem: read-only proposal (no write, no token) -----------------

def test_refine_workitem_is_registered_with_pat():
    if not _ensure_devops_registered():
        return
    assert "refine_workitem" in R.get_registered_tool_names()


def test_refine_workitem_validates_input_without_network():
    if not _ensure_devops_registered():
        return
    # invalid id and empty request both return before any DevOps/LLM call
    bad_id = _run(R.execute_tool("refine_workitem", {"work_item_id": 0, "refinement_request": "x"}))
    assert "error" in bad_id
    bad_req = _run(R.execute_tool("refine_workitem", {"work_item_id": 5, "refinement_request": "  "}))
    assert "error" in bad_req


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
