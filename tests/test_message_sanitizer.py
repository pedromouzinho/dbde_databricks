# =============================================================================
# tests/test_message_sanitizer.py — orphan tool-message repair (pure, no network)
# =============================================================================
# Locks in sanitize_messages: drop orphan tool messages, synthesize missing tool
# answers, leave valid histories untouched, and be idempotent. Repairs the
# partial-stream shape that otherwise 400s on the next request.
#
# Runs with pytest, or standalone:  python tests/test_message_sanitizer.py
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from message_sanitizer import sanitize_messages


def _assistant_tc(*ids):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": i, "function": {"name": "t", "arguments": "{}"}} for i in ids]}


def _tool(tid, content="ok"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


def test_drops_orphan_tool_message():
    msgs = [{"role": "system", "content": "s"}, _tool("ghost")]
    out = sanitize_messages(msgs)
    assert out == [{"role": "system", "content": "s"}]


def test_appends_synthetic_for_missing_tool_call():
    msgs = [_assistant_tc("a", "b"), _tool("a")]
    out = sanitize_messages(msgs)
    roles = [(m["role"], m.get("tool_call_id")) for m in out]
    assert roles == [("assistant", None), ("tool", "a"), ("tool", "b")]
    assert out[2]["content"] == "[no result]"


def test_well_formed_passthrough_and_idempotent():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        _assistant_tc("a"),
        _tool("a", "result"),
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs
    assert sanitize_messages(out) == out  # idempotent


def test_partial_stream_shape_is_repaired():
    # assistant declared 3 tool_calls but only 2 results were appended (stream cut)
    msgs = [_assistant_tc("a", "b", "c"), _tool("a"), _tool("b")]
    out = sanitize_messages(msgs)
    answered = [m["tool_call_id"] for m in out if m["role"] == "tool"]
    assert answered == ["a", "b", "c"]
    assert out[-1]["content"] == "[no result]"
    # result is now API-valid: every declared id answered, no orphan
    assert sanitize_messages(out) == out


def test_handles_malformed_and_none_content():
    msgs = [
        {"role": "user", "content": None},
        "not-a-dict",
        {"role": "assistant", "content": "", "tool_calls": [{"no_id": True}]},  # no usable id
        _tool("x"),  # orphan
    ]
    out = sanitize_messages(msgs)  # must not raise
    # the malformed assistant has no declared ids; the orphan tool is dropped
    assert all(isinstance(m, dict) for m in out)
    assert not any(m.get("role") == "tool" for m in out)


def test_non_list_passthrough():
    assert sanitize_messages(None) is None


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
