# =============================================================================
# tool_registry_databricks.py — Full tool registry for Databricks runtime
# =============================================================================

import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any] | Any]

_handlers: Dict[str, ToolHandler] = {}
_definitions: Dict[str, dict] = {}


def register_tool(name: str, handler: ToolHandler, definition: Optional[dict] = None) -> None:
    tool_name = str(name or "").strip()
    if not tool_name:
        raise ValueError("Tool name cannot be empty")
    _handlers[tool_name] = handler
    if definition is not None:
        _definitions[tool_name] = definition


def has_tool(name: str) -> bool:
    return str(name or "").strip() in _handlers


def get_all_tool_definitions() -> List[dict]:
    return list(_definitions.values())


def get_registered_tool_names() -> List[str]:
    return list(_handlers.keys())


# Tools that need conversation/user context injected. The LLM never supplies
# these (they are server-side context), so the agent loop passes them to
# execute_tool and we merge them in for the tools whose signatures accept them.
# Mirrors the original tools.py inject_conv_id / inject_user_sub table.
_INJECT_CONV_ID = {"generate_file", "generate_presentation", "search_uploaded_document", "code_interpreter"}
_INJECT_USER_SUB = {
    "generate_file", "generate_presentation", "search_uploaded_document",
    "generate_user_stories", "prepare_outlook_draft", "query_workitems", "query_hierarchy",
}


async def execute_tool(name: str, arguments: Dict[str, Any], conv_id: str = "", user_sub: str = "") -> Any:
    tool_name = str(name or "").strip()
    handler = _handlers.get(tool_name)
    if not handler:
        return {"error": f"Tool \'{tool_name}\' not found. Available: {list(_handlers.keys())}"}
    # Inject server-side context (only for tools that declare it — injecting an
    # unexpected kwarg would break the handler dispatch below).
    arguments = dict(arguments or {})
    if conv_id and tool_name in _INJECT_CONV_ID:
        arguments.setdefault("conv_id", conv_id)
    if user_sub and tool_name in _INJECT_USER_SUB:
        arguments.setdefault("user_sub", user_sub)
    try:
        # Try kwargs first (direct function registration)
        try:
            result = handler(**arguments)
        except TypeError:
            # Fallback: handler expects single dict arg (wrapper pattern)
            result = handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as e:
        logger.error("[Registry] Error executing %s: %s", tool_name, e, exc_info=True)
        return {"error": f"Tool execution failed: {str(e)[:500]}"}


# =============================================================================
# TOOL DEFINITIONS (OpenAI function-calling format)
# =============================================================================

TOOL_CODE_INTERPRETER = {
    "type": "function",
    "function": {
        "name": "code_interpreter",
        "description": "Execute Python code for calculations, data analysis, chart generation. Has pandas, numpy, math, statistics, duckdb.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"}
            },
            "required": ["code"]
        }
    }
}

TOOL_QUERY_WORKITEMS = {
    "type": "function",
    "function": {
        "name": "query_workitems",
        "description": "Search Azure DevOps work items (User Stories, Bugs, Tasks, Features, Epics). Can search by text, ID, type, state, or area path.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search in work item titles"},
                "id": {"type": "integer", "description": "Specific work item ID to fetch"},
                "state": {"type": "string", "description": "Filter by state (New, Active, Closed, etc.)"},
                "type": {"type": "string", "description": "Filter by type (User Story, Bug, Task, Feature, Epic)"},
                "area_path": {"type": "string", "description": "Filter by area path (e.g. 'IT.DIT\\DIT\\ADMChannels\\DBKS')"},
                "top": {"type": "integer", "description": "Max results (default 200)"}
            },
            "required": []
        }
    }
}

