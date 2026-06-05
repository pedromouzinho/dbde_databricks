# =============================================================================
# tests/test_storage_logic.py — regression tests for the storage contract
# =============================================================================
# Pure-logic tests for storage_databricks (no DB / no asyncpg required). These
# lock in the Fase 0-1 fixes: Azure table-name -> snake_case mapping and the
# OData -> SQL filter translation, which several callers depend on.
#
# Runs with pytest, or standalone:  python tests/test_storage_logic.py
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage_databricks as s


def test_safe_table_pascalcase_to_snake():
    # Azure-style names used by the callers must line up with the snake_case
    # tables created in the schema.
    assert s._safe_table("WriterProfiles") == "writer_profiles"
    assert s._safe_table("UploadIndex") == "upload_index"
    assert s._safe_table("ChatHistory") == "chat_history"
    # already snake_case / lowercase stays put
    assert s._safe_table("conversations") == "conversations"
    assert s._safe_table("feedback") == "feedback"
    # injection chars are stripped (defence against SQL injection via table name)
    assert s._safe_table("Bad; DROP--") == "baddrop"


def test_odata_partition_and_row_key():
    conds, params = s._odata_to_sql("PartitionKey eq 'conv' and RowKey eq 'r1'", 1)
    assert conds == ["partition_key = $1", "row_key = $2"]
    assert params == ["conv", "r1"]


def test_odata_operators_and_jsonb_field():
    conds, params = s._odata_to_sql("ExpiresAt lt '2026' and Name eq 'O''Brien'", 1)
    assert conds == ["data->>'ExpiresAt' < $1", "data->>'Name' = $2"]
    # doubled '' OData quote is unescaped to a single '
    assert params == ["2026", "O'Brien"]


def test_odata_all_operators_map():
    for op, sql in (("eq", "="), ("ne", "<>"), ("gt", ">"),
                    ("ge", ">="), ("lt", "<"), ("le", "<=")):
        conds, params = s._odata_to_sql(f"PartitionKey {op} 'x'", 1)
        assert conds == [f"partition_key {sql} $1"], (op, conds)
        assert params == ["x"]


def test_odata_start_index_offset():
    # params must keep numbering when partition/row filters already consumed $1/$2
    conds, params = s._odata_to_sql("Status eq 'open'", 3)
    assert conds == ["data->>'Status' = $3"]
    assert params == ["open"]


def test_odata_unparseable_clause_is_skipped():
    # fail-open: garbage clauses are dropped, not crashed on
    conds, params = s._odata_to_sql("this is not odata", 1)
    assert conds == []
    assert params == []


def test_is_undefined_table_detection():
    assert s._is_undefined_table(Exception('relation "foo" does not exist'))
    assert not s._is_undefined_table(Exception("some other error"))


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
