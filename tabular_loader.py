from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from binascii import Error as BinasciiError
from base64 import b64decode
from datetime import date, datetime, time
from typing import Iterator

from config_databricks import (
    UPLOAD_MAX_FILE_BYTES,
    UPLOAD_MAX_FILE_BYTES_CSV,
    UPLOAD_MAX_FILE_BYTES_TSV,
    UPLOAD_MAX_FILE_BYTES_XLSX,
    UPLOAD_MAX_FILE_BYTES_XLSB,
    UPLOAD_MAX_FILE_BYTES_XLS,
)

TABULAR_PREVIEW_CHAR_LIMIT = 100_000
TABULAR_PREVIEW_ROW_LIMIT = 200
TABULAR_RECORD_LIMIT = 500_000
SUPPORTED_TABULAR_EXTENSIONS = (".csv", ".tsv", ".xlsx", ".xlsb", ".xls")
_GENERIC_COLUMN_PATTERN = re.compile(
    r"^(campo|field|col|column|attr|param|value|dado|data|var|prop)[_\s]?\d+$",
    re.IGNORECASE,
)
_UUID_VALUE_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$|^[0-9a-f]{32}$",
    re.IGNORECASE,
)
_BASE64_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
_BOOLEAN_VALUES = {"true", "false", "yes", "no", "sim", "nao", "1", "0"}
_POLYMORPHIC_GENERIC_THRESHOLD = 3
_POLYMORPHIC_PIVOT_MAX_DISTINCT = 50
_POLYMORPHIC_PIVOT_MIN_FILL = 0.90
_POLYMORPHIC_EMPTY_COL_THRESHOLD = 0.30

_TABULAR_UPLOAD_LIMITS = {
    ".csv": UPLOAD_MAX_FILE_BYTES_CSV,
    ".tsv": UPLOAD_MAX_FILE_BYTES_TSV,
    ".xlsx": UPLOAD_MAX_FILE_BYTES_XLSX,
    ".xlsb": UPLOAD_MAX_FILE_BYTES_XLSB,
    ".xls": UPLOAD_MAX_FILE_BYTES_XLS,
}


class TabularLoaderError(ValueError):
    pass


def get_tabular_extension(filename: str) -> str:
    return os.path.splitext(str(filename or "").strip().lower())[1]


def is_tabular_filename(filename: str) -> bool:
    return get_tabular_extension(filename) in SUPPORTED_TABULAR_EXTENSIONS


def get_tabular_upload_limit_bytes(filename: str, default_bytes: int | None = None) -> int:
    ext = get_tabular_extension(filename)
    return int(_TABULAR_UPLOAD_LIMITS.get(ext, default_bytes or UPLOAD_MAX_FILE_BYTES))


def get_tabular_upload_limits() -> dict[str, int]:
    return dict(_TABULAR_UPLOAD_LIMITS)


def load_tabular_preview(
    raw_bytes: bytes,
    filename: str,
    preview_rows: int = TABULAR_PREVIEW_ROW_LIMIT,
    preview_char_limit: int = TABULAR_PREVIEW_CHAR_LIMIT,
) -> dict:
    ext = get_tabular_extension(filename)
    if ext == ".csv":
        return _load_delimited_preview(raw_bytes, delimiter_hint=None, preview_rows=preview_rows, preview_char_limit=preview_char_limit)
    if ext == ".tsv":
        return _load_delimited_preview(raw_bytes, delimiter_hint="\t", preview_rows=preview_rows, preview_char_limit=preview_char_limit)
    if ext == ".xlsx":
        return _load_xlsx_preview(raw_bytes, preview_rows=preview_rows, preview_char_limit=preview_char_limit)
    if ext == ".xlsb":
        return _load_xlsb_preview(raw_bytes, preview_rows=preview_rows, preview_char_limit=preview_char_limit)
    if ext == ".xls":
        return _load_xls_preview(raw_bytes, preview_rows=preview_rows, preview_char_limit=preview_char_limit)
    raise TabularLoaderError(f"Formato tabular não suportado: {ext or 'desconhecido'}")


def load_tabular_dataset(
    raw_bytes: bytes,
    filename: str,
    max_rows: int = TABULAR_RECORD_LIMIT,
) -> dict:
    ext = get_tabular_extension(filename)
    if ext == ".csv":
        return _load_delimited_dataset(raw_bytes, delimiter_hint=None, max_rows=max_rows)
    if ext == ".tsv":
        return _load_delimited_dataset(raw_bytes, delimiter_hint="\t", max_rows=max_rows)
    if ext == ".xlsx":
        return _load_xlsx_dataset(raw_bytes, max_rows=max_rows)
    if ext == ".xlsb":
        return _load_xlsb_dataset(raw_bytes, max_rows=max_rows)
    if ext == ".xls":
        return _load_xls_dataset(raw_bytes, max_rows=max_rows)
    raise TabularLoaderError(f"Formato tabular não suportado: {ext or 'desconhecido'}")


