# DBDE AI Assistant — Migration Guide: Azure → Databricks

## TL;DR

Migração do assistente AI de Azure App Service + Azure OpenAI + Azure Table Storage
para Databricks Apps + Foundation Model APIs + Lakebase (Postgres).

---

## Arquitectura Comparada

| Camada | Azure (original) | Databricks (novo) |
|--------|-------------------|-------------------|
| LLM Inference | Azure OpenAI (GPT-4.1) + Anthropic API | Foundation Model APIs (Claude Opus/Sonnet/Haiku, GPT OSS) |
| Embeddings | text-embedding-3-small (Azure) | databricks-gte-large-en (1024 dim) |
| App Runtime | Azure App Service | Databricks Apps (FastAPI) |
| State Storage | Azure Table Storage (REST) | Lakebase (Managed Postgres) |
| Blob Storage | Azure Blob Storage | Lakebase BYTEA + UC Volumes |
| Search/RAG | Azure AI Search + Reranking | Lakebase pgvector + embedding endpoint |
| Auth | Azure AD + custom | Workspace SSO (auto) |
| Secrets | Azure App Settings / KeyVault | Databricks Secrets (DataBricksKVScopeDataQ) |
| External APIs | DevOps, Figma, Miro, Email | Mesmos (httpx, tokens em secrets) |

---

## Modelos Disponíveis no Workspace

| Endpoint | Modelo | Use Case |
|----------|--------|----------|
| databricks-claude-opus-4-8 | Claude Opus 4.8 | Máxima capacidade (tier=pro) |
| databricks-claude-sonnet-4-6 | Claude Sonnet 4.6 | Balanceado (tier=standard) |
| databricks-claude-haiku-4-5 | Claude Haiku 4.5 | Rápido/barato (tier=fast) |
| databricks-gpt-oss-120b | GPT OSS 120B | Reasoning (alternativa) |
| databricks-llama-4-maverick | Llama 4 Maverick | Open-source (400B MoE) |
| databricks-gte-large-en | GTE Large | Embeddings (1024 dim) |

Todos suportam:
- ✅ Chat completions (OpenAI-compatible)
- ✅ Tool/function calling
- ✅ Streaming (SSE)
- ✅ Vision/multimodal (Sonnet, Opus)

---

## Ficheiros Criados

```
dbde-assistant-databricks/
├── app.yaml                     # Databricks App manifest (recursos, env, comando)
├── app.py                       # FastAPI entrypoint (lifespan, routes)
├── config_databricks.py         # Configuração (substitui config.py)
├── llm_provider_databricks.py   # LLM client (substitui llm_provider.py)
├── storage_databricks.py        # Lakebase storage (substitui storage.py)
├── routes_chat_databricks.py    # Chat API + agent loop
├── tool_registry_databricks.py  # Tool registry + DevOps tools
├── requirements.txt             # Dependencies (sem Azure SDKs)
└── MIGRATION_GUIDE.md           # Este ficheiro
```

---

## Plano de Migração Passo-a-Passo

### Fase 1: MVP Funcional (1-2 dias)

1. **Deploy da App** (já preparado)
   - `app.yaml` define recursos (LLM endpoints + Lakebase)
   - `app.py` inicializa pool Postgres + regista tools
   - Testar com `/health` e `/api/chat`

2. **Validar LLM** (✅ confirmado)
   - Chat completions: funciona
   - Tool calling: funciona
   - Streaming: funciona
   - Embeddings: funciona (1024 dim)

3. **Validar Storage**
   - Lakebase cria tabelas automaticamente no startup
   - Schema compatível com Azure Table Storage (PartitionKey/RowKey/JSONB)

### Fase 2: Tools Externas (2-3 dias)

4. **Azure DevOps** (já implementado no registry)
   - Adicionar PAT ao KeyVault: `devops-pat`
   - Tools: query_workitems, generate_user_stories

5. **Figma / Miro** (copiar do original)
   - `tools_figma.py` e `tools_miro.py` funcionam sem alterações
   - Apenas precisam tokens no KeyVault: `figma-token`, `miro-token`

6. **Email** (copiar do original)
   - `tools_email.py` precisa de Microsoft Graph token
   - Adicionar ao KeyVault: `graph-token`

### Fase 3: RAG / Knowledge Search (1 semana)

7. **Embeddings + Vector Search**
   - Opção A: Lakebase + pgvector (já preparado no schema)
   - Opção B: Databricks Vector Search (mais escalável)
   - Upload de documentos → chunk → embed → store

8. **Migrar índices existentes**
   - Exportar Azure AI Search indexes (DevOps, Omni)
   - Re-indexar com GTE Large embeddings

### Fase 4: Features Avançadas (2+ semanas)

9. **Presentation Pipeline**
   - `presentation_*.py` modules (pptx_engine, xlsx_engine)
   - Dependem apenas de python-pptx/openpyxl — funcionam as-is

10. **PII Shield**
    - `pii_shield.py` é regex-based — funciona sem alterações
    - Complementar com Unity Catalog column masking

11. **Frontend**
    - Copiar `frontend/` para o Databricks App
    - Vite build → servir static files via FastAPI

---

## Mapeamento de Secrets (KeyVault → app.yaml)

| Secret Name | Uso | Obrigatório |
|-------------|-----|---|
| devops-pat | Azure DevOps Personal Access Token | Se DevOps tools activas |
| figma-token | Figma API access token | Se Figma tool activa |
| miro-token | Miro API access token | Se Miro tool activa |
| graph-token | Microsoft Graph (email) | Se Email tool activa |

---

## O Que NÃO Precisa de Migração

Estes módulos do original funcionam as-is no Databricks:
- `code_interpreter.py` — subprocess Python (melhor ainda com compute Databricks)
- `pii_shield.py` / `prompt_shield.py` — regex/heuristic based
- `token_counter.py` — usa tiktoken
- `export_engine.py` / `pptx_engine.py` / `xlsx_engine.py` — file generation
- `tools_figma.py` / `tools_miro.py` — API calls via httpx
- `tabular_loader.py` / `tabular_artifacts.py` — pandas/duckdb
- `presentation_*.py` — todo o pipeline de apresentações

---

## Comandos de Deploy

```bash
# 1. Criar a App (se ainda não existe)
databricks apps create dbde-assistant \
  --description "DBDE AI Assistant powered by Databricks" \
  --source-code-path /Workspace/Users/x251367@bcpcorp.net/dbde-assistant-databricks

# 2. Deploy
databricks apps deploy dbde-assistant \
  --source-code-path /Workspace/Users/x251367@bcpcorp.net/dbde-assistant-databricks

# 3. Verificar
databricks apps get dbde-assistant

# 4. Logs
databricks apps logs dbde-assistant
```

---

## Vantagens da Migração

1. **Custo**: Modelos servidos pela Databricks (pay-per-token), sem Azure OpenAI deployment dedicado
2. **Latency**: Endpoints no mesmo datacenter que a App
3. **Governance**: Tokens, usage, audit trail via Databricks
4. **Escala**: Auto-scaling nativo, sem App Service plan management
5. **Modelos**: Acesso a Claude Opus/Sonnet/Haiku + Llama + GPT OSS (melhor que só Azure OpenAI)
6. **Storage**: Lakebase (Postgres) é SQL real, não key-value limitado
7. **Simplicidade**: Menos infra para gerir (tudo numa plataforma)
