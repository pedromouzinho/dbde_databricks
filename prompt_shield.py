"""Prompt Shield — DISABLED in Databricks mode (internal users only)."""

PROMPT_SHIELD_ENABLED = False

def check_messages(messages, *args, **kwargs):
    return None  # No threat detected

def is_prompt_injection(text, *args, **kwargs):
    return False