def detect_polymorphic_schema(
    columns: list[str],
    sample_records: list[dict],
    column_types: dict[str, str],
    row_count: int,
) -> dict | None:
    if not columns or not sample_records:
        return None

    generic_columns = [column for column in columns if _GENERIC_COLUMN_PATTERN.match(str(column or "").strip())]
    if len(generic_columns) < _POLYMORPHIC_GENERIC_THRESHOLD:
        return None

    empty_columns = [column for column in columns if _sample_fill_rate(sample_records, column) == 0.0]
    non_generic_columns = [column for column in columns if column not in generic_columns]
    best_candidate: tuple[float, str, dict[str, list[dict]]] | None = None

    for candidate in non_generic_columns:
        fill_rate = _sample_fill_rate(sample_records, candidate)
        values = [str(record.get(candidate, "") or "").strip() for record in sample_records]
        distinct_values = [value for value in sorted(set(values)) if value]
        if fill_rate < _POLYMORPHIC_PIVOT_MIN_FILL:
            continue
        if not (2 <= len(distinct_values) <= _POLYMORPHIC_PIVOT_MAX_DISTINCT):
            continue

        groups: dict[str, list[dict]] = {}
        for record in sample_records:
            key = str(record.get(candidate, "") or "").strip()
            if not key:
                continue
            groups.setdefault(key, []).append(record)
        if len(groups) < 2:
            continue

        fill_variance_hits = 0
        type_variance_hits = 0
        for generic_column in generic_columns:
            fill_rates = []
            inferred_types = set()
            for group_rows in groups.values():
                values_in_group = _non_empty_values(group_rows, generic_column)
                fill_rates.append(len(values_in_group) / max(1, len(group_rows)))
                if values_in_group:
                    inferred_types.add(_infer_generic_value_type(values_in_group))
            if fill_rates and (max(fill_rates) - min(fill_rates)) >= 0.5:
                fill_variance_hits += 1
            if len(inferred_types) > 1:
                type_variance_hits += 1

        score = float(fill_variance_hits * 2 + type_variance_hits)
        if score <= 0:
            continue
        if best_candidate is None or score > best_candidate[0]:
            best_candidate = (score, candidate, groups)

    if best_candidate is None:
        return None

    empty_ratio = len(empty_columns) / max(1, len(columns))
    score, pivot_column, pivot_groups = best_candidate
    if empty_ratio < _POLYMORPHIC_EMPTY_COL_THRESHOLD and score < 3:
        return None

    universal_columns = [
        column
        for column in non_generic_columns
        if all(_sample_fill_rate(rows, column) >= 0.9 for rows in pivot_groups.values())
    ]

    pivot_profiles: dict[str, dict] = {}
    for pivot_value, rows in sorted(pivot_groups.items(), key=lambda item: item[0]):
        filled_generics = {}
        empty_generics = []
        for generic_column in generic_columns:
            values = _non_empty_values(rows, generic_column)
            if not values:
                empty_generics.append(generic_column)
                continue
            filled_generics[generic_column] = {
                "fill_pct": round((len(values) / max(1, len(rows))) * 100, 1),
                "inferred_type": _infer_generic_value_type(values),
                "samples": _distinct_samples(values, limit=3),
            }
        pivot_profiles[pivot_value] = {
            "row_count": len(rows),
            "filled_generics": filled_generics,
            "empty_generics": empty_generics,
        }

    summary_text = (
        f"Detetado padrão polimórfico/EAV com pivot '{pivot_column}', "
        f"{len(generic_columns)} colunas genéricas e {len(pivot_profiles)} perfis de pivot "
        f"em amostra de {len(sample_records)} linhas (dataset total: {row_count})."
    )
    return {
        "is_polymorphic": True,
        "pivot_column": pivot_column,
        "generic_columns": generic_columns,
        "empty_columns": empty_columns,
        "universal_columns": universal_columns,
        "pivot_profiles": pivot_profiles,
        "pivot_values_count": len(pivot_profiles),
        "summary_text": summary_text,
    }


