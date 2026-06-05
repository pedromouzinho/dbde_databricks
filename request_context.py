"""Request context — simplified for Databricks."""

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_conversation_id: ContextVar[str] = ContextVar("conversation_id", default="")

def get_request_id() -> str:
    return _request_id.get() or str(uuid.uuid4())[:8]

def get_conversation_id() -> str:
    return _conversation_id.get()

def set_request_context(request_id: str = "", conversation_id: str = ""):
    if request_id:
        _request_id.set(request_id)
    if conversation_id:
        _conversation_id.set(conversation_id)

def reset_request_context():
    _request_id.set("")
    _conversation_id.set("")

def format_request_context() -> str:
    rid = _request_id.get()
    cid = _conversation_id.get()
    parts = []
    if rid:
        parts.append(f"req={rid}")
    if cid:
        parts.append(f"conv={cid[:8]}")
    return " ".join(parts)
