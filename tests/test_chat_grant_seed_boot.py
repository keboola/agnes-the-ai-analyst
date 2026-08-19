"""End-to-end: booting the real app (real ``lifespan``, not the extracted
``seed_everyone_chat_grant`` in isolation — see
``tests/db_pg/test_chat_grant_seed_contract.py`` for that) on a genuinely
fresh instance with ``chat.enabled: true`` actually seeds the Everyone/
chat/chat grant. Proves the ``app/main.py`` wiring (computing
``everyone_group_preexisted`` before ``ensure_system`` runs, reading
``chat.enabled`` from ``instance.yaml``) is correct, not just the unit under
test.
"""

from __future__ import annotations


def test_fresh_boot_with_chat_enabled_seeds_everyone_chat_grant(e2e_env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.db import SYSTEM_EVERYONE_GROUP, close_system_db

    close_system_db()  # defensive: no stale connection from a prior test's DATA_DIR

    state_dir = e2e_env["data_dir"] / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "instance.yaml").write_text("chat:\n  enabled: true\n")

    from app.main import create_app

    app = create_app()
    client = TestClient(app)

    r = client.get("/api/health")  # first request — triggers lifespan startup
    assert r.status_code == 200

    from src.repositories import resource_grants_repo, user_groups_repo

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None, "Everyone system group must be seeded at boot"
    assert resource_grants_repo().has_grant([everyone["id"]], "chat", "chat"), (
        "a fresh instance booted with chat.enabled=true must have the Everyone "
        "chat grant seeded — otherwise chat is invisible to everyone, including "
        "admins, until an admin hand-adds the grant"
    )


def test_fresh_boot_with_chat_disabled_does_not_seed_grant(e2e_env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.db import SYSTEM_EVERYONE_GROUP, close_system_db

    close_system_db()

    state_dir = e2e_env["data_dir"] / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "instance.yaml").write_text("chat:\n  enabled: false\n")

    from app.main import create_app

    app = create_app()
    client = TestClient(app)

    r = client.get("/api/health")
    assert r.status_code == 200

    from src.repositories import resource_grants_repo, user_groups_repo

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None
    assert not resource_grants_repo().has_grant([everyone["id"]], "chat", "chat"), (
        "chat.enabled=false must never seed the chat grant"
    )
