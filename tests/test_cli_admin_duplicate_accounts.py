"""`agnes admin duplicate-accounts` — the case-variant reconciliation report.

`users` is UNIQUE on `email`, so two rows differing only in case are legal,
invisible to every constraint, and never adjacent in an address-sorted list.
`get_by_email_ci` silently resolves one of them. This command is the only
surface that names the collision, so its output has to be *actionable*: the
right row marked, ids usable as arguments, and a next step that does not walk
the operator into the same ambiguity.
"""

import json

import cli.commands.admin as admin_mod
import src.db as db_mod
import src.repositories as repos_mod


def _boom(*_a, **_k):
    raise RuntimeError("system DuckDB must not be opened on a Postgres instance")


def _group(email, rows, resolved_id=-1):
    for r in rows:
        r.setdefault("unreachable_by_sign_in", False)
        r.setdefault("has_password", False)
    reachable = [r for r in rows if not r["unreachable_by_sign_in"]]
    return {
        "email": email,
        "count": len(rows),
        "resolved_id": (reachable[0]["id"] if reachable else None) if resolved_id == -1 else resolved_id,
        "users": rows,
    }


DUP = _group(
    "ann@corp.example",
    [
        {"id": "aaaaaaaa-1111-4000-8000-000000000001", "email": "Ann@Corp.example", "active": True},
        {"id": "bbbbbbbb-2222-4000-8000-000000000002", "email": "ann@corp.example", "active": True},
    ],
)


def _wire(monkeypatch, groups):
    class _Users:
        def list_case_variant_duplicates(self):
            return groups

    monkeypatch.setattr(repos_mod, "use_pg", lambda: True)
    monkeypatch.setattr(db_mod, "get_system_db", _boom)
    monkeypatch.setattr(admin_mod, "users_repo", lambda: _Users())


def test_reports_nothing_when_no_addresses_collide(monkeypatch, capsys):
    _wire(monkeypatch, [])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    assert "No case-variant duplicate accounts found." in capsys.readouterr().out


def test_marks_the_row_sign_in_resolves_to(monkeypatch, capsys):
    """Without this marker the report is a list of ids with no way to tell
    which one deactivating would actually disable the identity."""
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out

    resolved_line = next(ln for ln in out.splitlines() if "Ann@Corp.example" in ln)
    other_line = next(ln for ln in out.splitlines() if ln.strip().startswith("ann@corp.example"))
    assert "sign-in resolves here" in resolved_line
    assert "sign-in resolves here" not in other_line


def test_prints_ids_in_full_so_they_can_be_pasted_into_the_next_command(monkeypatch, capsys):
    """`list-users` truncates ids to 8 chars. This report recommends acting on
    a row by id, so a truncated id would make its own advice unusable."""
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out
    for row in DUP["users"]:
        assert row["id"] in out


def test_reconciliation_hint_addresses_rows_by_id_not_by_email(monkeypatch, capsys):
    """`agnes admin deactivate <email>` resolves by EXACT spelling, so pointing
    the operator at the address would walk them back into the ambiguity the
    report just surfaced."""
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out
    assert "agnes admin deactivate <id>" in out
    assert "agnes admin deactivate <email>" not in out


def test_hint_does_not_recommend_a_membership_move_it_cannot_deliver(monkeypatch, capsys):
    """`agnes admin group add-member` takes an address, and the endpoint resolves
    it with `get_by_email_ci` (`app/api/access.py`) — the same lookup sign-in
    uses, i.e. always the row this report marks.

    So advising "pick the row to keep, then move its memberships" is advice the
    tooling cannot carry out for half the choices it offers: an operator keeping
    the unmarked row gets a 201 that granted the group to the row they are about
    to disable. The hint names the marked row as the one to keep and says plainly
    why the other direction is not available, rather than leaving the operator to
    discover it from a silent success.
    """
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out

    assert "KEEP the row marked" in out
    assert "not supported by these commands" in out
    # And the reason, so the advice is checkable rather than folklore: an
    # operator would reasonably assume disabling the marked row redirects the
    # lookup. It does not — `get_by_email_ci` ignores `active` on purpose.
    assert "ignores active state" in out


