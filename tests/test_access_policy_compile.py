"""Unit tests for the no-SQL builder's structured-spec -> SQL compiler.

The compiler is the safety-critical core of the access-policy builder: it must
never emit the two-column plaintext leak (`SELECT *, md5(col) AS col`), and its
output must be a policy the real validator accepts.
"""

from src.access_policy_compile import compile_policy

COLS = ["invoice_id", "cost_center", "email", "national_id", "amount_eur"]


def test_hash_mask_excludes_before_rederiving():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"national_id": "hide", "email": "hash"},
    }
    out = compile_policy(spec, COLS)
    # Both the hidden and the re-derived column are EXCLUDEd from `*`, so the
    # star can never re-emit the plaintext original alongside the masked one.
    assert set(out.excluded) == {"national_id", "email"}
    assert "EXCLUDE" in out.sql
    assert '"national_id"' in out.sql and '"email"' in out.sql
    # exactly one output column named email, and it is the hash
    assert out.sql.count('AS "email"') == 1
    assert 'md5("email") AS "email"' in out.sql
    # the row rule uses the transpile-safe group-membership idiom, not IN
    assert 'list_contains($user_groups, "cost_center")' in out.sql


def test_show_only_is_a_bare_star_no_exclude():
    spec = {"table": "invoices", "row_rules": [], "row_combine": "and", "column_masks": {}}
    out = compile_policy(spec, COLS)
    assert out.sql == 'SELECT * FROM "invoices"'
    assert out.excluded == []
    # a no-op policy is flagged, not silently accepted
    assert any("full table" in w for w in out.warnings)


def test_nullify_and_unmask_masks():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "amount_eur": "nullify",
            "email": {"choice": "unmask", "group": "Finance"},
        },
    }
    out = compile_policy(spec, COLS)
    assert 'NULL AS "amount_eur"' in out.sql
    assert 'CASE WHEN list_contains($user_groups, \'Finance\') THEN "email" ELSE NULL END AS "email"' in out.sql
    assert set(out.excluded) == {"amount_eur", "email"}


def test_unknown_columns_are_dropped_with_a_warning():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "does_not_exist", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"ghost": "hide"},
    }
    out = compile_policy(spec, COLS)
    # neither the unknown row rule nor the unknown mask reaches the SQL
    assert "does_not_exist" not in out.sql
    assert "ghost" not in out.sql
    assert any("ghost" in w for w in out.warnings)


def test_eq_and_in_row_ops_use_literals():
    spec = {
        "table": "invoices",
        "row_rules": [
            {"column": "cost_center", "op": "eq", "value": "FIN-EU"},
            {"column": "amount_eur", "op": "in", "value": [10, 20]},
        ],
        "row_combine": "or",
        "column_masks": {},
    }
    out = compile_policy(spec, COLS)
    assert "\"cost_center\" = 'FIN-EU'" in out.sql
    assert '"amount_eur" IN (10, 20)' in out.sql
    assert " OR " in out.sql


def test_self_owned_rows_bind_the_identity_placeholder():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "email", "op": "eq_caller_email"}],
        "row_combine": "and",
        "column_masks": {},
    }
    out = compile_policy(spec, COLS)
    assert '"email" = $user_email' in out.sql


def test_compiled_sql_passes_the_real_validator():
    from src.access_policy_validate import validate_policy_sql

    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"email": "hash", "national_id": "hide"},
    }
    out = compile_policy(spec, COLS)
    # The builder's own output must never be rejected by the gate every save runs.
    validate_policy_sql(
        out.sql,
        table_id="invoices",
        table_name="invoices",
        mapping_table_names=set(),
        for_remote=False,
    )