TOOL_QUERY_HIERARCHY = {
    "type": "function",
    "function": {
        "name": "query_hierarchy",
        "description": "Query hierárquica parent/child no Azure DevOps. OBRIGATÓRIO para pedidos como 'estrutura da Epic X', 'features/user stories da Epic', 'filhos de', 'dentro de'. Resolve ligações parent-child reais (não inferidas por título).",
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "integer", "description": "ID do item pai (ex: a Epic)."},
                "parent_type": {"type": "string", "description": "Tipo do pai. Default: 'Epic'."},
                "child_type": {"type": "string", "description": "Tipo dos filhos a listar. Default: 'User Story'. Para a árvore de uma Epic, usar 'Feature' e depois descer."},
                "area_path": {"type": "string", "description": "Filtro opcional por area path."},
                "title_contains": {"type": "string", "description": "Filtro opcional por título (contains, sem acentos)."},
            },
            "required": [],
        },
    },
}

TOOL_GENERATE_USER_STORIES = {
    "type": "function",
    "function": {
        "name": "generate_user_stories",
        "description": "Generate detailed User Stories from a feature description using the MSE template (Proveniência, Condições, Composição, Comportamento, Mockup).",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Feature description to generate stories from"},
                "context": {"type": "string", "description": "Additional context (existing stories, constraints)"}
            },
            "required": ["description"]
        }
    }
}

TOOL_SEARCH_FIGMA = {
    "type": "function",
    "function": {
        "name": "search_figma",
        "description": "Search and inspect Figma design files. Provide a Figma URL or file key to read node structure, text content, and component details.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text or Figma file URL"},
                "file_key": {"type": "string", "description": "Figma file key (if known)"},
                "node_id": {"type": "string", "description": "Specific node ID to inspect"}
            },
            "required": ["query"]
        }
    }
}

TOOL_SEARCH_MIRO = {
    "type": "function",
    "function": {
        "name": "search_miro",
        "description": "Search Miro boards for sticky notes, shapes, and text content.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text"},
                "board_id": {"type": "string", "description": "Specific board ID (optional)"}
            },
            "required": ["query"]
        }
    }
}

TOOL_GENERATE_FILE = {
    "type": "function",
    "function": {
        "name": "generate_file",
        "description": "Gera ficheiro para download (CSV, XLSX, PDF, DOCX, HTML) quando o utilizador pedir explicitamente para gerar/descarregar ficheiro com dados.",
        "parameters": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["csv", "xlsx", "pdf", "docx", "html"], "description": "Formato do ficheiro a gerar."},
                "title": {"type": "string", "description": "Título/nome base do ficheiro."},
                "data": {"type": "array", "items": {"type": "object"}, "description": "Linhas de dados (array de objetos)."},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "Headers/ordem das colunas no ficheiro."},
            },
            "required": ["format", "title", "data", "columns"],
        },
    },
}

TOOL_GENERATE_CHART = {
    "type": "function",
    "function": {
        "name": "generate_chart",
        "description": "Gera gráfico interativo (bar, pie, line, scatter, histogram, hbar). USA SEMPRE que o utilizador pedir gráfico, chart, visualização ou distribuição visual. Extrai dados de tool_results anteriores ou de dados fornecidos.",
        "parameters": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "description": "Tipo: 'bar','pie','line','scatter','histogram','hbar'. Default: 'bar'."},
                "title": {"type": "string", "description": "Título do gráfico."},
                "x_values": {"type": "array", "items": {"type": "string"}, "description": "Valores eixo X (categorias ou datas). Ex: ['Active','Closed','New']"},
                "y_values": {"type": "array", "items": {"type": "number"}, "description": "Valores eixo Y (numéricos). Ex: [45, 30, 12]"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels para pie chart. Ex: ['Bug','US','Task']"},
                "values": {"type": "array", "items": {"type": "number"}, "description": "Valores para pie chart. Ex: [20, 50, 30]"},
                "series": {"type": "array", "items": {"type": "object"}, "description": "Multi-series. Cada obj: {type,name,x,y,labels,values}"},
                "x_label": {"type": "string", "description": "Label do eixo X"},
                "y_label": {"type": "string", "description": "Label do eixo Y"},
            },
            "required": ["title"],
        },
    },
}

