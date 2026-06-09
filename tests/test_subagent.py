# =============================================================================
# tests/test_subagent.py — agent delegation (pure, no network)
# =============================================================================
# Locks in run_subagent: depth guard, delegate_task excluded from the sub-agent's
# tools (anti-recursion), happy path, a tool round-trip that surfaces artifacts,
# and that delegate_task registers. The LLM/execute_tool are monkeypatched.
#
# Runs with pytest, or standalone:  python tests/test_subagent.py
# =============================================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subagent as S


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, content="", tool_calls=None, model="stub"):
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = model


class _TC:
    def __init__(self, name, arguments="{}", id="tc1"):
        self.name = name
        self.arguments = arguments
        self.id = id


def test_depth_guard_refuses_nested_delegation():
    out = _run(S.run_subagent("faz algo", depth=1))
    assert "error" in out and "aninhada" in out["error"].lower()


def test_empty_task_errors():
    assert "error" in _run(S.run_subagent("   "))


def test_subset_excludes_delegate_task():
    orig = S.get_all_tool_definitions
    S.get_all_tool_definitions = lambda: [
        {"function": {"name": "delegate_task"}},
        {"function": {"name": "query_workitems"}},
    ]
    try:
        names = [d["function"]["name"] for d in S._subagent_tools()]
    finally:
        S.get_all_tool_definitions = orig
    assert names == ["query_workitems"]


def test_happy_path_no_tools_returns_text():
    orig_llm, orig_tools = S.llm_with_fallback, S.get_all_tool_definitions
    S.get_all_tool_definitions = lambda: []

    async def fake_llm(**kwargs):
        return _Resp(content="feito", tool_calls=[])
    S.llm_with_fallback = fake_llm
    try:
        out = _run(S.run_subagent("faz X"))
    finally:
        S.llm_with_fallback, S.get_all_tool_definitions = orig_llm, orig_tools
    assert out["result"] == "feito"
    assert out["iterations"] == 1
    assert out["tools_used"] == []


def test_tool_round_trip_surfaces_artifacts():
    orig_llm, orig_exec, orig_tools = S.llm_with_fallback, S.execute_tool, S.get_all_tool_definitions
    S.get_all_tool_definitions = lambda: [{"function": {"name": "generate_file"}}]

    responses = [
        _Resp(tool_calls=[_TC("generate_file", "{}", "t1")]),  # 1st: call a tool
        _Resp(content="resumo final"),                          # 2nd: final text
    ]

    async def fake_llm(**kwargs):
        return responses.pop(0)

    async def fake_exec(name, args, conv_id="", user_sub=""):
        return {"ok": True, "_file_download": {"endpoint": "/api/download/1", "label": "f.xlsx"}}

    S.llm_with_fallback, S.execute_tool = fake_llm, fake_exec
    try:
        out = _run(S.run_subagent("gera ficheiro", context="dados"))
    finally:
        S.llm_with_fallback, S.execute_tool, S.get_all_tool_definitions = orig_llm, orig_exec, orig_tools

    assert out["result"] == "resumo final"
    assert out["tools_used"] == ["generate_file"]
    # the sub-agent's file artifact is surfaced for the parent loop
    assert out["_auto_file_downloads"] == [{"endpoint": "/api/download/1", "label": "f.xlsx"}]


def test_agent_type_restricts_and_excludes_writes():
    orig = S.get_all_tool_definitions
    S.get_all_tool_definitions = lambda: [
        {"function": {"name": n}} for n in
        ("code_interpreter", "generate_chart", "query_workitems", "generate_user_stories",
         "create_workitem", "delegate_task")
    ]
    try:
        analyst = {d["function"]["name"] for d in S._subagent_tools("data_analyst")}
        general = {d["function"]["name"] for d in S._subagent_tools("general")}
    finally:
        S.get_all_tool_definitions = orig
    # data_analyst keeps only its profile tools...
    assert "code_interpreter" in analyst and "generate_chart" in analyst
    assert "generate_user_stories" not in analyst
    # ...and NO profile (not even general) exposes create_workitem or delegate_task
    assert "create_workitem" not in analyst and "create_workitem" not in general
    assert "delegate_task" not in general


def test_run_subagents_parallel_aggregates():
    orig = S.run_subagent
    calls = []

    async def fake_run(task, context="", *, agent_type="general", **kw):
        calls.append((task, agent_type))
        out = {"result": f"done:{task}", "agent_type": agent_type}
        if task == "b":
            out["_auto_file_downloads"] = [{"endpoint": "/d/2", "label": "b.xlsx"}]
        return out

    S.run_subagent = fake_run
    try:
        out = _run(S.run_subagents_parallel(
            [{"task": "a", "agent_type": "researcher"}, {"task": "b", "agent_type": "data_analyst"}],
            conv_id="c", user_sub="u"))
    finally:
        S.run_subagent = orig
    assert out["count"] == 2
    assert {r["result"] for r in out["results"]} == {"done:a", "done:b"}
    assert out["_auto_file_downloads"] == [{"endpoint": "/d/2", "label": "b.xlsx"}]
    assert ("a", "researcher") in calls and ("b", "data_analyst") in calls


def test_parallel_empty_errors():
    assert "error" in _run(S.run_subagents_parallel([]))


def test_delegate_task_registered():
    import tool_registry_databricks as R
    try:
        import importlib
        import config_databricks
        importlib.reload(config_databricks)
        R.register_all_tools()
    except Exception as e:
        print(f"SKIP registration (deps unavailable): {e}")
        return
    assert "delegate_task" in R.get_registered_tool_names()
    # definition shape: supports single (task) and parallel (tasks) + agent_type;
    # nothing is hard-required (task OR tasks).
    d = next(x for x in R.get_all_tool_definitions() if x["function"]["name"] == "delegate_task")
    props = d["function"]["parameters"]["properties"]
    assert {"task", "tasks", "agent_type"} <= set(props)
    assert d["function"]["parameters"]["required"] == []


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
