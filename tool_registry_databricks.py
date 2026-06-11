# =============================================================================
# tool_registry_databricks.py — Full tool registry for Databricks runtime
# =============================================================================

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config_databricks import TOOL_CALL_TIMEOUT_SECONDS

# Heavy tools get a longer per-call timeout than the default; everything else
# uses TOOL_CALL_TIMEOUT_SECONDS. A timed-out tool returns an error result, which
# the agent loop appends as the tool answer (keeps one-result-per-tool_call).
_TOOL_TIMEOUT_OVERRIDES = {
    "code_interpreter": 180,
    "generate_presentation": 180,
    "classify_uploaded_emails": 300,
    "search_workitems": 600,  # may lazily build the embedding index on first use
    "delegate_task": 420,     # one or several sub-agents, each its own multi-step loop
}


def _timeout_for(tool_name: str) -> float:
    return float(_TOOL_TIMEOUT_OVERRIDES.get(tool_name, TOOL_CALL_TIMEOUT_SECONDS))

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
_INJECT_CONV_ID = {
    "generate_file", "generate_presentation", "search_uploaded_document", "code_interpreter",
    "create_workitem", "classify_uploaded_emails", "delegate_task",
}
_INJECT_USER_SUB = {
    "generate_file", "generate_presentation", "search_uploaded_document",
    "generate_user_stories", "prepare_outlook_draft", "query_workitems", "query_hierarchy",
    "create_workitem", "analyze_patterns", "classify_uploaded_emails",
    "get_writer_profile", "save_writer_profile", "delegate_task",
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
            timeout = _timeout_for(tool_name)
            try:
                result = await asyncio.wait_for(result, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("[Registry] Tool %s timed out after %.0fs", tool_name, timeout)
                return {"error": f"A tool '{tool_name}' excedeu o tempo limite ({int(timeout)}s)."}
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

TOOL_CREATE_WORKITEM = {
    "type": "function",
    "function": {
        "name": "create_workitem",
        "description": (
            "Cria um work item no Azure DevOps (User Story, Bug, Task, Feature). "
            "AÇÃO DE ESCRITA IRREVERSÍVEL — fluxo OBRIGATÓRIO em dois passos: "
            "1) Chama PRIMEIRO sem 'confirmed' (ou confirmed=false). Recebes um preview e um 'confirmation_token'; "
            "mostra o preview ao utilizador e pede confirmação explícita. "
            "2) SÓ depois de o utilizador confirmar explicitamente, chama de novo com confirmed=true e o MESMO confirmation_token. "
            "Nunca uses confirmed=true sem aprovação explícita do utilizador na conversa."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "work_item_type": {"type": "string", "enum": ["User Story", "Bug", "Task", "Feature"], "description": "Tipo de work item. Default: 'User Story'."},
                "title": {"type": "string", "description": "Título do work item (obrigatório)."},
                "description": {"type": "string", "description": "Descrição (HTML simples permitido)."},
                "acceptance_criteria": {"type": "string", "description": "Critérios de aceitação (HTML simples permitido)."},
                "area_path": {"type": "string", "description": "Area Path no DevOps (opcional; usa o default se vazio)."},
                "assigned_to": {"type": "string", "description": "Responsável — email ou display name (opcional)."},
                "tags": {"type": "string", "description": "Tags separadas por ';' (opcional)."},
                "confirmed": {"type": "boolean", "description": "Ausente/false no 1º passo (gera preview+token). True só após confirmação explícita do utilizador."},
                "confirmation_token": {"type": "string", "description": "Token devolvido no 1º passo. Reenvia-o inalterado no 2º passo."},
            },
            "required": ["title"],
        },
    },
}

TOOL_REFINE_WORKITEM = {
    "type": "function",
    "function": {
        "name": "refine_workitem",
        "description": (
            "Gera uma PROPOSTA de revisão de uma User Story / work item existente no Azure DevOps, "
            "a partir de um pedido de refinamento. READ-ONLY: lê o work item e devolve uma versão revista "
            "(título, descrição, critérios de aceitação) para o utilizador rever. NÃO altera nada no DevOps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "work_item_id": {"type": "integer", "description": "ID do work item a refinar."},
                "refinement_request": {"type": "string", "description": "Instrução de refinamento (o que melhorar/alterar)."},
            },
            "required": ["work_item_id", "refinement_request"],
        },
    },
}

