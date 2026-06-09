# =============================================================================
# tests/test_compaction.py — conversation compaction planning (pure, no network)
# =============================================================================
# Locks in the context-window compaction logic:
#   - no compaction when the history fits the budget,
#   - compaction past threshold keeps system + recent tail,
#   - the kept tail never starts on an orphan `tool` message (API-breaking),
#   - the summary rendering captures roles + tool-call names.
#
# Runs with pytest, or standalone:  python tests/test_compaction.py
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conversation_compaction as C


def _msg(role, content, **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return m


def test_no_compaction_when_under_budget():
    msgs = [_msg("system", "sys"), _msg("user", "ola"), _msg("assistant", "boas")]
    plan = C.plan_compaction(msgs, budget=100_000)
    assert plan["compacted"] is False


def test_compacts_when_over_threshold():
    # tiny budget forces compaction; keep_recent=2 leaves older msgs to summarize
    big = "palavra " * 200
    msgs = [_msg("system", "sys")] + [_msg("user", big), _msg("assistant", big),
                                      _msg("user", big), _msg("assistant", big)]
    plan = C.plan_compaction(msgs, budget=50, keep_recent=2)
    assert plan["compacted"] is True
    assert plan["system_offset"] == 1
    assert plan["split"] == len(msgs) - 2      # keep last 2 verbatim
    assert plan["summarized_count"] == plan["split"] - 1


def test_kept_tail_never_starts_on_orphan_tool():
    msgs = [
        _msg("system", "sys"),
        _msg("user", "q"),
        _msg("assistant", "", tool_calls=[{"function": {"name": "query_workitems"}}]),
        _msg("tool", "result A"),
        _msg("tool", "result B"),
        _msg("user", "segue"),
        _msg("assistant", "ok"),
    ]
    # keep_recent=3 would put the boundary on a `tool` message -> must move forward
    s = C._safe_split_index(msgs, keep_recent=3, system_offset=1)
    assert msgs[s]["role"] != "tool"
    assert msgs[s]["role"] == "user"


def test_render_for_summary_includes_roles_and_tool_names():
    msgs = [
        _msg("user", "exporta para excel"),
        _msg("assistant", "", tool_calls=[{"function": {"name": "generate_file"}}]),
        _msg("tool", "ok ficheiro"),
    ]
    out = C._render_for_summary(msgs)
    assert "user: exporta para excel" in out
    assert "generate_file" in out
    assert "tool: ok ficheiro" in out


def test_render_truncates_long_content():
    out = C._render_for_summary([_msg("user", "x" * 9000)])
    assert len(out) < 9000  # capped by SUMMARY_INPUT_CHAR_CAP


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
