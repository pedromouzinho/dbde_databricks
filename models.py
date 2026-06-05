# =============================================================================
# models.py — Modelos Pydantic do Assistente AI DBDE
# =============================================================================
# Todos os request/response models centralizados com limites defensivos.
# =============================================================================

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


MAX_ID_LEN = 128
MAX_USER_LEN = 80
MAX_DISPLAY_NAME_LEN = 120
MAX_PASSWORD_LEN = 256
MAX_FILENAME_LEN = 255
MAX_MIME_LEN = 100
MAX_SHORT_TEXT_LEN = 512
MAX_MEDIUM_TEXT_LEN = 4096
MAX_LONG_TEXT_LEN = 20000
MAX_RESPONSE_TEXT_LEN = 500000
MAX_BASE64_IMAGE_LEN = 8_000_000
MAX_EXPORT_BLOB_REF_LEN = 500
MAX_MESSAGES_PER_SAVE = 4000
MAX_TOOL_ITEMS = 200
MAX_BLOB_REFS = 50


# =============================================================================
# AGENT (principal)
# =============================================================================

class ChatImageInput(BaseModel):
    base64: str = Field(min_length=1, max_length=MAX_BASE64_IMAGE_LEN)
    content_type: Optional[str] = Field(default="image/png", max_length=MAX_MIME_LEN)
    filename: Optional[str] = Field(default=None, max_length=MAX_FILENAME_LEN)


class AgentChatRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    question: str = Field(min_length=1, max_length=MAX_LONG_TEXT_LEN)
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    image_base64: Optional[str] = Field(default=None, max_length=MAX_BASE64_IMAGE_LEN)
    image_content_type: Optional[str] = Field(default="image/png", max_length=MAX_MIME_LEN)
    images: Optional[List[ChatImageInput]] = Field(default=None, max_length=10)
    mode: Optional[Literal["general", "userstory"]] = "general"
    model_tier: Optional[Literal["fast", "standard", "pro"]] = None


class AgentChatResponse(BaseModel):
    model_config = {"protected_namespaces": ()}
    answer: str = Field(max_length=MAX_RESPONSE_TEXT_LEN)
    conversation_id: str = Field(max_length=MAX_ID_LEN)
    request_id: str = Field(default="", max_length=MAX_ID_LEN)
    request_status: Literal["ok", "blocked", "error"] = "ok"
    request_error_kind: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    tools_used: List[str] = Field(default_factory=list, max_length=MAX_TOOL_ITEMS)
    tool_details: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_TOOL_ITEMS)
    tokens_used: Dict[str, Any] = Field(default_factory=dict)
    total_time_ms: int = 0
    model_used: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    is_fallback: bool = False
    mode: Literal["general", "userstory"] = "general"
    has_exportable_data: bool = False
    export_index: Optional[int] = None


# =============================================================================
# STREAMING EVENTS (SSE)
# =============================================================================

class StreamEvent(BaseModel):
    """Evento SSE enviado ao frontend durante streaming."""
    type: Literal["thinking", "tool_start", "tool_result", "token", "done", "error"]
    text: Optional[str] = Field(default=None, max_length=MAX_RESPONSE_TEXT_LEN)
    tool: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    data: Optional[Dict[str, Any]] = None


# =============================================================================
# AUTH
# =============================================================================

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USER_LEN)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USER_LEN)
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_LEN)
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LEN)
    role: Literal["user", "admin"] = "user"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)
    new_password: str = Field(min_length=8, max_length=MAX_PASSWORD_LEN)


# =============================================================================
# MODE SWITCHING
# =============================================================================

class ModeSwitchRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    mode: Literal["general", "userstory"]


class ModeSwitchResponse(BaseModel):
    success: bool
    mode: Literal["general", "userstory"]
    conversation_id: str = Field(max_length=MAX_ID_LEN)
    message: str = Field(default="", max_length=MAX_MEDIUM_TEXT_LEN)