TOOL_COMPUTE_KPI = {
    "type": "function",
    "function": {
        "name": "compute_kpi",
        "description": (
            "Calcula KPIs/métricas sobre work items do Azure DevOps: contagens, "
            "distribuições por estado/tipo/responsável, e evolução temporal. "
            "Usa para 'quantas stories por estado', 'distribuição de bugs', "
            "'throughput por mês'. Filtra com os MESMOS parâmetros do query_workitems."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a procurar no título (opcional)."},
                "state": {"type": "string", "description": "Filtrar por estado (ex: 'Active', 'Closed')."},
                "type": {"type": "string", "description": "Filtrar por tipo (ex: 'User Story', 'Bug')."},
                "area_path": {"type": "string", "description": "Filtrar por Area Path."},
                "group_by": {"type": "string", "description": "Campo de agrupamento: 'state','type','assigned_to','created_by','area'."},
                "kpi_type": {"type": "string", "enum": ["count", "distribution", "timeline"], "description": "'count' (default), 'distribution' (estado+tipo), 'timeline' (por mês)."},
            },
            "required": [],
        },
    },
}

TOOL_ANALYZE_PATTERNS = {
    "type": "function",
    "function": {
        "name": "analyze_patterns",
        "description": (
            "Analisa o PADRÃO DE ESCRITA de work items reais do DevOps (estrutura, linguagem, "
            "campos, template) usando o LLM. Útil antes de gerar user stories ou para perceber "
            "o estilo de um autor. Com analysis_type='author_style' + created_by, guarda um perfil de escrita."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "created_by": {"type": "string", "description": "Autor a analisar (para estilo de autor)."},
                "topic": {"type": "string", "description": "Tópico/tema a filtrar nos títulos."},
                "work_item_type": {"type": "string", "description": "Tipo de work item. Default: 'User Story'."},
                "area_path": {"type": "string", "description": "Area Path a filtrar."},
                "sample_size": {"type": "integer", "description": "Nº de exemplos a analisar (default 15)."},
                "analysis_type": {"type": "string", "enum": ["template", "author_style"], "description": "'template' (padrão geral) ou 'author_style' (estilo de um autor; guarda perfil)."},
            },
            "required": [],
        },
    },
}

TOOL_SEARCH_WORKITEMS = {
    "type": "function",
    "function": {
        "name": "search_workitems",
        "description": (
            "Pesquisa SEMÂNTICA em work items do Azure DevOps já indexados (encontra os mais "
            "relevantes por significado, não por filtro exato). Usa para 'encontra stories "
            "parecidas com...', 'há algo sobre...', ou para fundamentar geração de user stories. "
            "Para filtros exatos (estado/tipo/área) usa antes o query_workitems."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto/intenção a pesquisar semanticamente."},
                "top": {"type": "integer", "description": "Nº de resultados (default 10)."},
            },
            "required": ["query"],
        },
    },
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

TOOL_CLASSIFY_UPLOADED_EMAILS = {
    "type": "function",
    "function": {
        "name": "classify_uploaded_emails",
        "description": (
            "Classifica em bulk os emails de um ficheiro Excel/CSV carregado, segundo "
            "instruções do utilizador, e gera ficheiros de ações para o Outlook (.ps1/.csv/.xlsx). "
            "Usa quando o utilizador pede para triar/classificar uma lista de emails carregada."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string", "description": "Critérios de classificação/triagem (obrigatório)."},
                "filename": {"type": "string", "description": "Nome do ficheiro carregado a usar (opcional; usa o último se vazio)."},
                "batch_size": {"type": "integer", "description": "Nº de emails por lote de classificação (opcional)."},
            },
            "required": ["instructions"],
        },
    },
}

TOOL_GET_WRITER_PROFILE = {
    "type": "function",
    "function": {
        "name": "get_writer_profile",
        "description": (
            "Carrega o perfil de escrita guardado de um autor (vocabulário, estrutura de título e de "
            "critérios de aceitação) para personalizar a geração de user stories no estilo desse autor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "author_name": {"type": "string", "description": "Nome do autor."},
            },
            "required": ["author_name"],
        },
    },
}