def _infer_generic_value_type(values: list[str]) -> str:
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return "text"
    distinct = set(cleaned)
    if len(distinct) == 1:
        return "fixed_value"
    if sum(1 for value in cleaned if _UUID_VALUE_PATTERN.match(value)) / len(cleaned) >= 0.8:
        return "uuid"
    if sum(1 for value in cleaned if _looks_like_base64(value)) / len(cleaned) >= 0.8:
        return "base64_encoded"
    if len({value.lower() for value in cleaned}) <= 2 and all(value.lower() in _BOOLEAN_VALUES for value in cleaned):
        return "boolean"
    if sum(1 for value in cleaned if _parse_float(value) is not None) / len(cleaned) >= 0.8:
        return "numeric"
    if sum(1 for value in cleaned if _parse_datetime(value) is not None) / len(cleaned) >= 0.8:
        return "date"
    return "text"


def _load_delimited_preview(
    raw_bytes: bytes,
    delimiter_hint: str | None,
    preview_rows: int,
    preview_char_limit: int,
) -> dict:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise TabularLoaderError("CSV/TSV vazio.")
    delimiter = delimiter_hint or _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if not header:
        raise TabularLoaderError("CSV/TSV sem header válido.")
    columns = _normalize_header_row(header)
    sample_rows: list[list[str]] = []
    preview_lines = [delimiter.join(columns)]
    row_count = 0
    truncated = False
    for raw_row in reader:
        row_count += 1
        row = _normalize_row(raw_row, len(columns))
        if len(sample_rows) < preview_rows:
            sample_rows.append(row)
        line = delimiter.join(row)
        if _fits_preview(preview_lines, line, preview_char_limit):
            preview_lines.append(line)
        else:
            truncated = True
    return _preview_payload(columns, sample_rows, row_count, delimiter, preview_lines, truncated)


def _load_delimited_dataset(raw_bytes: bytes, delimiter_hint: str | None, max_rows: int) -> dict:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise TabularLoaderError("CSV/TSV vazio.")
    delimiter = delimiter_hint or _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if not header:
        raise TabularLoaderError("CSV/TSV sem header válido.")
    columns = _normalize_header_row(header)
    records = []
    row_count = 0
    truncated = False
    for raw_row in reader:
        row_count += 1
        row = _normalize_row(raw_row, len(columns))
        if len(records) < max_rows:
            records.append({col: row[idx] for idx, col in enumerate(columns)})
        else:
            truncated = True
    return {
        "columns": columns,
        "records": records,
        "row_count": row_count,
        "rows_loaded": len(records),
        "truncated": truncated,
        "delimiter": delimiter,
    }


def _load_xlsx_preview(raw_bytes: bytes, preview_rows: int, preview_char_limit: int) -> dict:
    all_rows = _read_excel_rows(raw_bytes)
    if not all_rows:
        raise TabularLoaderError("Excel vazio.")
    columns = _normalize_header_row(all_rows[0])
    row_count = len(all_rows) - 1
    sample_rows: list[list[str]] = []
    preview_lines = ["\t".join(columns)]
    truncated = False
    for row in all_rows[1:]:
        normalized = _normalize_row(_row_values_from_sequence(row), len(columns))
        if len(sample_rows) < preview_rows:
            sample_rows.append(normalized)
        line = "\t".join(normalized)
        if _fits_preview(preview_lines, line, preview_char_limit):
            preview_lines.append(line)
        else:
            truncated = True
        if len(sample_rows) >= preview_rows and len(preview_lines) >= (preview_rows + 1):
            break
    truncated = truncated or row_count > len(sample_rows)
    return _preview_payload(columns, sample_rows, row_count, "\t", preview_lines, truncated)


def _load_xlsx_dataset(raw_bytes: bytes, max_rows: int) -> dict:
    all_rows = _read_excel_rows(raw_bytes)
    if not all_rows:
        raise TabularLoaderError("Excel vazio.")
    columns = _normalize_header_row(all_rows[0])
    return _collect_dataset(
        columns,
        (_row_values_from_sequence(row) for row in all_rows[1:]),
        max_rows=max_rows,
        delimiter="\t",
    )


def _load_xlsb_preview(raw_bytes: bytes, preview_rows: int, preview_char_limit: int) -> dict:
    all_rows = _read_excel_rows(raw_bytes)
    if not all_rows:
        raise TabularLoaderError("XLSB vazio.")
    columns = _normalize_header_row(all_rows[0])
    sample_rows, row_count, preview_lines, truncated = _collect_row_preview(
        columns,
        (_row_values_from_sequence(row) for row in all_rows[1:]),
        "\t",
        preview_rows,
        preview_char_limit,
    )
    return _preview_payload(columns, sample_rows, row_count, "\t", preview_lines, truncated)