# =============================================================================
# CHAT PERSISTENCE
# =============================================================================

class SaveChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=MAX_USER_LEN)
    conversation_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    title: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    messages: list = Field(default_factory=list, max_length=MAX_MESSAGES_PER_SAVE)


class UpdateChatTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_SHORT_TEXT_LEN)


class PrivacyDeleteRequest(BaseModel):
    confirmation: Literal["DELETE_MY_DATA"] = "DELETE_MY_DATA"
    delete_account: bool = False


# =============================================================================
# FEEDBACK & LEARNING
# =============================================================================

class FeedbackRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    message_index: int
    rating: int = Field(ge=1, le=10)
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class SpeechPromptNormalizeRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    mode: Optional[Literal["general", "userstory"]] = "general"
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    language: Optional[str] = Field(default="pt-PT", max_length=32)


class SpeechPromptNormalizeResponse(BaseModel):
    raw_transcript: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    normalized_prompt: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    confidence: Literal["high", "medium", "low"] = "medium"
    inferred_mode: Literal["general", "userstory"] = "general"
    auto_send_allowed: bool = False
    notes: List[str] = Field(default_factory=list, max_length=10)
    provider_used: Optional[str] = Field(default=None, max_length=160)
    model_used: Optional[str] = Field(default=None, max_length=160)
    provider_policy_mode: Optional[str] = Field(default=None, max_length=32)
    provider_family: Optional[str] = Field(default=None, max_length=64)
    external_provider: bool = False
    data_sensitivity: Optional[str] = Field(default=None, max_length=32)
    provider_policy_note: Optional[str] = Field(default=None, max_length=500)


class SpeechPromptTokenResponse(BaseModel):
    token: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    region: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=1, max_length=32)
    provider: Literal["azure_speech"] = "azure_speech"
    expires_in_seconds: int = Field(default=600, ge=60, le=3600)


class UserStoryWorkspaceRequest(BaseModel):
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    objective: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    team_scope: str = Field(min_length=1, max_length=MAX_SHORT_TEXT_LEN)
    epic_or_feature: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    context: Optional[str] = Field(default=None, max_length=MAX_LONG_TEXT_LEN)
    reference_author: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LEN)
    reference_topic: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)


class UserStoryValidateRequest(BaseModel):
    draft_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    draft: Optional[Dict[str, Any]] = None


class UserStoryPublishRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    area_path: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    assigned_to: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LEN)
    tags: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    final_draft: Optional[Dict[str, Any]] = None


class UserStoryFeedbackEventRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    outcome: Literal["accepted", "edited", "rejected"]
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    final_draft: Optional[Dict[str, Any]] = None


class UserStoryPromoteRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    user_sub: str = Field(min_length=1, max_length=MAX_SHORT_TEXT_LEN)
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class UserStoryCurationReviewRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    action: Literal["approve", "reject", "deactivate", "reactivate"]
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class UserStorySearchSyncRequest(BaseModel):
    draft_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    top: int = Field(default=200, ge=1, le=1000)


class UserStoryDevOpsSyncRequest(BaseModel):
    since_iso: Optional[str] = Field(default=None, max_length=80)
    since_days: int = Field(default=30, ge=1, le=365)
    top: int = Field(default=1200, ge=1, le=5000)
    update_cursor: bool = True


class UserStoryKnowledgeSyncRequest(BaseModel):
    max_docs: int = Field(default=1500, ge=1, le=10000)
    batch_size: int = Field(default=150, ge=1, le=500)
    update_state: bool = True


class UserStoryKnowledgeAssetUploadRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    file_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    title: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    domain: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    journey: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    flow: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    team_scope: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class UserStoryKnowledgeAssetTextRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    content: str = Field(min_length=1, max_length=MAX_LONG_TEXT_LEN)
    asset_key: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    domain: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    journey: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    flow: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    team_scope: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class UserStoryKnowledgeAssetBundleItem(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    content: str = Field(min_length=1, max_length=MAX_LONG_TEXT_LEN)
    asset_key: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    domain: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    journey: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    flow: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    team_scope: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class UserStoryKnowledgeAssetBundleRequest(BaseModel):
    items: List[UserStoryKnowledgeAssetBundleItem] = Field(default_factory=list, max_length=250)


class UserStoryKnowledgeAssetReviewRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    action: Literal["deactivate", "reactivate", "delete"]
    note: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)


