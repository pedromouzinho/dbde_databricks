from __future__ import annotations

import csv
import io
import os
import tempfile
from collections.abc import Iterator

from config_databricks import UPLOAD_TABULAR_ARTIFACT_BATCH_ROWS
from tabular_loader import (
    TABULAR_PREVIEW_CHAR_LIMIT,
    TABULAR_PREVIEW_ROW_LIMIT,
    TabularLoaderError,
    get_tabular_extension,
    _normalize_header_row,
    _normalize_row,
    _preview_payload,
    _row_values_from_sequence,
    _temporary_tabular_file,
)


def build_tabular_artifact(
    raw_bytes: bytes,
    filename: str,
    *,
    batch_rows: int = UPLOAD_TABULAR_ARTIFACT_BATCH_ROWS,
) -> dict:
    import duckdb
    import pandas as pd

    columns, row_iter = _iter_tabular_rows(raw_bytes, filename)
    if not columns:
        raise TabularLoaderError("Não foi possível criar artefacto tabular sem colunas.")

    # Collect all rows into a list for DataFrame construction.
    # This is dramatically faster than row-by-row INSERT via executemany:
    # 323K rows x 63 cols: executemany ~100s vs DataFrame+COPY ~3s.
    all_rows = [_normalize_row(row, len(columns)) for row in row_iter]
    row_count = len(all_rows)

    temp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".parquet")
    temp_handle.close()
    temp_path = temp_handle.name
    conn = duckdb.connect(database=":memory:")
    try:
        df = pd.DataFrame(all_rows, columns=columns, dtype=str)
        df = df.fillna("")
        conn.register("df_view", df)
        conn.execute(
            f"COPY df_view TO '{temp_path.replace(chr(39), chr(39)+chr(39))}' (FORMAT PARQUET, COMPRESSION ZSTD)",
        )
        del df
        with open(temp_path, "rb") as fh:
            artifact_bytes = fh.read()
        return {
            "format": "parquet",
            "row_count": row_count,
            "columns": columns,
            "artifact_bytes": artifact_bytes,
        }
    finally:
        conn.close()
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def load_tabular_artifact_dataset(
    artifact_bytes: bytes,
    *,
    max_rows: int,
) -> dict:
    import duckdb

    safe_limit = max(1, int(max_rows or 1))
    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            row_count = int(conn.execute("SELECT COUNT(*) FROM read_parquet(?)", [temp_path]).fetchone()[0] or 0)
            query = f"SELECT * FROM read_parquet(?) LIMIT {safe_limit}"
            cursor = conn.execute(query, [temp_path])
            rows = cursor.fetchall()
            columns = [str(col[0]) for col in (cursor.description or [])]
        finally:
            conn.close()

    records = [{column: _duckdb_value_to_string(row[idx]) for idx, column in enumerate(columns)} for row in rows]
    return {
        "columns": columns,
        "records": records,
        "row_count": row_count,
        "rows_loaded": len(records),
        "truncated": row_count > len(records),
        "delimiter": "\t",
    }


def load_tabular_artifact_preview(
    artifact_bytes: bytes,
    *,
    preview_rows: int = TABULAR_PREVIEW_ROW_LIMIT,
    preview_char_limit: int = TABULAR_PREVIEW_CHAR_LIMIT,
) -> dict:
    import duckdb

    safe_preview_rows = max(1, int(preview_rows or 1))
    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            row_count = int(conn.execute("SELECT COUNT(*) FROM read_parquet(?)", [temp_path]).fetchone()[0] or 0)
            query = f"SELECT * FROM read_parquet(?) LIMIT {safe_preview_rows}"
            cursor = conn.execute(query, [temp_path])
            rows = cursor.fetchall()
            columns = [str(col[0]) for col in (cursor.description or [])]
        finally:
            conn.close()

    sample_rows: list[list[str]] = []
    preview_lines = ["\t".join(columns)]
    truncated = False
    for row in rows:
        normalized = [_duckdb_value_to_string(value) for value in row]
        if len(sample_rows) < safe_preview_rows:
            sample_rows.append(normalized)
        line = "\t".join(normalized)
        current_size = sum(len(item) for item in preview_lines) + max(0, len(preview_lines) - 1)
        projected = current_size + 1 + len(line)
        if projected <= preview_char_limit:
            preview_lines.append(line)
        else:
            truncated = True
    return _preview_payload(columns, sample_rows, row_count, "\t", preview_lines, truncated)


