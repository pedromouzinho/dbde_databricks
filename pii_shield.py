"""PII Shield — DISABLED in Databricks mode (100% internal, no data leaves platform)."""

import logging
logger = logging.getLogger(__name__)

PII_ENABLED = False

class PIIMaskingContext:
    """No-op context manager."""
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

def mask_pii(text, *args, **kwargs):
    return text

def mask_messages(messages, *args, **kwargs):
    return messages

def _regex_pre_mask(text, *args, **kwargs):
    return text

logger.info("[PII] Shield DISABLED (Databricks internal mode)")