class RuleRequest(BaseModel):
    category: str = Field(min_length=1, max_length=MAX_SHORT_TEXT_LEN)
    rule_text: str = Field(min_length=1, max_length=MAX_MEDIUM_TEXT_LEN)
    source: str = Field(default="manual", max_length=MAX_SHORT_TEXT_LEN)


class ClientErrorReport(BaseModel):
    error_type: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=5000)
    stack: Optional[str] = Field(default=None, max_length=10000)
    component: Optional[str] = Field(default=None, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    user_agent: Optional[str] = Field(default=None, max_length=500)
    timestamp: Optional[str] = Field(default=None, max_length=50)
    request_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    agent_mode: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)


# =============================================================================
# LEGACY (retrocompatibilidade com /chat endpoint antigo)
# =============================================================================

class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_LONG_TEXT_LEN)
    index: str = Field(default="devops", max_length=MAX_SHORT_TEXT_LEN)


class Source(BaseModel):
    id: str = Field(max_length=MAX_ID_LEN)
    title: str = Field(max_length=MAX_SHORT_TEXT_LEN)
    status: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    url: str = Field(default="", max_length=MAX_MEDIUM_TEXT_LEN)
    score: float = 0.0


class ChatResponse(BaseModel):
    model_config = {"protected_namespaces": ()}
    answer: str = Field(max_length=MAX_RESPONSE_TEXT_LEN)
    sources: List[Source] = Field(default_factory=list, max_length=MAX_TOOL_ITEMS)
    tokens_used: Dict[str, Any] = Field(default_factory=dict)
    search_time_ms: int = 0
    total_time_ms: int = 0
    index_used: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    model_used: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)


# =============================================================================
# LLM PROVIDER (interno)
# =============================================================================

class LLMToolCall(BaseModel):
    """Formato normalizado de tool call — independente do provider."""
    id: str = Field(max_length=MAX_ID_LEN)
    name: str = Field(max_length=MAX_SHORT_TEXT_LEN)
    arguments: Dict[str, Any]


class LLMResponse(BaseModel):
    """Formato normalizado de resposta LLM — independente do provider."""
    content: Optional[str] = Field(default=None, max_length=MAX_RESPONSE_TEXT_LEN)
    tool_calls: Optional[List[LLMToolCall]] = Field(default=None, max_length=MAX_TOOL_ITEMS)
    usage: Dict[str, int] = Field(default_factory=dict)
    model: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    provider: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    is_error: bool = False
    error_kind: str = Field(default="", max_length=MAX_SHORT_TEXT_LEN)
    error_detail: Optional[str] = Field(default=None, max_length=MAX_RESPONSE_TEXT_LEN)
    fallback_chain: Optional[List[Dict[str, Any]]] = None


# =============================================================================
# EXPORT
# =============================================================================

class ExportRequest(BaseModel):
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    tool_call_index: int = Field(default=-1, ge=-1, le=10000)
    format: Literal["csv", "xlsx", "pdf", "svg", "html", "zip"] = "xlsx"
    chart_type: Optional[Literal["bar", "pie", "sankey"]] = None
    title: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT_LEN)
    summary: Optional[str] = Field(default=None, max_length=MAX_MEDIUM_TEXT_LEN)
    data: Optional[dict] = None
    result_blob_ref: Optional[str] = Field(default=None, max_length=MAX_EXPORT_BLOB_REF_LEN)
    result_blob_refs: Optional[List[str]] = Field(default=None, max_length=MAX_BLOB_REFS)
    prefer_async: bool = True