def profile_tabular_artifact_columns(
    artifact_bytes: bytes,
    *,
    columns: list[str],
    max_columns: int = 80,
) -> list[dict]:
    import duckdb

    if not artifact_bytes:
        raise TabularLoaderError("Artefacto tabular vazio.")

    limited_columns = [str(col or "").strip() for col in (columns or []) if str(col or "").strip()][: max(1, max_columns)]
    if not limited_columns:
        return []

    profiles = []
    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            for column in limited_columns:
                col_expr = _duckdb_ident(column)
                text_expr = f"TRIM(COALESCE(CAST({col_expr} AS VARCHAR), ''))"
                num_expr = f"TRY_CAST({col_expr} AS DOUBLE)"
                dt_expr = f"TRY_CAST({col_expr} AS TIMESTAMP)"

                non_empty_count, empty_count, numeric_count, datetime_count = conn.execute(
                    (
                        "SELECT "
                        f"COUNT(*) FILTER (WHERE {text_expr} <> ''), "
                        f"COUNT(*) FILTER (WHERE {text_expr} = ''), "
                        f"COUNT({num_expr}), "
                        f"COUNT({dt_expr}) "
                        "FROM read_parquet(?)"
                    ),
                    [temp_path],
                ).fetchone()

                non_empty_count = int(non_empty_count or 0)
                empty_count = int(empty_count or 0)
                numeric_count = int(numeric_count or 0)
                datetime_count = int(datetime_count or 0)

                sample_rows = conn.execute(
                    (
                        f"SELECT {text_expr} AS value "
                        "FROM read_parquet(?) "
                        f"WHERE {text_expr} <> '' "
                        "LIMIT 5"
                    ),
                    [temp_path],
                ).fetchall()
                sample_values = [str(row[0] or "") for row in sample_rows if row and str(row[0] or "")]

                ratio = numeric_count / max(1, non_empty_count)
                type_hint = "numeric" if ratio >= 0.8 and numeric_count > 0 else "text"
                if type_hint == "text" and datetime_count >= max(5, int(0.6 * max(1, non_empty_count))):
                    type_hint = "datetime"

                profile = {
                    "name": column,
                    "non_empty": non_empty_count,
                    "empty": empty_count,
                    "type": type_hint,
                    "sample": sample_values,
                }

                if type_hint == "numeric" and numeric_count > 0:
                    min_value, max_value, mean_value, std_value = conn.execute(
                        (
                            "SELECT "
                            f"MIN({num_expr}), "
                            f"MAX({num_expr}), "
                            f"AVG({num_expr}), "
                            f"STDDEV_SAMP({num_expr}) "
                            "FROM read_parquet(?)"
                        ),
                        [temp_path],
                    ).fetchone()
                    profile.update(
                        {
                            "min": round(float(min_value), 6) if min_value is not None else None,
                            "max": round(float(max_value), 6) if max_value is not None else None,
                            "mean": round(float(mean_value), 6) if mean_value is not None else None,
                            "std": round(float(std_value), 6) if std_value is not None else 0.0,
                        }
                    )
                else:
                    distinct_count = conn.execute(
                        (
                            "SELECT COUNT(DISTINCT value) FROM ("
                            f"SELECT {text_expr} AS value "
                            "FROM read_parquet(?) "
                            f"WHERE {text_expr} <> ''"
                            ") t"
                        ),
                        [temp_path],
                    ).fetchone()[0]
                    top_rows = conn.execute(
                        (
                            f"SELECT {text_expr} AS value, COUNT(*) AS count "
                            "FROM read_parquet(?) "
                            f"WHERE {text_expr} <> '' "
                            "GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT 5"
                        ),
                        [temp_path],
                    ).fetchall()
                    profile["distinct_count"] = int(distinct_count or 0)
                    profile["top_values"] = [
                        {"value": str(value or ""), "count": int(count or 0)}
                        for value, count in top_rows
                    ]

                profiles.append(profile)
        finally:
            conn.close()
    return profiles


