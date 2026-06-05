# =============================================================================
# tools_upload.py — Uploaded document semantic search
# =============================================================================

import json
import logging

from config_databricks import UPLOAD_INDEX_TOP
from storage_databricks import table_query, blob_download_json, parse_blob_ref
from tools_knowledge import get_embedding, _cosine_similarity
from utils import odata_escape

logger = logging.getLogger(__name__)


async def _load_indexed_chunks(conv_id: str, user_sub: str = ""):
    safe_conv = odata_escape(str(conv_id or "").strip())
    if not safe_conv:
        return []
    safe_user = str(user_sub or "").strip()
    try:
        rows = await table_query("UploadIndex", f"PartitionKey eq '{safe_conv}'", top=max(1, min(UPLOAD_INDEX_TOP, 500)))
    except Exception as e:
        logging.error("[Tools] _load_indexed_chunks table query failed: %s", e)
        rows = []
    chunk_pool = []
    for row in rows:
        owner_sub = str(row.get("UserSub", "") or "")
        if safe_user:
            # Segurança: impedir leitura de chunks de outros utilizadores.
            if not owner_sub or owner_sub != safe_user:
                continue
        has_chunks = str(row.get("HasChunks", "")).lower() in ("true", "1")
        if not has_chunks:
            continue
        filename = str(row.get("Filename", "") or "")
        chunk_ref = str(row.get("ChunksBlobRef", "") or "")
        container, blob_name = parse_blob_ref(chunk_ref)
        if not container or not blob_name:
            continue
        try:
            payload = await blob_download_json(container, blob_name)
        except Exception as e:
            logging.warning("[Tools] _load_indexed_chunks blob read failed for %s: %s", chunk_ref, e)
            continue
        chunks = []
        if isinstance(payload, dict):
            chunks = payload.get("chunks", []) if isinstance(payload.get("chunks"), list) else []
        if not chunks:
            continue
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_pool.append((filename, chunk))
    return chunk_pool

def _resolve_uploaded_files_memory(conv_id: str = "", user_sub: str = ""):
    try:
        from agent import uploaded_files_store  # import lazy para evitar ciclo no import-time
    except Exception as e:
        logging.error("[Tools] search_uploaded_document cannot import uploaded_files_store: %s", e)
        return None, []

    requested = (conv_id or "").strip()
    safe_user = str(user_sub or "").strip()
    if requested:
        raw = uploaded_files_store.get(requested)
        if isinstance(raw, dict) and isinstance(raw.get("files"), list):
            files = raw.get("files", [])
            if safe_user:
                files = [f for f in files if str((f or {}).get("user_sub", "") or "") == safe_user]
            return requested, files
        if isinstance(raw, dict) and raw:
            files = [raw]
            if safe_user:
                files = [f for f in files if str((f or {}).get("user_sub", "") or "") == safe_user]
            return requested, files
        return requested, []
    return None, []

async def tool_search_uploaded_document(query: str = "", conv_id: str = "", user_sub: str = ""):
    q = (query or "").strip()
    if not q:
        return {"error": "query é obrigatório"}

    resolved_conv_id = (conv_id or "").strip()
    if not resolved_conv_id:
        return {"error": "conv_id é obrigatório para pesquisa em documento carregado"}

    safe_user = str(user_sub or "").strip()
    chunk_pool = await _load_indexed_chunks(resolved_conv_id, user_sub=safe_user)

    # Fallback retrocompatível: memória local (deploy antigo / jobs ainda sem indexação persistida).
    source = "upload_index"
    if not chunk_pool:
        source = "memory_fallback"
        _, files = _resolve_uploaded_files_memory(resolved_conv_id, user_sub=safe_user)
        for file_data in files:
            chunks = file_data.get("chunks")
            if not isinstance(chunks, list) or not chunks:
                continue
            fname = file_data.get("filename", "")
            for chunk in chunks:
                chunk_pool.append((fname, chunk))

    if not chunk_pool:
        return {"error": "Nenhum documento com chunks semânticos indexados nesta conversa."}

    query_embedding = await get_embedding(q)
    if not query_embedding:
        return {"error": "Falha ao calcular embedding da query"}

    scored = []
    for filename, chunk in chunk_pool:
        chunk_embedding = chunk.get("embedding")
        try:
            score = _cosine_similarity(query_embedding, chunk_embedding)
        except Exception as e:
            logging.warning("[Tools] search_uploaded_document chunk score failed: %s", e)
            continue
        if score < 0:
            continue
        scored.append(
            {
                "filename": filename,
                "chunk_index": chunk.get("index"),
                "start": chunk.get("start"),
                "end": chunk.get("end"),
                "score": score,
                "text": chunk.get("text", ""),
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_chunks = scored[:5]
    for item in top_chunks:
        item["score"] = round(item["score"], 4)

    return {
        "source": source,
        "conversation_id": resolved_conv_id,
        "filenames": sorted(list({f for f, _ in chunk_pool if f})),
        "query": q,
        "total_chunks": len(chunk_pool),
        "total_results": len(top_chunks),
        "items": top_chunks,
    }
