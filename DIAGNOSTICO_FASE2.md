# Diagnóstico Fase 2 — Inventário de migração (original → Databricks)

> Comparação entre `dbde_ai_assistant-main` (Azure, original) e `dbde_databricks`
> (este repo). Objetivo do projeto: **correr no Databricks com o mínimo de
> dependências / menor risco**. Data: 2026-06-05.

## Sumário executivo

| Métrica | Valor |
|---|---|
| Módulos `.py` no original (raiz) | 80 |
| Módulos no databricks (raiz) | 35 |
| Já migrados / partilhados | 29 + 5 renomeados (`*_databricks`) |
| **Por avaliar/migrar** | **46 módulos (~22 750 linhas)** |
| **Dependências novas exigidas** | **0** (tudo usa fastapi/httpx/pandas/openpyxl, já presentes) |

**Conclusão-chave:** o obstáculo da Fase 2 **não são dependências** (não há
nenhuma nova) — é o trabalho de **adaptar o acoplamento a Azure** (storage/LLM/
auth) para os equivalentes Databricks já existentes. Vários blocos com muitas
linhas (`agent.py`, `tools.py`) estão **provavelmente já cobertos** pelo refactor
databricks e não precisam de migração.

## Pré-requisito comum (pequeno)

Os módulos do original importam alguns símbolos que faltam nos módulos
`*_databricks`. São *shims* finos (poucas linhas):

- `storage_databricks`: `StorageOperationError`, `ensure_tables_exist`,
  `ensure_blob_containers`, `init_http_client` (no-ops ou aliases).
- `llm_provider_databricks`: `close_all_providers`, `get_provider_for_spec`.

(Já existem: `table_merge`, `blob_download_json`, `blob_upload_json`,
`parse_blob_ref`, `llm_simple`, `llm_with_fallback`, etc.)

## Inventário por bloco

| # | Bloco | Módulos | Linhas | Acoplam. Azure | Deps novas | Esforço | Valor | Recomendação |
|---|---|---|---:|---|---|---|---|---|
| 1 | **Apresentações avançadas** | presentation_briefing/orchestrator/pipeline/planning/reasoning/review/runtime/sources (8) | ~2 240 | **Muito baixo** (0–2) | 0 | **Baixo–médio** | **Alto** (tool degradada hoje) | ✅ **Migrar 1.º** |
| 2 | **User Story Lane** | user_story_lane + story_* (11) | ~6 360 | **Alto** (75 refs só na USL) | 0 | **Alto** | Alto p/ PO | ⏳ Migrar depois (bloco grande, faseado) |
| 3 | **Routes admin/auth/digest** | routes_admin, routes_auth, routes_digest, route_deps, auth_runtime (5) | ~2 920 | Médio–alto | 0 | Médio | Auth pode ser exigido p/ banco | 🔶 Avaliar (ver auth/SSO) |
| 4 | **Agent core / tools** | agent.py, tools.py (2) | ~7 470 | Alto | 0 | — | — | ⛔ **Provavelmente já coberto** (agent loop em `routes_chat_databricks`, tools em `tools_*`) — só verificar lacunas |
| 5 | **Workers assíncronos** | export_worker, upload_worker, worker_entrypoint, worker_health_server, job_store (5) | ~514 | Médio | 0 | Médio | Baixo (1 vCPU, app single-process) | ⛔ Saltar (contra "mínimo"; ingestão já é inline) |
| 6 | **Privacidade/segurança** | field_encryption, pii_column_scanner, privacy_service, provider_governance (4) | ~886 | Médio | 0 | Médio | Compliance? | 🔶 Só se exigido (PII é no-op por design) |
| 7 | **Quotas / rate-limit** | rate_limit_storage, token_quota (2) | ~365 | Médio | 0 | Baixo | Médio (sem multi-user hoje) | 🔶 Opcional |
| 8 | **Métricas / monitorização** | agent_metrics, tool_metrics (2) | ~265 | Baixo | 0 | Baixo | Médio (é Priority #6) | 🔶 Opcional (nice-to-have) |
| 9 | **Diversos** | document_intelligence (OCR), figma_story_map, mdse_email_journal, speech_prompt, upload_helpers, upload_scan, start_server (7) | ~1 710 | Variável | 0 | Variável | Baixo–médio | ⛔ Maioritariamente saltar (OCR/speech já fora de scope; pdf já inline) |

## Recomendação priorizada (alinhada com "menos dependências, menos risco")

1. **Bloco 1 — Apresentações avançadas** → migrar primeiro.
   Razão: a tool `generate_presentation` está hoje degradada (fallback simples),
   o acoplamento a Azure é quase nulo (basta re-apontar imports para
   `*_databricks` + os shims), **0 dependências novas**, alto valor visível.
2. **Bloco 4 — Verificar (não migrar)** agent/tools: confirmar que nada de útil
   se perdeu no refactor. Barato e remove ~7 500 linhas do âmbito.
3. **Bloco 2 — User Story Lane**: migrar a seguir, faseado (é o maior e o mais
   acoplado), se for prioridade do PO.
4. **Blocos 5–9**: saltar por omissão (contra a filosofia "mínimo") salvo
   requisito explícito (ex.: auth/SSO para o banco → reavaliar Bloco 3).

## Próximo passo sugerido

Começar pelo **Bloco 1 (apresentações)** num PR dedicado: migrar os 8 módulos +
shims, ligar o caminho "vnext" em `tools_export.py`, e verificar com a app a
correr. Estimativa: 1 PR auto-contido, sem dependências novas.