def export_tabular_artifact_as_csv_bytes(artifact_bytes: bytes) -> bytes:
    import duckdb

    if not artifact_bytes:
        raise TabularLoaderError("Artefacto tabular vazio.")

    with _temporary_tabular_file(artifact_bytes, ".parquet") as parquet_path:
        with tempfile.NamedTemporaryFile(prefix="dbde_artifact_", suffix=".csv", delete=False) as tmp_csv:
            csv_path = tmp_csv.name
        conn = duckdb.connect(database=":memory:")
        try:
            safe_parquet_path = str(parquet_path).replace("'", "''")
            safe_csv_path = str(csv_path).replace("'", "''")
            conn.execute(
                f"COPY (SELECT * FROM read_parquet('{safe_parquet_path}')) "
                f"TO '{safe_csv_path}' (FORMAT CSV, HEADER, DELIMITER ',')",
            )
            with open(csv_path, "rb") as fh:
                return fh.read()
        finally:
            conn.close()
            try:
                os.unlink(csv_path)
            except OSError:
                pass


def iter_tabular_artifact_batches(
    artifact_bytes: bytes,
    *,
    columns: list[str] | None = None,
    batch_rows: int = 5000,
) -> Iterator[list[dict[str, str]]]:
    import duckdb

    if not artifact_bytes:
        raise TabularLoaderError("Artefacto tabular vazio.")

    safe_batch_rows = max(100, min(int(batch_rows or 0), 50_000))
    selected_columns = [str(col or "").strip() for col in (columns or []) if str(col or "").strip()]

    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            if selected_columns:
                select_clause = ", ".join(_duckdb_ident(column) for column in selected_columns)
            else:
                select_clause = "*"
            cursor = conn.execute(f"SELECT {select_clause} FROM read_parquet(?)", [temp_path])
            result_columns = [str(col[0]) for col in (cursor.description or [])]
            while True:
                rows = cursor.fetchmany(safe_batch_rows)
                if not rows:
                    break
                yield [
                    {
                        column: _duckdb_value_to_string(row[idx])
                        for idx, column in enumerate(result_columns)
                    }
                    for row in rows
                ]
        finally:
            conn.close()


def compute_tabular_artifact_numeric_metrics(
    artifact_bytes: bytes,
    *,
    column: str,
    requested_metrics: list[str],
) -> dict:
    import duckdb

    safe_column = str(column or "").strip()
    if not artifact_bytes or not safe_column:
        raise TabularLoaderError("Artefacto tabular ou coluna inválida para métricas numéricas.")

    metric_map = {
        "count": "COUNT(try_cast({col} AS DOUBLE))",
        "sum": "SUM(try_cast({col} AS DOUBLE))",
        "mean": "AVG(try_cast({col} AS DOUBLE))",
        "min": "MIN(try_cast({col} AS DOUBLE))",
        "max": "MAX(try_cast({col} AS DOUBLE))",
        "std": "STDDEV_SAMP(try_cast({col} AS DOUBLE))",
        "median": "QUANTILE_CONT(try_cast({col} AS DOUBLE), 0.5)",
        "p25": "QUANTILE_CONT(try_cast({col} AS DOUBLE), 0.25)",
        "p75": "QUANTILE_CONT(try_cast({col} AS DOUBLE), 0.75)",
    }
    selected_metrics = []
    for metric in requested_metrics or []:
        safe_metric = str(metric or "").strip()
        if safe_metric in metric_map and safe_metric not in selected_metrics:
            selected_metrics.append(safe_metric)
    if not selected_metrics:
        selected_metrics = ["count"]

    column_expr = _duckdb_ident(safe_column)
    select_parts = [
        f"{metric_map[metric].format(col=column_expr)} AS {_duckdb_ident(metric)}"
        for metric in selected_metrics
    ]
    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            row = conn.execute(
                f"SELECT {', '.join(select_parts)} FROM read_parquet(?)",
                [temp_path],
            ).fetchone()
        finally:
            conn.close()

    result = {}
    for idx, metric in enumerate(selected_metrics):
        value = row[idx] if row and idx < len(row) else None
        if value is None:
            continue
        if metric == "count":
            result[metric] = int(value)
        else:
            result[metric] = round(float(value), 6)
    return result


