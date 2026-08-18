"""Unit tests for the no-SQL builder's structured-spec -> SQL compiler.

The compiler is the safety-critical core of the access-policy builder: it must
never emit the two-column plaintext leak (e.g. ``SELECT *, md5(col) AS col``),
must never produce ``SELECT *`` (so a newly-added source column cannot leak),
and must keep the original column type for allowed ``unmask`` callers while
returning a safe fallback for everyone else.
"""

import pytest

from src.access_policy_compile import compile_policy

COLS = [
    {"name": "invoice_id", "type": "BIGINT"},
    {"name": "cost_center", "type": "VARCHAR"},
    {"name": "email", "type": "VARCHAR"},
    {"name": "national_id", "type": "VARCHAR"},
    {"name": "amount_eur", "type": "DOUBLE"},
]


def _projected(sql: str) -> str:
    """Return the projection clause between SELECT and FROM."""
    return sql.split(" FROM ")[0].replace("SELECT ", "")


def test_hash_mask_uses_explicit_projection_and_single_alias():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"national_id": "hide", "email": "hash"},
    }
    out = compile_policy(spec, COLS)
    # Hidden and re-derived columns are still tracked.
    assert set(out.excluded) == {"national_id", "email"}
    assert out.derived == ["email"]
    # No star projection: a newly-added source column cannot leak by default.
    assert "SELECT *" not in out.sql
    assert "EXCLUDE" not in out.sql
    # The hidden column is omitted entirely.
    assert '"national_id"' not in _projected(out.sql)
    # Exactly one output column named email, and it is the hash.
    assert out.sql.count('AS "email"') == 1
    assert 'md5("email") AS "email"' in out.sql
    # The row rule uses the transpile-safe group-membership idiom, not IN.
    assert 'list_contains($user_groups, "cost_center")' in out.sql


def test_show_only_is_explicit_projection_and_warns():
    spec = {"table": "invoices", "row_rules": [], "row_combine": "and", "column_masks": {}}
    out = compile_policy(spec, COLS)
    assert out.sql == ('SELECT "invoice_id", "cost_center", "email", "national_id", "amount_eur" FROM "invoices"')
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
    # nullify preserves the numeric type
    assert 'CAST(NULL AS DOUBLE) AS "amount_eur"' in out.sql
    # unmask for text callers keeps the original type: the THEN branch is the
    # raw column; the ELSE branch is the fixed redaction string.
    assert "CASE WHEN list_contains($user_groups, 'Finance') THEN \"email\" ELSE '*****' END AS \"email\"" in out.sql
    assert set(out.excluded) == {"amount_eur", "email"}


def test_unmask_for_non_text_uses_null_with_type_cast():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, COLS)
    assert (
        'CASE WHEN list_contains($user_groups, \'Finance\') THEN "amount_eur" ELSE CAST(NULL AS DOUBLE) END AS "amount_eur"'
        in out.sql
    )


def test_unmask_with_multiple_groups_uses_or():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Finance", "Legal"]},
        },
    }
    out = compile_policy(spec, COLS)
    assert (
        "CASE WHEN list_contains($user_groups, 'Finance') OR list_contains($user_groups, 'Legal') THEN \"email\""
        in out.sql
    )
    assert " ELSE '*****' END AS \"email\"" in out.sql


def test_unmask_empty_allowlist_always_masks():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": {"choice": "unmask", "groups": []},
        },
    }
    out = compile_policy(spec, COLS)
    assert 'CASE WHEN FALSE THEN "email" ELSE \'*****\' END AS "email"' in out.sql


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
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Finance", "Legal"]},
            "national_id": "hide",
            "amount_eur": "nullify",
        },
    }
    out = compile_policy(spec, COLS)
    # The builder's own output must never be rejected by the gate every save runs,
    # including the remote transpile path.
    validate_policy_sql(
        out.sql,
        table_id="invoices",
        table_name="invoices",
        mapping_table_names=set(),
        for_remote=False,
    )
    validate_policy_sql(
        out.sql,
        table_id="invoices",
        table_name="invoices",
        mapping_table_names=set(),
        for_remote=True,
    )


def test_explicit_projection_is_fixed_and_omits_hidden_columns():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"national_id": "hide"},
    }
    out = compile_policy(spec, COLS)
    assert "SELECT *" not in out.sql
    assert '"national_id"' not in out.sql
    # remaining columns appear in the input order
    expected = '"invoice_id", "cost_center", "email", "amount_eur"'
    assert expected in out.sql


def test_backwards_compatible_string_columns_default_to_text():
    """Until all callers pass typed descriptors, plain column names are accepted."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"amount_eur": {"choice": "unmask", "group": "Finance"}},
    }
    out = compile_policy(spec, ["invoice_id", "cost_center", "email", "amount_eur"])
    # Without a known type we fall back to treating the column as text-like.
    assert "ELSE '*****'" in out.sql


def test_show_columns_are_not_duplicated():
    """Explicit 'show' choices must not produce duplicate output columns."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": "show",
            "national_id": "hide",
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, COLS)
    # Each output column appears exactly once and in the original table order.
    assert out.sql.count('AS "email"') == 0  # show columns need no alias
    assert out.sql.count('AS "amount_eur"') == 1
    proj = _projected(out.sql)
    expected = (
        '"invoice_id", "cost_center", "email", '
        "CASE WHEN list_contains($user_groups, 'Finance') THEN \"amount_eur\" "
        'ELSE CAST(NULL AS DOUBLE) END AS "amount_eur"'
    )
    assert proj == expected
    assert '"national_id"' not in out.sql


def test_hiding_all_columns_fails_closed():
    """A policy that would project no columns must not fall back to SELECT *."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {c["name"]: "hide" for c in COLS},
    }
    with pytest.raises(ValueError, match="select no columns"):
        compile_policy(spec, COLS)


def test_masked_columns_preserve_input_order():
    """The projection order matches the table's column order, not the mask spec order."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        # Spec dict order is email, invoice_id, amount_eur.
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Legal"]},
            "invoice_id": "hash",
            "amount_eur": "nullify",
        },
    }
    out = compile_policy(spec, COLS)
    proj = _projected(out.sql)
    expected = (
        'md5("invoice_id") AS "invoice_id", "cost_center", '
        "CASE WHEN list_contains($user_groups, 'Legal') THEN \"email\" ELSE '*****' END AS \"email\", "
        '"national_id", CAST(NULL AS DOUBLE) AS "amount_eur"'
    )
    assert proj == expected


def test_composite_text_types_do_not_use_string_redaction():
    """Arrays/structs containing VARCHAR must keep the composite type, not '*****'."""
    cols = [
        {"name": "tags", "type": "VARCHAR[]"},
        {"name": "nested", "type": "STRUCT(v VARCHAR, i BIGINT)"},
        {"name": "amount_eur", "type": "VARCHAR"},
    ]
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "tags": {"choice": "unmask", "groups": ["Finance"]},
            "nested": {"choice": "unmask", "groups": ["Finance"]},
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, cols)
    # VARCHAR[] and STRUCT must fall back to a type-preserving NULL.
    assert "ELSE CAST(NULL AS VARCHAR[])" in out.sql
    assert "ELSE CAST(NULL AS STRUCT(v VARCHAR, i BIGINT))" in out.sql
    # Plain VARCHAR still gets the fixed redaction string.
    assert "ELSE '*****'" in out.sql