def _load_xlsb_dataset(raw_bytes: bytes, max_rows: int) -> dict:
    all_rows = _read_excel_rows(raw_bytes)
    if not all_rows:
        raise TabularLoaderError("XLSB vazio.")
    columns = _normalize_header_row(all_rows[0])
    return _collect_dataset(
        columns,
        (_row_values_from_sequence(row) for row in all_rows[1:]),
        max_rows=max_rows,
        delimiter="\t",
    )


def _load_xls_preview(raw_bytes: bytes, preview_rows: int, preview_char_limit: int) -> dict:
    dataset = _load_xls_dataset(raw_bytes, max_rows=preview_rows)
    columns = dataset["columns"]
    sample_rows = [[record.get(col, "") for col in columns] for record in dataset["records"]]
    preview_lines = ["\t".join(columns)] + ["\t".join(row) for row in sample_rows]
    truncated = bool(dataset["truncated"] or len("\n".join(preview_lines)) > preview_char_limit)
    data_text = ("\n".join(preview_lines))[:preview_char_limit]
    preview_payload = _preview_payload(columns, sample_rows, dataset["row_count"], "\t", preview_lines, truncated)
    preview_payload["data_text"] = data_text
    return preview_payload


def _load_xls_dataset(raw_bytes: bytes, max_rows: int) -> dict:
    all_rows = _read_excel_rows(raw_bytes)
    if not all_rows:
        raise TabularLoaderError("Excel vazio.")
    columns = _normalize_header_row(all_rows[0])
    return _collect_dataset(
        columns,
        (_row_values_from_sequence(row) for row in all_rows[1:]),
        max_rows=max_rows,
        delimiter="\t",
    )


def _collect_row_preview(
    columns: list[str],
    row_iter: Iterator[list[str]],
    delimiter: str,
    preview_rows: int,
    preview_char_limit: int,
) -> tuple[list[list[str]], int, list[str], bool]:
    sample_rows: list[list[str]] = []
    preview_lines = [delimiter.join(columns)]
    row_count = 0
    truncated = False
    for row in row_iter:
        row_count += 1
        normalized = _normalize_row(row, len(columns))
        if len(sample_rows) < preview_rows:
            sample_rows.append(normalized)
        line = delimiter.join(normalized)
        if _fits_preview(preview_lines, line, preview_char_limit):
            preview_lines.append(line)
        else:
            truncated = True
    return sample_rows, row_count, preview_lines, truncated


def _collect_dataset(
    columns: list[str],
    row_iter: Iterator[list[str]],
    max_rows: int,
    delimiter: str,
) -> dict:
    records = []
    row_count = 0
    truncated = False
    for row in row_iter:
        row_count += 1
        normalized = _normalize_row(row, len(columns))
        if len(records) < max_rows:
            records.append({col: normalized[idx] for idx, col in enumerate(columns)})
        else:
            truncated = True
    return {
        "columns": columns,
        "records": records,
        "row_count": row_count,
        "rows_loaded": len(records),
        "truncated": truncated,
        "delimiter": delimiter,
    }


def _preview_payload(
    columns: list[str],
    sample_rows: list[list[str]],
    row_count: int,
    delimiter: str,
    preview_lines: list[str],
    truncated: bool,
) -> dict:
    sample_records = [{col: row[idx] for idx, col in enumerate(columns)} for row in sample_rows]
    column_types = _infer_column_types(columns, sample_rows)
    return {
        "columns": columns,
        "row_count": row_count,
        "data_text": "\n".join(preview_lines),
        "delimiter": delimiter,
        "sample_rows": sample_rows,
        "sample_records": sample_records,
        "col_analysis": _build_column_analysis(columns, sample_rows, column_types),
        "column_types": column_types,
        "truncated": truncated or row_count > len(sample_rows),
    }


def _sample_fill_rate(rows: list[dict], column: str) -> float:
    if not rows:
        return 0.0
    return len(_non_empty_values(rows, column)) / len(rows)


def _non_empty_values(rows: list[dict], column: str) -> list[str]:
    values = []
    for row in rows:
        value = str((row or {}).get(column, "") or "").strip()
        if value:
            values.append(value)
    return values