TOOL_GENERATE_PRESENTATION = {
    "type": "function",
    "function": {
        "name": "generate_presentation",
        "description": "Generate a PowerPoint (PPTX) presentation from a brief or structured content.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Presentation title"},
                "content": {"type": "string", "description": "Content/brief for the presentation"},
                "slides_count": {"type": "integer", "description": "Approximate number of slides (default: auto)"}
            },
            "required": ["title", "content"]
        }
    }
}

TOOL_SEARCH_KNOWLEDGE = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "Search internal knowledge base, documentation, and uploaded documents using semantic search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    }
}

TOOL_SEARCH_UPLOADED_DOCUMENT = {
    "type": "function",
    "function": {
        "name": "search_uploaded_document",
        "description": "Search within documents uploaded in the current conversation (PDF, DOCX, Excel, CSV).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query within uploaded documents"}
            },
            "required": ["query"]
        }
    }
}

TOOL_PREPARE_EMAIL_DRAFT = {
    "type": "function",
    "function": {
        "name": "prepare_outlook_draft",
        "description": "Prepare an Outlook email draft with subject, body, and recipients.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email(s), comma-separated"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (supports HTML)"},
                "cc": {"type": "string", "description": "CC recipients (optional)"}
            },
            "required": ["to", "subject", "body"]
        }
    }
}


# =============================================================================
# REGISTRATION — wires handlers to definitions
# =============================================================================

def register_all_tools():
    """Register all available tools based on configuration. Graceful on import errors."""
    from config_databricks import DEVOPS_PAT, FIGMA_ACCESS_TOKEN, MIRO_ACCESS_TOKEN

    # --- Always available ---
    try:
        _register_code_interpreter()
    except Exception as e:
        logger.warning("[Registry] code_interpreter registration failed: %s", e)

    _register_knowledge_search()

    try:
        _register_upload_tools()
    except Exception as e:
        logger.warning("[Registry] Upload tools failed: %s", e)

    # --- DevOps (if PAT configured) ---
    if DEVOPS_PAT:
        try:
            _register_devops_tools()
        except Exception as e:
            logger.warning("[Registry] DevOps tools failed: %s", e)
    else:
        logger.info("[Registry] DevOps tools DISABLED (no PAT)")

    # --- Figma (if token configured) ---
    if FIGMA_ACCESS_TOKEN:
        try:
            _register_figma_tools()
        except Exception as e:
            logger.warning("[Registry] Figma tools failed: %s", e)
    else:
        logger.info("[Registry] Figma tools DISABLED (no token)")

    # --- Miro (if token configured) ---
    if MIRO_ACCESS_TOKEN:
        try:
            _register_miro_tools()
        except Exception as e:
            logger.warning("[Registry] Miro tools failed: %s", e)
    else:
        logger.info("[Registry] Miro tools DISABLED (no token)")

    # --- Export / File generation (always available) ---
    try:
        _register_export_tools()
    except Exception as e:
        logger.warning("[Registry] Export tools failed: %s", e)

    # --- Email ---
    try:
        _register_email_tools()
    except Exception as e:
        logger.warning("[Registry] Email tools failed: %s", e)

    logger.info("[Registry] %d tools registered: %s", len(_handlers), list(_handlers.keys()))


# =============================================================================
# INDIVIDUAL REGISTRATIONS
# =============================================================================

def _register_code_interpreter():
    """Sandboxed Python execution with uploaded file access."""
    from code_interpreter import execute_code

    async def handler(args):
        code = args.get("code", "")
        conv_id = str(args.get("conv_id", "") or "")
        # Get uploaded files from routes module (in-memory store), scoped to the
        # current conversation so code_interpreter only sees this chat's files.
        uploaded_files = {}
        try:
            from routes_chat_databricks import _uploaded_files
            for uid, fdata in _uploaded_files.items():
                if "bytes" not in fdata or "filename" not in fdata:
                    continue
                if conv_id and str(fdata.get("conversation_id", "") or "") != conv_id:
                    continue
                uploaded_files[fdata["filename"]] = fdata["bytes"]
        except Exception:
            pass
        result = await execute_code(code, uploaded_files=uploaded_files if uploaded_files else None)
        return result

    register_tool("code_interpreter", handler, TOOL_CODE_INTERPRETER)


