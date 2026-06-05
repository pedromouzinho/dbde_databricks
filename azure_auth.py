"""Azure Auth — DISABLED (Databricks handles auth natively)."""

async def build_azure_openai_auth_headers(*args, **kwargs):
    return {}

async def build_search_auth_headers(*args, api_key="", service_name="", **kwargs):
    headers = {}
    if api_key:
        headers["api-key"] = api_key
    return headers

async def close_azure_auth(*args, **kwargs):
    pass