def _distinct_samples(values: list[str], limit: int = 3) -> list[str]:
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _build_column_analysis(
    columns: list[str],
    sample_rows: list[list[str]],
    column_types: dict[str, str],
) -> list[dict]:
    analyses = []
    for idx, column in enumerate(columns):
        values = [row[idx] for row in sample_rows if idx < len(row) and str(row[idx]).strip()]
        analyses.append(
            {
                "name": column,
                "sample": values[:3],
                "non_empty_in_preview": len(values),
                "preview_type": column_types.get(column, "empty"),
            }
        )
    return analyses


def _infer_column_types(columns: list[str], sample_rows: list[list[str]]) -> dict[str, str]:
    result = {}
    for idx, column in enumerate(columns):
        values = [row[idx] for row in sample_rows if idx < len(row) and str(row[idx]).strip()]
        if not values:
            result[column] = "empty"
            continue
        numeric_hits = sum(1 for value in values if _parse_float(value) is not None)
        date_hits = sum(1 for value in values if _parse_datetime(value) is not None)
        if numeric_hits / max(1, len(values)) >= 0.8:
            result[column] = "numeric"
        elif date_hits / max(1, len(values)) >= 0.8:
            result[column] = "date"
        else:
            result[column] = "text"
    return result


def _temporary_tabular_file(raw_bytes: bytes, suffix: str):
    class _TempPath:
        def __enter__(self_inner):
            handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            handle.write(raw_bytes)
            handle.flush()
            handle.close()
            self_inner.path = handle.name
            return self_inner.path

        def __exit__(self_inner, exc_type, exc, tb):
            try:
                os.unlink(self_inner.path)
            except Exception:
                pass
            return False

    return _TempPath()


def _read_excel_rows(raw_bytes: bytes, sheet_index: int = 0) -> list:
    """Return the first sheet's rows as a list of sequences (row 0 = header).

    Tries python-calamine (fast; handles .xlsx/.xlsb/.xls). Falls back to pandas
    (openpyxl for .xlsx) when calamine is unavailable, so Excel still loads on a
    minimal dependency set. One place owns the calamine-vs-pandas decision.
    """
    try:
        from python_calamine import CalamineWorkbook
        workbook = CalamineWorkbook.from_filelike(io.BytesIO(raw_bytes))
        return workbook.get_sheet_by_index(sheet_index).to_python()
    except ImportError:
        pass  # python-calamine not installed -> pandas fallback
    except Exception:
        pass  # calamine present but failed to parse -> try pandas as a last resort
    try:
        import pandas as pd
    except Exception as exc:
        raise TabularLoaderError(
            "Leitura de Excel requer python-calamine ou pandas no servidor."
        ) from exc
    try:
        frame = pd.read_excel(io.BytesIO(raw_bytes), dtype=object, header=None)
    except Exception as exc:
        raise TabularLoaderError("Falha a ler ficheiro Excel.") from exc
    # header=None -> row 0 is the header, matching calamine's to_python() shape.
    return frame.where(frame.notna(), None).values.tolist()


def _normalize_header_row(values) -> list[str]:
    columns = []
    for idx, value in enumerate(values):
        text = _stringify_cell(value).strip()
        columns.append(text or f"Col{idx + 1}")
    return columns


def _normalize_row(values, width: int) -> list[str]:
    seq = list(values)
    if len(seq) < width:
        seq.extend([""] * (width - len(seq)))
    return [_stringify_cell(seq[idx]) if idx < len(seq) else "" for idx in range(width)]


def _row_values_from_sequence(values) -> list[str]:
    return list(values or [])


def _stringify_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, float):
        # Calamine returns integers as 10.0 — display as "10" not "10.0"
        if value == value and value == int(value):  # not NaN and is integer
            return str(int(value))
        return str(value)
    return str(value)


def _fits_preview(preview_lines: list[str], next_line: str, preview_char_limit: int) -> bool:
    current_size = sum(len(line) for line in preview_lines) + max(0, len(preview_lines) - 1)
    projected = current_size + 1 + len(next_line)
    return projected <= preview_char_limit


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return str(dialect.delimiter or ",")
    except Exception:
        if sample.count(";") > sample.count(","):
            return ";"
        if sample.count("\t") > sample.count(","):
            return "\t"
        return ","


def _parse_float(value: str) -> float | None:
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except Exception:
        return None


def _looks_like_base64(value: str) -> bool:
    raw = str(value or "").strip()
    if len(raw) < 16 or len(raw) % 4 != 0 or not _BASE64_VALUE_PATTERN.match(raw):
        return False
    try:
        decoded = b64decode(raw, validate=True)
    except (ValueError, BinasciiError):
        return False
    return bool(decoded)


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
