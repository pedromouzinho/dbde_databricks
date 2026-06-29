# =============================================================================
# upload_ingest.py — Extract text from uploaded files, chunk, embed and index
# =============================================================================
# Replaces the Azure `upload_worker.py` ingestion pipeline. Runs inline (the
# files are small) and stores semantic chunks in Lakebase so that
# `tools_upload.tool_search_uploaded_document` can find them.
#
# Index contract (consumed by tools_upload.py / tools_email.py):
#   table "UploadIndex" row:
#     PartitionKey = conv_id, RowKey = upload_id
#     UserSub, Filename, HasChunks="true", ChunksBlobRef="upload-chunks/<conv>/<id>.json"
#   blob JSON: {"chunks": [{index, start, end, text, embedding}, ...]}
# =============================================================================

import base64
import io
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Any

from config_databricks import IMAGE_INGEST_OCR_ENABLED, LLM_TIER_VISION
from storage_databricks import blob_upload_json, table_insert
from tools_knowledge import get_embedding

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1200          # characters per chunk
CHUNK_OVERLAP = 150        # character overlap between consecutive chunks
MAX_CHUNKS = 200           # safety cap per document
CHUNKS_CONTAINER = "upload-chunks"

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_IMAGE_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
# Skip vision transcription for images bigger than this (Databricks rejects very
# large images; ~8 MB also bounds the base64 payload).
_OCR_MAX_BYTES = 8_000_000
_OCR_PROMPT = (
    "Transcreve e descreve o conteudo desta imagem em PT-PT, de forma estruturada e "
    "fiel. Inclui TODO o texto visivel (titulos, labels, botoes, campos, valores) e "
    "uma breve descricao do que o ecra mostra. Nao inventes nada que nao esteja na imagem."
)


def _is_image_filename(filename: str) -> bool:
    return os.path.splitext(str(filename or ""))[1].lower() in _IMAGE_EXTS


# -----------------------------------------------------------------------------
# Text extraction
# -----------------------------------------------------------------------------

def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _extract_pdf(data: bytes) -> str:
    # Prefer pdfplumber (better layout), fall back to pypdf.
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                out.append(page.extract_text() or "")
        text = "\n".join(out).strip()
        if text:
            return text
    except Exception as e:
        logger.warning("[Ingest] pdfplumber failed, trying pypdf: %s", e)
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        logger.warning("[Ingest] pypdf failed: %s", e)
        return ""


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text).strip()
    except Exception as e:
        logger.warning("[Ingest] docx extraction failed: %s", e)
        return ""


def _extract_tabular(data: bytes, filename: str) -> str:
    try:
        from tabular_loader import load_tabular_dataset
        ds = load_tabular_dataset(data, filename)
        columns = ds.get("columns", []) or []
        records = ds.get("records", []) or []
        lines = [" | ".join(str(c) for c in columns)]
        for rec in records:
            lines.append(" | ".join(f"{c}: {rec.get(c, '')}" for c in columns))
        return "\n".join(lines).strip()
    except Exception as e:
        logger.warning("[Ingest] tabular extraction failed: %s", e)
        return ""


def extract_text(data: bytes, filename: str) -> str:
    """Best-effort plain-text extraction from an uploaded file."""
    name = (filename or "").lower()
    try:
        from tabular_loader import is_tabular_filename
        if is_tabular_filename(filename):
            return _extract_tabular(data, filename)
    except Exception:
        pass
    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith(".docx"):
        return _extract_docx(data)
    # txt / md / json / csv-like / unknown -> decode as text
    return _decode_text(data)


async def extract_image_text(data: bytes, filename: str) -> str:
    """Transcribe an uploaded image into searchable text via the Databricks vision
    endpoint (ciclo-fechado — internal serving endpoint). Returns "" on any failure
    or when disabled, so the caller degrades gracefully (no indexing)."""
    if not IMAGE_INGEST_OCR_ENABLED:
        return ""
    if not data or len(data) > _OCR_MAX_BYTES:
        return ""
    try:
        from llm_provider_databricks import llm_with_fallback
        mime = _IMAGE_MIME_BY_EXT.get(os.path.splitext(filename or "")[1].lower(), "image/png")
        b64 = base64.b64encode(data).decode("ascii")
        resp = await llm_with_fallback(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            tier=LLM_TIER_VISION,
            max_tokens=1500,
            temperature=0.0,
        )
        return (resp.content or "").strip()
    except Exception as e:
        logger.warning("[Ingest] image vision transcription failed: %s", e)
        return ""


# -----------------------------------------------------------------------------
# Chunking
# -----------------------------------------------------------------------------

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: List[Dict[str, Any]] = []
    start = 0
    n = len(text)
    step = max(1, size - overlap)
    idx = 0
    while start < n and idx < MAX_CHUNKS:
        end = min(start + size, n)
        piece = text[start:end].strip()
        if piece:
            chunks.append({"index": idx, "start": start, "end": end, "text": piece})
            idx += 1
        start += step
    return chunks


# -----------------------------------------------------------------------------
# Ingestion (extract -> chunk -> embed -> store)
# -----------------------------------------------------------------------------

async def ingest_upload(conv_id: str, upload_id: str, filename: str, data: bytes, user_sub: str = "") -> Dict[str, Any]:
    """Extract, chunk, embed and index an uploaded file. Returns a summary dict."""
    conv_id = (conv_id or "").strip()
    if not conv_id:
        return {"indexed": False, "reason": "conv_id required"}

    # Images can't be plain-text extracted: transcribe them via the vision endpoint
    # so screenshots (e.g. a user-story mockup) become searchable like any document.
    if _is_image_filename(filename):
        text = await extract_image_text(data, filename)
    else:
        text = extract_text(data, filename)
    chunks = chunk_text(text)
    if not chunks:
        return {"indexed": False, "reason": "no extractable text", "chars": len(text or "")}

    # Embed each chunk (best effort; drop chunks whose embedding failed).
    embedded: List[Dict[str, Any]] = []
    for chunk in chunks:
        emb = await get_embedding(chunk["text"])
        if not emb:
            continue
        chunk["embedding"] = emb
        embedded.append(chunk)

    if not embedded:
        return {"indexed": False, "reason": "embedding failed", "chunks": len(chunks)}

    blob_name = f"{conv_id}/{upload_id}.json"
    try:
        await blob_upload_json(CHUNKS_CONTAINER, blob_name, {"chunks": embedded})
        await table_insert("UploadIndex", {
            "PartitionKey": conv_id,
            "RowKey": upload_id,
            "UserSub": user_sub or "",
            "Filename": filename or "",
            "HasChunks": "true",
            "ChunksBlobRef": f"{CHUNKS_CONTAINER}/{blob_name}",
            "ChunkCount": len(embedded),
            "UploadedAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("[Ingest] persist failed: %s", e)
        return {"indexed": False, "reason": f"persist failed: {str(e)[:120]}", "chunks": len(embedded)}

    return {"indexed": True, "chunks": len(embedded), "filename": filename}