TOOL_SAVE_WRITER_PROFILE = {
    "type": "function",
    "function": {
        "name": "save_writer_profile",
        "description": (
            "Guarda/atualiza o perfil de escrita de um autor (memória de preferências). "
            "Usa depois de analisar o estilo de um autor para reutilizar em gerações futuras."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "author_name": {"type": "string", "description": "Nome do autor."},
                "analysis": {"type": "string", "description": "Descrição do estilo/padrão de escrita."},
                "preferred_vocabulary": {"type": "string", "description": "Vocabulário preferido (opcional)."},
                "title_pattern": {"type": "string", "description": "Padrão de título (opcional)."},
                "ac_structure": {"type": "string", "description": "Estrutura de critérios de aceitação (opcional)."},
            },
            "required": ["author_name", "analysis"],
        },
    },
}


TOOL_DELEGATE_TASK = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delega uma sub-tarefa COMPLEXA/pesada a um sub-agente com contexto isolado e tools "
            "próprias. Para UMA tarefa: passa 'task' (+'context', +'agent_type'). Para VÁRIAS "
            "sub-tarefas INDEPENDENTES: passa 'tasks' (lista) — correm em PARALELO. O sub-agente "
            "NÃO vê o histórico da conversa, por isso inclui no contexto tudo o que precisa. "
            "agent_type escolhe o perfil: 'general', 'data_analyst' (dados/gráficos), "
            "'story_writer' (user stories), 'researcher' (pesquisa read-only), 'presenter' "
            "(apresentações/ficheiros). Os sub-agentes não criam work items."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Objetivo claro e auto-contido (forma simples, 1 sub-tarefa)."},
                "context": {"type": "string", "description": "Dados/contexto que o sub-agente precisa (IDs, dados extraídos, requisitos)."},
                "agent_type": {
                    "type": "string",
                    "enum": ["general", "data_analyst", "story_writer", "researcher", "presenter"],
                    "description": "Perfil do sub-agente (default 'general').",
                },
                "tasks": {
                    "type": "array",
                    "description": "Várias sub-tarefas independentes a correr em paralelo. Cada item: {task, context?, agent_type?}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "context": {"type": "string"},
                            "agent_type": {"type": "string"},
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": [],
        },
    },
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

    # --- Learning / writer profiles ---
    try:
        _register_learning_tools()
    except Exception as e:
        logger.warning("[Registry] Learning tools failed: %s", e)

    # --- Agent delegation (sub-agents) ---
    try:
        _register_subagent_tools()
    except Exception as e:
        logger.warning("[Registry] Sub-agent tool failed: %s", e)

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


def _build_wiql_where(query: str = "", state: str = "", type: str = "", area_path: str = "", id: int = 0) -> str:
    """Convert friendly LLM filter params into a WIQL WHERE clause.

    Shared by query_workitems and compute_kpi so both filter work items
    identically — one source of truth for the DevOps query semantics.
    """
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
    return " AND ".join(conditions)