def summarize_tabular_artifact_values(
    artifact_bytes: bytes,
    *,
    column: str,
    top_n: int = 200,
    all_limit: int = 0,
) -> dict:
    import duckdb

    safe_column = str(column or "").strip()
    if not artifact_bytes or not safe_column:
        raise TabularLoaderError("Artefacto tabular ou coluna inválida para resumo categórico.")

    top_limit = max(1, min(int(top_n or 0), 10000))
    full_limit = max(0, min(int(all_limit or 0), 10000))
    column_expr = _duckdb_ident(safe_column)
    value_expr = (
        f"TRIM(COALESCE(CAST({column_expr} AS VARCHAR), ''))"
    )

    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            non_empty_count, empty_count, distinct_count = conn.execute(
                (
                    "SELECT "
                    f"COUNT(*) FILTER (WHERE {value_expr} <> ''), "
                    f"COUNT(*) FILTER (WHERE {value_expr} = ''), "
                    f"COUNT(DISTINCT CASE WHEN {value_expr} <> '' THEN {value_expr} END) "
                    "FROM read_parquet(?)"
                ),
                [temp_path],
            ).fetchone()

            top_values = conn.execute(
                (
                    f"SELECT {value_expr} AS value, COUNT(*) AS count "
                    "FROM read_parquet(?) "
                    f"WHERE {value_expr} <> '' "
                    "GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT ?"
                ),
                [temp_path, top_limit],
            ).fetchall()

            all_values = []
            if full_limit > 0:
                all_values = conn.execute(
                    (
                        f"SELECT {value_expr} AS value, COUNT(*) AS count "
                        "FROM read_parquet(?) "
                        f"WHERE {value_expr} <> '' "
                        "GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT ?"
                    ),
                    [temp_path, full_limit],
                ).fetchall()
        finally:
            conn.close()

    return {
        "non_empty_count": int(non_empty_count or 0),
        "empty_count": int(empty_count or 0),
        "distinct_count": int(distinct_count or 0),
        "top_values": [(str(value or ""), int(count or 0)) for value, count in top_values],
        "all_values": [(str(value or ""), int(count or 0)) for value, count in all_values],
    }


def aggregate_tabular_artifact_by_period(
    artifact_bytes: bytes,
    *,
    date_column: str,
    value_column: str,
    group_mode: str,
    requested_metrics: list[str],
) -> dict:
    import duckdb

    safe_date_column = str(date_column or "").strip()
    safe_value_column = str(value_column or "").strip()
    safe_group_mode = str(group_mode or "").strip().lower()
    if not artifact_bytes or not safe_date_column or not safe_value_column:
        raise TabularLoaderError("Parâmetros inválidos para agregação temporal do artefacto tabular.")

    dt_expr = f"TRY_CAST({_duckdb_ident(safe_date_column)} AS TIMESTAMP)"
    num_expr = f"TRY_CAST({_duckdb_ident(safe_value_column)} AS DOUBLE)"
    group_expr_map = {
        "year": f"strftime({dt_expr}, '%Y')",
        "month": f"strftime({dt_expr}, '%Y-%m')",
        "quarter": f"printf('%04d-Q%d', year({dt_expr}), quarter({dt_expr}))",
        "week": f"strftime({dt_expr}, '%G-W%V')",
        "day": f"strftime({dt_expr}, '%Y-%m-%d')",
    }
    group_expr = group_expr_map.get(safe_group_mode)
    if not group_expr:
        raise TabularLoaderError("group_mode inválido para agregação temporal.")

    metric_map = {
        "count": "COUNT(*)",
        "sum": f"SUM({num_expr})",
        "mean": f"AVG({num_expr})",
        "min": f"MIN({num_expr})",
        "max": f"MAX({num_expr})",
        "std": f"STDDEV_SAMP({num_expr})",
        "median": f"QUANTILE_CONT({num_expr}, 0.5)",
        "p25": f"QUANTILE_CONT({num_expr}, 0.25)",
        "p75": f"QUANTILE_CONT({num_expr}, 0.75)",
    }
    selected_metrics = []
    for metric in requested_metrics or []:
        safe_metric = str(metric or "").strip()
        if safe_metric in metric_map and safe_metric not in selected_metrics:
            selected_metrics.append(safe_metric)
    if not selected_metrics:
        selected_metrics = ["mean"]

    select_metrics = [f"{metric_map[metric]} AS {_duckdb_ident(metric)}" for metric in selected_metrics]
    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            rows = conn.execute(
                (
                    f"SELECT {group_expr} AS group_key, "
                    "COUNT(*) AS bucket_count, "
                    f"COUNT({num_expr}) AS numeric_count, "
                    + ", ".join(select_metrics)
                    + " FROM read_parquet(?) "
                    f"WHERE {dt_expr} IS NOT NULL "
                    "GROUP BY 1 ORDER BY 1"
                ),
                [temp_path],
            ).fetchall()
        finally:
            conn.close()

    groups = []
    numeric_points = 0
    row_points = 0
    for row in rows:
        if not row:
            continue
        key = str(row[0] or "")
        bucket_count = int(row[1] or 0)
        numeric_count = int(row[2] or 0)
        metrics_map_out = {}
        for idx, metric in enumerate(selected_metrics, start=3):
            value = row[idx] if idx < len(row) else None
            if value is None:
                continue
            metrics_map_out[metric] = int(value) if metric == "count" else round(float(value), 6)
        groups.append({"group": key, "count": bucket_count, "metrics": metrics_map_out})
        row_points += bucket_count
        numeric_points += numeric_count
    return {
        "groups": groups,
        "rows_processed": row_points,
        "numeric_points": numeric_points,
    }