def test_shows_deactivation_state_per_row(monkeypatch, capsys):
    """The failure mode the report exists for: the disabled row is not the one
    sign-in reaches."""
    group = _group(
        "ann@corp.example",
        [
            {"id": "aaaaaaaa-1111-4000-8000-000000000001", "email": "Ann@Corp.example", "active": True},
            {"id": "bbbbbbbb-2222-4000-8000-000000000002", "email": "ann@corp.example", "active": False},
        ],
    )
    _wire(monkeypatch, [group])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out
    assert "DEACTIVATED" in out
    resolved_line = next(ln for ln in out.splitlines() if "Ann@Corp.example" in ln)
    assert "active" in resolved_line and "sign-in resolves here" in resolved_line


def test_json_carries_the_whole_report(monkeypatch, capsys):
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_addresses"] == 1
    assert payload["shown"] == 1
    assert payload["duplicates"][0]["resolved_id"] == DUP["users"][0]["id"]
    assert [u["email"] for u in payload["duplicates"][0]["users"]] == [
        "Ann@Corp.example",
        "ann@corp.example",
    ]


def test_limit_truncates_but_still_reports_the_true_total(monkeypatch, capsys):
    """A capped report that showed only the capped count would read as
    "that's all of them" — the opposite of what a reconciliation needs."""
    groups = [
        _group(
            f"user{i}@corp.example",
            [
                {"id": f"id-{i}-a", "email": f"User{i}@corp.example", "active": True},
                {"id": f"id-{i}-b", "email": f"user{i}@corp.example", "active": True},
            ],
        )
        for i in range(5)
    ]
    _wire(monkeypatch, groups)
    admin_mod.duplicate_accounts(limit=2, as_json=False)
    out = capsys.readouterr().out
    assert "5 address(es) held by more than one account" in out
    assert "3 more" in out
    assert "user4@corp.example" not in out


def test_limit_is_reflected_in_json_without_hiding_the_total(monkeypatch, capsys):
    groups = [DUP, _group("bob@corp.example", [{"id": "x", "email": "Bob@corp.example", "active": True}])]
    _wire(monkeypatch, groups)
    admin_mod.duplicate_accounts(limit=1, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_addresses"] == 2
    assert payload["shown"] == 1


def test_never_opens_system_duckdb_on_a_postgres_instance(monkeypatch, capsys):
    """Same trap as PR #878: a vestigial `get_system_db()` turns a read-only
    diagnostic into a hard crash on PG, where `get_system_db` raises."""
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    assert "ann@corp.example" in capsys.readouterr().out


def test_a_padded_row_is_named_unreachable_rather_than_just_unmarked(monkeypatch, capsys):
    """A whitespace-padded address is reached by no sign-in door at all, which
    is a different and worse state than "not the resolved one". Leaving it
    blank would read as an ordinary duplicate the operator could still keep."""
    group = _group(
        "pad@corp.example",
        [
            {"id": "p1", "email": " pad@corp.example", "active": True, "unreachable_by_sign_in": True},
            {"id": "p2", "email": "pad@corp.example", "active": True},
        ],
    )
    _wire(monkeypatch, [group])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out

    lines = {u: next(ln for ln in out.splitlines() if f" {u}  " in ln) for u in ("p1", "p2")}
    assert "unreachable" in lines["p1"]
    assert "sign-in resolves here" not in lines["p1"]
    assert "sign-in resolves here" in lines["p2"]


def test_a_group_nobody_can_sign_in_to_says_so_loudly(monkeypatch, capsys):
    group = _group(
        "ghost@corp.example",
        [
            {"id": "g1", "email": " ghost@corp.example", "active": True, "unreachable_by_sign_in": True},
            {"id": "g2", "email": "ghost@corp.example ", "active": True, "unreachable_by_sign_in": True},
        ],
    )
    _wire(monkeypatch, [group])
    admin_mod.duplicate_accounts(limit=0, as_json=False)
    out = capsys.readouterr().out
    assert "no row here is reachable by sign-in" in out


def test_json_carries_no_credential_columns(monkeypatch, capsys):
    """The repository projects an allow-list, but the CLI is what an operator
    redirects to a file — so assert on the bytes it actually writes."""
    _wire(monkeypatch, [DUP])
    admin_mod.duplicate_accounts(limit=0, as_json=True)
    raw = capsys.readouterr().out
    for banned in ("password_hash", "reset_token", "setup_token"):
        assert banned not in raw