def _register_knowledge_search():
    """Knowledge search (pgvector when available, fallback to stub)."""
    async def handler(args):
        query = args.get("query", "")
        try:
            from tools_knowledge import tool_search_workitems
            # tool_search_workitems(query: str, top: int, filter_expr) — pass the
            # query string positionally, not a dict (that broke semantic search).
            result = await tool_search_workitems(query, top=10)
            return result
        except Exception as e:
            return {"results": [], "message": f"Knowledge search unavailable: {str(e)[:200]}"}

    register_tool("search_knowledge", handler, TOOL_SEARCH_KNOWLEDGE)


def _register_upload_tools():
    """Semantic search within documents uploaded in the current conversation."""
    from tools_upload import tool_search_uploaded_document
    register_tool("search_uploaded_document", tool_search_uploaded_document, TOOL_SEARCH_UPLOADED_DOCUMENT)
    logger.info("[Registry] Upload tools registered")


def _register_devops_tools():
    """Azure DevOps work item tools."""
    from tools_devops import tool_query_workitems, tool_generate_user_stories

    async def _query_workitems_adapter(query: str = "", state: str = "", type: str = "", top: int = 200, area_path: str = "", id: int = 0, **kwargs):
        """Adapter: converts LLM tool params to WIQL WHERE clause."""
        conditions = []
        if id:
            conditions.append(f"[System.Id] = {int(id)}")
        if query:
            conditions.append(f"[System.Title] CONTAINS '{query}'")
        if state:
            conditions.append(f"[System.State] = '{state}'")
        if type:
            conditions.append(f"[System.WorkItemType] = '{type}'")
        if area_path:
            conditions.append(f"[System.AreaPath] UNDER '{area_path}'")
        if not conditions:
            conditions.append("[System.ChangedDate] >= @today - 30")
        wiql_where = " AND ".join(conditions)
        return await tool_query_workitems(wiql_where=wiql_where, top=top)

    from tools_devops import tool_query_hierarchy

    register_tool("query_workitems", _query_workitems_adapter, TOOL_QUERY_WORKITEMS)
    register_tool("query_hierarchy", tool_query_hierarchy, TOOL_QUERY_HIERARCHY)
    register_tool("generate_user_stories", tool_generate_user_stories, TOOL_GENERATE_USER_STORIES)
    logger.info("[Registry] DevOps tools registered")


def _register_figma_tools():
    """Figma design inspection. Delegates to the module's own registration so we
    keep the richer definitions (search_figma accepts figma_url) and also expose
    analyze_figma_flow — both were dropped by the hand-rolled registration."""
    from tools_figma import _register_figma_tool
    _register_figma_tool()
    logger.info("[Registry] Figma tools registered")


def _register_miro_tools():
    """Miro board search. Delegates to the module's own registration."""
    from tools_miro import _register_miro_tool
    _register_miro_tool()
    logger.info("[Registry] Miro tools registered")


def _register_export_tools():
    """File generation (CSV, XLSX, PDF, PPTX)."""
    from tools_export import tool_generate_chart, tool_generate_file, tool_generate_presentation

    register_tool("generate_file", tool_generate_file, TOOL_GENERATE_FILE)
    register_tool("generate_chart", tool_generate_chart, TOOL_GENERATE_CHART)
    register_tool("generate_presentation", tool_generate_presentation, TOOL_GENERATE_PRESENTATION)
    logger.info("[Registry] Export tools registered")


def _register_email_tools():
    """Outlook email draft."""
    from tools_email import tool_prepare_outlook_draft

    register_tool("prepare_outlook_draft", tool_prepare_outlook_draft, TOOL_PREPARE_EMAIL_DRAFT)
    logger.info("[Registry] Email tools registered")
