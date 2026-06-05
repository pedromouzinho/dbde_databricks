from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from storage_databricks import table_insert, table_merge, table_query
from utils import odata_escape

logger = logging.getLogger(__name__)
_TABLE_NAME = "DataDictionary"
_TABLE_NAME_SANITIZER = re.compile(r"[^a-z0-9_]+")


def normalize_table_name(table_name: str) -> str:
    raw = os.path.basename(str(table_name or "").strip())
    stem, _ = os.path.splitext(raw)
    normalized = _TABLE_NAME_SANITIZER.sub("_", stem.lower()).strip("_")
    return normalized or "unknown_table"


def build_dictionary_scope(table_name: str, owner_sub: str = "") -> str:
    table_scope = normalize_table_name(table_name)
    owner_scope = str(owner_sub or "").strip() or "global"
    return f"{owner_scope}::{table_scope}"


def _make_row_key(pivot_value: str, column_name: str) -> str:
    safe_pivot = str(pivot_value or "__global__").strip() or "__global__"
    safe_column = str(column_name or "").strip() or "unknown_column"
    return f"{safe_pivot}::{safe_column}"


async def save_mapping(
    table_name: str,
    *,
    pivot_column: str,
    pivot_value: str,
    column_name: str,
    mapped_name: str,
    description: str = "",
    data_type: str = "",
    updated_by: str = "",
    owner_sub: str = "",
) -> bool:
    normalized_table = build_dictionary_scope(table_name, owner_sub=owner_sub)
    safe_pivot_value = str(pivot_value or "__global__").strip() or "__global__"
    safe_column = str(column_name or "").strip() or "unknown_column"
    entity = {
        "PartitionKey": normalized_table,
        "RowKey": _make_row_key(safe_pivot_value, safe_column),
        "PivotColumn": str(pivot_column or "").strip(),
        "PivotValue": safe_pivot_value,
        "ColumnName": safe_column,
        "MappedName": str(mapped_name or "").strip(),
        "Description": str(description or "").strip()[:2000],
        "DataType": str(data_type or "").strip(),
        "UpdatedAt": datetime.now(timezone.utc).isoformat(),
        "UpdatedBy": str(updated_by or "").strip(),
    }
    existing = await table_query(
        _TABLE_NAME,
        (
            f"PartitionKey eq '{odata_escape(normalized_table)}' and "
            f"RowKey eq '{odata_escape(entity['RowKey'])}'"
        ),
        top=1,
    )
    if existing:
        return await table_merge(_TABLE_NAME, entity) is not False
    return bool(await table_insert(_TABLE_NAME, entity))


async def save_mappings_batch(
    table_name: str,
    mappings: list[dict[str, Any]],
    *,
    pivot_column: str = "",
    updated_by: str = "",
    owner_sub: str = "",
) -> int:
    saved_count = 0
    for mapping in mappings or []:
        if not isinstance(mapping, dict):
            continue
        pivot_value = str(mapping.get("pivot_value", "__global__") or "__global__").strip() or "__global__"
        column_name = str(mapping.get("column_name", "") or "").strip()
        mapped_name = str(mapping.get("mapped_name", "") or "").strip()
        if not column_name or not mapped_name:
            continue
        ok = await save_mapping(
            table_name,
            pivot_column=str(mapping.get("pivot_column", "") or pivot_column or "").strip(),
            pivot_value=pivot_value,
            column_name=column_name,
            mapped_name=mapped_name,
            description=str(mapping.get("description", "") or "").strip(),
            data_type=str(mapping.get("data_type", "") or "").strip(),
            updated_by=updated_by,
            owner_sub=owner_sub,
        )
        if ok:
            saved_count += 1
    return saved_count


async def get_dictionary(table_name: str, pivot_value: str = "", top: int = 500, owner_sub: str = "") -> list[dict]:
    normalized_table = build_dictionary_scope(table_name, owner_sub=owner_sub)
    filters = [f"PartitionKey eq '{odata_escape(normalized_table)}'"]
    if pivot_value:
        filters.append(f"PivotValue eq '{odata_escape(str(pivot_value or '').strip())}'")
    rows = await table_query(_TABLE_NAME, " and ".join(filters), top=max(1, min(int(top or 500), 1000)))
    return [
        {
            "pivot_value": str(row.get("PivotValue", "") or ""),
            "column_name": str(row.get("ColumnName", "") or ""),
            "mapped_name": str(row.get("MappedName", "") or ""),
            "description": str(row.get("Description", "") or ""),
            "data_type": str(row.get("DataType", "") or ""),
            "pivot_column": str(row.get("PivotColumn", "") or ""),
        }
        for row in rows or []
    ]


def format_dictionary_for_prompt(entries: list[dict], table_name: str = "", max_items: int = 80) -> str:
    if not entries:
        return ""
    trimmed = [entry for entry in (entries or []) if isinstance(entry, dict)][:max(1, max_items)]
    if not trimmed:
        return ""

    lines = [f"## Dicionário de dados para {normalize_table_name(table_name) if table_name else 'tabela'}"]
    global_entries = [
        entry
        for entry in trimmed
        if str(entry.get("pivot_value", "") or "").strip() in ("", "__global__")
    ]
    if global_entries:
        lines.append("")
        lines.append("### Mapeamentos globais")
        for entry in global_entries:
            lines.append(_format_mapping_line(entry))

    grouped: defaultdict[str, list[dict]] = defaultdict(list)
    for entry in trimmed:
        pivot_value = str(entry.get("pivot_value", "") or "").strip()
        if pivot_value in ("", "__global__"):
            continue
        grouped[pivot_value].append(entry)
    for pivot_value in sorted(grouped):
        entries_for_pivot = grouped[pivot_value]
        pivot_column = str(entries_for_pivot[0].get("pivot_column", "") or "pivot").strip() or "pivot"
        lines.append("")
        lines.append(f"### {pivot_column}={pivot_value}")
        for entry in entries_for_pivot:
            lines.append(_format_mapping_line(entry))
    return "\n".join(lines)


def _format_mapping_line(entry: dict) -> str:
    column_name = str(entry.get("column_name", "") or "").strip()
    mapped_name = str(entry.get("mapped_name", "") or "").strip()
    description = str(entry.get("description", "") or "").strip()
    data_type = str(entry.get("data_type", "") or "").strip()
    suffix = []
    if data_type:
        suffix.append(f"type={data_type}")
    if description:
        suffix.append(description)
    extra = f" ({'; '.join(suffix)})" if suffix else ""
    return f"- {column_name} = **{mapped_name}**{extra}"