def compare_tabular_artifact_periods(
    artifact_bytes: bytes,
    *,
    date_column: str,
    value_column: str,
    period1: str,
    period2: str,
    requested_metrics: list[str],
) -> dict:
    import duckdb

    safe_date_column = str(date_column or "").strip()
    safe_value_column = str(value_column or "").strip()
    if not artifact_bytes or not safe_date_column or not safe_value_column:
        raise TabularLoaderError("Parâmetros inválidos para comparação de períodos.")

    dt_expr = f"TRY_CAST({_duckdb_ident(safe_date_column)} AS TIMESTAMP)"
    num_expr = f"TRY_CAST({_duckdb_ident(safe_value_column)} AS DOUBLE)"
    metric_map = {
        "count": "COUNT(*)",
        "sum": f"SUM({num_expr})",
        "mean": f"AVG({num_expr})",
        "min": f"MIN({num_expr})",
        "max": f"MAX({num_expr})",
        "std": f"STDDEV_SAMP({num_expr})",
        "median": f"QUANTILE_CONT({num_expr}, 0.5)",
        "p25": f"QUANTILE_CONT({num_expr}, 0.25)",
        "p75": f"QUANTILE_CONT({num_expr}, 0.75)",
    }
    selected_metrics = []
    for metric in requested_metrics or []:
        safe_metric = str(metric or "").strip()
        if safe_metric in metric_map and safe_metric not in selected_metrics:
            selected_metrics.append(safe_metric)
    if not selected_metrics:
        selected_metrics = ["mean"]

    def _period_clause(expr: str) -> tuple[str, str]:
        safe_expr = str(expr or "").strip()
        if not safe_expr:
            raise TabularLoaderError("Expressão de período vazia.")
        if len(safe_expr) > 32:
            raise TabularLoaderError("Expressão de período demasiado longa.")
        if safe_expr.isdigit() and len(safe_expr) == 4:
            return (f"strftime({dt_expr}, '%Y') = ?", safe_expr)
        if len(safe_expr) == 7 and safe_expr[4] == "-":
            return (f"strftime({dt_expr}, '%Y-%m') = ?", safe_expr)
        if len(safe_expr) == 7 and safe_expr[4:6].upper() == "-Q":
            return (f"printf('%04d-Q%d', year({dt_expr}), quarter({dt_expr})) = ?", safe_expr.upper())
        if len(safe_expr) == 8 and safe_expr[4:6].upper() == "-W":
            return (f"strftime({dt_expr}, '%G-W%V') = ?", safe_expr.upper())
        if len(safe_expr) == 10 and safe_expr[4] == "-" and safe_expr[7] == "-":
            return (f"strftime({dt_expr}, '%Y-%m-%d') = ?", safe_expr)
        return (f"strftime({dt_expr}, '%Y-%m-%d') LIKE ?", f"{safe_expr}%")

    def _compute_row(row, selected_metrics_local):
        if not row:
            return {"count": 0, "numeric_count": 0, "metrics": {}}
        bucket_count = int(row[0] or 0)
        numeric_count = int(row[1] or 0)
        metrics = {}
        for idx, metric in enumerate(selected_metrics_local, start=2):
            value = row[idx] if idx < len(row) else None
            if value is None:
                continue
            metrics[metric] = int(value) if metric == "count" else round(float(value), 6)
        return {"count": bucket_count, "numeric_count": numeric_count, "metrics": metrics}

    select_metrics = [f"{metric_map[metric]} AS {_duckdb_ident(metric)}" for metric in selected_metrics]
    period1_clause, period1_value = _period_clause(period1)
    period2_clause, period2_value = _period_clause(period2)

    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            base_select = (
                "SELECT COUNT(*) AS bucket_count, "
                f"COUNT({num_expr}) AS numeric_count, "
                + ", ".join(select_metrics)
                + " FROM read_parquet(?) "
                f"WHERE {dt_expr} IS NOT NULL AND "
            )
            row1 = conn.execute(base_select + period1_clause, [temp_path, period1_value]).fetchone()
            row2 = conn.execute(base_select + period2_clause, [temp_path, period2_value]).fetchone()
        finally:
            conn.close()

    result1 = _compute_row(row1, selected_metrics)
    result2 = _compute_row(row2, selected_metrics)
    return {"period1": result1, "period2": result2}