def _register_devops_tools():
    """Azure DevOps work item tools."""
    from tools_devops import (
        tool_query_workitems,
        tool_generate_user_stories,
        tool_create_workitem,
        tool_refine_workitem,
        tool_compute_kpi,
        tool_analyze_patterns_with_llm,
        issue_create_workitem_confirmation_token,
    )

    async def _query_workitems_adapter(query: str = "", state: str = "", type: str = "", top: int = 200, area_path: str = "", id: int = 0, **kwargs):
        """Adapter: converts LLM tool params to WIQL WHERE clause."""
        wiql_where = _build_wiql_where(query=query, state=state, type=type, area_path=area_path, id=id)
        return await tool_query_workitems(wiql_where=wiql_where, top=top)

    async def _compute_kpi_adapter(query: str = "", state: str = "", type: str = "", area_path: str = "", group_by: str = "", kpi_type: str = "count", **kwargs):
        """Adapter: same friendly filters as query_workitems, then compute KPIs."""
        wiql_where = _build_wiql_where(query=query, state=state, type=type, area_path=area_path)
        return await tool_compute_kpi(wiql_where=wiql_where, group_by=group_by or None, kpi_type=kpi_type or "count")

    from tools_devops import tool_query_hierarchy

    async def _create_workitem_adapter(
        confirmed: bool = False,
        confirmation_token: str = "",
        conv_id: str = "",
        user_sub: str = "",
        **fields,
    ):
        """Two-step write guard for create_workitem.

        First call (confirmed falsy) never writes: it issues a confirmation
        token bound to this conversation/user and returns a preview. Only the
        second call (confirmed=True + matching token) reaches tool_create_workitem,
        which validates/consumes the token and performs the DevOps write.
        """
        allowed = ("work_item_type", "title", "description", "acceptance_criteria", "area_path", "assigned_to", "tags")
        payload = {k: fields[k] for k in allowed if fields.get(k) is not None}
        if not str(payload.get("title", "")).strip():
            return {"error": "Título é obrigatório"}
        if not confirmed:
            token = issue_create_workitem_confirmation_token(conv_id, user_sub)
            return {
                "needs_confirmation": True,
                "confirmation_token": token,
                "proposed": payload,
                "message": (
                    "Pré-visualização do work item. Nada foi criado ainda. "
                    "Mostra estes detalhes ao utilizador e pede confirmação explícita. "
                    "Só depois de ele confirmar, chama create_workitem de novo com "
                    "confirmed=true e este confirmation_token."
                ),
            }
        return await tool_create_workitem(
            confirmed=True,
            confirmation_token=confirmation_token,
            conv_id=conv_id,
            user_sub=user_sub,
            **payload,
        )

    register_tool("query_workitems", _query_workitems_adapter, TOOL_QUERY_WORKITEMS)
    register_tool("query_hierarchy", tool_query_hierarchy, TOOL_QUERY_HIERARCHY)
    register_tool("generate_user_stories", tool_generate_user_stories, TOOL_GENERATE_USER_STORIES)
    register_tool("create_workitem", _create_workitem_adapter, TOOL_CREATE_WORKITEM)
    register_tool("refine_workitem", tool_refine_workitem, TOOL_REFINE_WORKITEM)
    register_tool("compute_kpi", _compute_kpi_adapter, TOOL_COMPUTE_KPI)
    register_tool("analyze_patterns", tool_analyze_patterns_with_llm, TOOL_ANALYZE_PATTERNS)

    async def _search_workitems_adapter(query: str = "", top: int = 10, **kwargs):
        """Semantic search over the Lakebase work-item index."""
        from tools_knowledge import tool_search_workitems
        return await tool_search_workitems(query, top=top)

    register_tool("search_workitems", _search_workitems_adapter, TOOL_SEARCH_WORKITEMS)
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
    """Outlook email draft + bulk email classification."""
    from tools_email import tool_prepare_outlook_draft, tool_classify_uploaded_emails

    register_tool("prepare_outlook_draft", tool_prepare_outlook_draft, TOOL_PREPARE_EMAIL_DRAFT)
    register_tool("classify_uploaded_emails", tool_classify_uploaded_emails, TOOL_CLASSIFY_UPLOADED_EMAILS)
    logger.info("[Registry] Email tools registered")


def _register_learning_tools():
    """Writer-profile memory (personalization of generated stories)."""
    from tools_learning import tool_get_writer_profile, tool_save_writer_profile

    register_tool("get_writer_profile", tool_get_writer_profile, TOOL_GET_WRITER_PROFILE)
    register_tool("save_writer_profile", tool_save_writer_profile, TOOL_SAVE_WRITER_PROFILE)
    logger.info("[Registry] Learning tools registered")


def _register_subagent_tools():
    """delegate_task — hand a heavy sub-task to one or several isolated sub-agents."""
    from subagent import run_subagent, run_subagents_parallel  # lazy import (avoid cycle)

    async def _delegate_adapter(task: str = "", context: str = "", agent_type: str = "general",
                                tasks=None, conv_id: str = "", user_sub: str = "", **_):
        if tasks:  # several independent sub-tasks -> run in parallel
            return await run_subagents_parallel(tasks, conv_id=conv_id, user_sub=user_sub, depth=0)
        return await run_subagent(task, context, agent_type=agent_type,
                                  conv_id=conv_id, user_sub=user_sub, depth=0)

    register_tool("delegate_task", _delegate_adapter, TOOL_DELEGATE_TASK)
    logger.info("[Registry] Sub-agent tool registered")