def load_tabular_artifact_time_series(
    artifact_bytes: bytes,
    *,
    date_column: str,
    value_column: str,
    max_points: int = 2000,
    full_points: bool = False,
) -> dict:
    import duckdb

    safe_date_column = str(date_column or "").strip()
    safe_value_column = str(value_column or "").strip()
    if not artifact_bytes or not safe_date_column or not safe_value_column:
        raise TabularLoaderError("Parâmetros inválidos para série temporal do artefacto tabular.")

    safe_max_points = max(1, min(int(max_points or 0), 100_000))
    dt_expr = f"TRY_CAST({_duckdb_ident(safe_date_column)} AS TIMESTAMP)"
    num_expr = f"TRY_CAST({_duckdb_ident(safe_value_column)} AS DOUBLE)"
    base_sql = (
        f"SELECT {dt_expr} AS dt, {num_expr} AS num "
        "FROM read_parquet(?) "
        f"WHERE {dt_expr} IS NOT NULL AND {num_expr} IS NOT NULL"
    )
    order_by = "dt, num"

    with _temporary_tabular_file(artifact_bytes, ".parquet") as temp_path:
        conn = duckdb.connect(database=":memory:")
        try:
            total_points = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM ({base_sql}) t",
                    [temp_path],
                ).fetchone()[0]
                or 0
            )
            if total_points <= 0:
                return {"points": [], "total_points": 0, "sampled": False}

            if full_points or total_points <= safe_max_points:
                rows = conn.execute(
                    f"SELECT dt, num FROM ({base_sql}) t ORDER BY {order_by}",
                    [temp_path],
                ).fetchall()
                sampled = False
            else:
                step = max(1, -(-total_points // safe_max_points))
                rows = conn.execute(
                    (
                        "SELECT dt, num FROM ("
                        f"SELECT dt, num, ROW_NUMBER() OVER (ORDER BY {order_by}) AS rn "
                        f"FROM ({base_sql}) t"
                        ") ranked "
                        "WHERE ((rn - 1) % ?) = 0 "
                        f"ORDER BY {order_by} LIMIT ?"
                    ),
                    [temp_path, step, safe_max_points],
                ).fetchall()
                sampled = True
        finally:
            conn.close()

    points = []
    for dt_value, num_value in rows:
        if dt_value is None or num_value is None:
            continue
        points.append((dt_value, float(num_value)))
    return {
        "points": points,
        "total_points": total_points,
        "sampled": sampled,
    }


def _insert_batch(conn, columns: list[str], batch: list[list[str]]) -> None:
    placeholders = ", ".join(["?"] * len(columns))
    conn.executemany(f"INSERT INTO uploaded VALUES ({placeholders})", batch)


def _duckdb_column_defs(columns: list[str]) -> str:
    return ", ".join(f"{_duckdb_ident(column)} VARCHAR" for column in columns)


def _duckdb_ident(value: str) -> str:
    return '"' + str(value or "").replace('"', '""') + '"'


def _duckdb_value_to_string(value) -> str:
    if value is None:
        return ""
    return str(value)


def _iter_tabular_rows(raw_bytes: bytes, filename: str) -> tuple[list[str], Iterator[list[str]]]:
    ext = get_tabular_extension(filename)
    if ext == ".csv":
        return _iter_delimited_rows(raw_bytes, delimiter_hint=None)
    if ext == ".tsv":
        return _iter_delimited_rows(raw_bytes, delimiter_hint="\t")
    if ext == ".xlsx":
        return _iter_xlsx_rows(raw_bytes)
    if ext == ".xlsb":
        return _iter_xlsb_rows(raw_bytes)
    if ext == ".xls":
        return _iter_xls_rows(raw_bytes)
    raise TabularLoaderError(f"Formato tabular não suportado: {ext or 'desconhecido'}")


def _iter_delimited_rows(raw_bytes: bytes, delimiter_hint: str | None) -> tuple[list[str], Iterator[list[str]]]:
    from tabular_loader import _sniff_delimiter

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise TabularLoaderError("CSV/TSV vazio.")
    delimiter = delimiter_hint or _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if not header:
        raise TabularLoaderError("CSV/TSV sem header válido.")
    columns = _normalize_header_row(header)

    def _rows() -> Iterator[list[str]]:
        for raw_row in reader:
            yield _row_values_from_sequence(raw_row)

    return columns, _rows()


def _iter_xlsx_rows(raw_bytes: bytes) -> tuple[list[str], Iterator[list[str]]]:
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_filelike(io.BytesIO(raw_bytes))
    sheet = workbook.get_sheet_by_index(0)
    all_rows = sheet.to_python()
    if not all_rows:
        raise TabularLoaderError("Excel vazio.")
    columns = _normalize_header_row(all_rows[0])

    def _rows() -> Iterator[list[str]]:
        for row in all_rows[1:]:
            yield _row_values_from_sequence(row)

    return columns, _rows()


def _iter_xlsb_rows(raw_bytes: bytes) -> tuple[list[str], Iterator[list[str]]]:
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_filelike(io.BytesIO(raw_bytes))
    sheet = workbook.get_sheet_by_index(0)
    all_rows = sheet.to_python()
    if not all_rows:
        raise TabularLoaderError("XLSB vazio.")
    columns = _normalize_header_row(all_rows[0])

    def _rows() -> Iterator[list[str]]:
        for row in all_rows[1:]:
            yield _row_values_from_sequence(row)

    return columns, _rows()


def _iter_xls_rows(raw_bytes: bytes) -> tuple[list[str], Iterator[list[str]]]:
    from python_calamine import CalamineWorkbook

    try:
        workbook = CalamineWorkbook.from_filelike(io.BytesIO(raw_bytes))
    except Exception:
        # Fallback to pandas/xlrd for edge-case .xls files
        try:
            import pandas as pd
        except Exception as exc:
            raise TabularLoaderError("Leitura de .xls requer pandas/xlrd no servidor.") from exc
        temp_ctx = _temporary_tabular_file(raw_bytes, ".xls")
        temp_path = temp_ctx.__enter__()
        try:
            frame = pd.read_excel(temp_path, dtype=object)
        except Exception as exc:
            temp_ctx.__exit__(None, None, None)
            raise TabularLoaderError("Falha a ler ficheiro .xls.") from exc
        temp_ctx.__exit__(None, None, None)
        if frame.empty and not list(frame.columns):
            raise TabularLoaderError("Excel vazio.")
        columns = _normalize_header_row(frame.columns.tolist())

        def _rows_pd() -> Iterator[list[str]]:
            for row in frame.itertuples(index=False, name=None):
                yield _row_values_from_sequence(row)

        return columns, _rows_pd()

    sheet = workbook.get_sheet_by_index(0)
    all_rows = sheet.to_python()
    if not all_rows:
        raise TabularLoaderError("Excel vazio.")
    columns = _normalize_header_row(all_rows[0])

    def _rows() -> Iterator[list[str]]:
        for row in all_rows[1:]:
            yield _row_values_from_sequence(row)

    return columns, _rows()
