"""Tests for all DuckDB repository classes."""

import os
import pytest


@pytest.fixture
def db_conn(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


# ---- SyncState ----

class TestSyncStateRepository:
    def test_update_and_get(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=1000, file_size_bytes=5000, hash="abc123")
        state = repo.get_table_state("orders")
        assert state is not None
        assert state["rows"] == 1000
        assert state["hash"] == "abc123"
        assert state["status"] == "ok"

    def test_get_nonexistent(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        assert repo.get_table_state("nonexistent") is None

    def test_get_last_sync(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        last = repo.get_last_sync("orders")
        assert last is not None

    def test_get_all_states(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        repo.update_sync(table_id="customers", rows=50, file_size_bytes=200, hash="h2")
        all_states = repo.get_all_states()
        assert len(all_states) == 2

    def test_history_recorded(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        repo.update_sync(table_id="orders", rows=200, file_size_bytes=800, hash="h2")
        history = repo.get_sync_history("orders", limit=10)
        assert len(history) == 2
        assert history[0]["rows"] == 200  # newest first

    def test_update_with_error(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(
            table_id="orders", rows=0, file_size_bytes=0, hash="",
            status="error", error="Connection timeout",
        )
        state = repo.get_table_state("orders")
        assert state["status"] == "error"
        assert state["error"] == "Connection timeout"


# ---- Users ----

class TestUserRepository:
    def test_create_and_get(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test User", role="analyst")
        user = repo.get_by_id("u1")
        assert user is not None
        assert user["email"] == "test@acme.com"
        assert user["role"] == "analyst"

    def test_get_by_email(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test User")
        user = repo.get_by_email("test@acme.com")
        assert user is not None
        assert user["id"] == "u1"

    def test_get_nonexistent(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        assert repo.get_by_id("nope") is None
        assert repo.get_by_email("nope@nope.com") is None

    def test_list_all(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="a@acme.com", name="A")
        repo.create(id="u2", email="b@acme.com", name="B")
        assert len(repo.list_all()) == 2

    def test_update_role(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.update(id="u1", role="admin")
        user = repo.get_by_id("u1")
        assert user["role"] == "admin"

    def test_delete(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.delete("u1")
        assert repo.get_by_id("u1") is None

    def test_set_password_hash(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.update(id="u1", password_hash="$argon2id$hashed")
        user = repo.get_by_id("u1")
        assert user["password_hash"] == "$argon2id$hashed"


# ---- Knowledge ----

class TestKnowledgeRepository:
    def test_create_and_get(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="MRR Definition", content="Monthly recurring...",
                     category="metrics", source_user="petr@acme.com")
        item = repo.get_by_id("k1")
        assert item is not None
        assert item["title"] == "MRR Definition"
        assert item["status"] == "pending"

    def test_list_by_status(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.create(id="k2", title="B", content="b", category="c")
        repo.update_status("k1", "approved")
        approved = repo.list_items(statuses=["approved"])
        assert len(approved) == 1
        assert approved[0]["id"] == "k1"

    def test_vote(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.vote("k1", "user1", 1)
        repo.vote("k1", "user2", -1)
        votes = repo.get_votes("k1")
        assert votes["upvotes"] == 1
        assert votes["downvotes"] == 1

    def test_vote_replace(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.vote("k1", "user1", 1)
        repo.vote("k1", "user1", -1)  # change vote
        votes = repo.get_votes("k1")
        assert votes["upvotes"] == 0
        assert votes["downvotes"] == 1

    def test_search(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="Revenue metrics", content="MRR definition", category="metrics")
        repo.create(id="k2", title="Support SLA", content="Response times", category="support")
        results = repo.search("revenue")
        assert len(results) == 1
        assert results[0]["id"] == "k1"


# ---- Audit ----

class TestAuditRepository:
    def test_log_and_query(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders",
                 params={"force": True}, result="ok", duration_ms=1200)
        entries = repo.query(limit=10)
        assert len(entries) == 1
        assert entries[0]["action"] == "sync_trigger"
        assert entries[0]["duration_ms"] == 1200

    def test_query_by_action(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders")
        repo.log(user_id="u1", action="login", resource=None)
        entries = repo.query(action="sync_trigger")
        assert len(entries) == 1

    def test_query_by_user(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders")
        repo.log(user_id="u2", action="sync_trigger", resource="customers")
        entries = repo.query(user_id="u1")
        assert len(entries) == 1


# ---- Telegram ----

class TestTelegramRepository:
    def test_link_and_get(self, db_conn):
        from src.repositories.notifications import TelegramRepository
        repo = TelegramRepository(db_conn)
        repo.link_user("u1", chat_id=12345)
        link = repo.get_link("u1")
        assert link is not None
        assert link["chat_id"] == 12345

    def test_unlink(self, db_conn):
        from src.repositories.notifications import TelegramRepository
        repo = TelegramRepository(db_conn)
        repo.link_user("u1", chat_id=12345)
        repo.unlink_user("u1")
        assert repo.get_link("u1") is None


# ---- PendingCode ----

class TestPendingCodeRepository:
    def test_create_and_verify(self, db_conn):
        from src.repositories.notifications import PendingCodeRepository
        repo = PendingCodeRepository(db_conn)
        repo.create_code("ABC123", chat_id=12345)
        code = repo.verify_code("ABC123")
        assert code is not None
        assert code["chat_id"] == 12345
        # Code consumed
        assert repo.verify_code("ABC123") is None


# ---- Script ----

class TestScriptRepository:
    def test_deploy_and_get(self, db_conn):
        from src.repositories.notifications import ScriptRepository
        repo = ScriptRepository(db_conn)
        repo.deploy("s1", name="sales_alert", owner="u1",
                     schedule="0 8 * * MON", source="print('hello')")
        script = repo.get("s1")
        assert script is not None
        assert script["schedule"] == "0 8 * * MON"

    def test_list_all(self, db_conn):
        from src.repositories.notifications import ScriptRepository
        repo = ScriptRepository(db_conn)
        repo.deploy("s1", name="alert1", owner="u1", source="pass")
        repo.deploy("s2", name="alert2", owner="u1", source="pass")
        assert len(repo.list_all()) == 2

    def test_undeploy(self, db_conn):
        from src.repositories.notifications import ScriptRepository
        repo = ScriptRepository(db_conn)
        repo.deploy("s1", name="test", owner="u1", source="pass")
        repo.undeploy("s1")
        assert repo.get("s1") is None


# ---- TableRegistry ----

class TestTableRegistryRepository:
    def test_register_and_get(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="orders", name="Orders", folder="sales",
                       sync_strategy="incremental", registered_by="admin")
        table = repo.get("orders")
        assert table is not None
        assert table["folder"] == "sales"

    def test_list_all(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", folder="f1")
        repo.register(id="t2", name="B", folder="f2")
        assert len(repo.list_all()) == 2

    def test_unregister(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", folder="f1")
        repo.unregister("t1")
        assert repo.get("t1") is None

    def test_register_with_source_fields(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(
            id="in.c-crm.company", name="company",
            source_type="keboola", bucket="in.c-crm", source_table="company",
            query_mode="local", sync_schedule="every 15m", profile_after_sync=True,
        )
        table = repo.get("in.c-crm.company")
        assert table["source_type"] == "keboola"
        assert table["bucket"] == "in.c-crm"
        assert table["source_table"] == "company"
        assert table["query_mode"] == "local"
        assert table["sync_schedule"] == "every 15m"
        assert table["profile_after_sync"] is True

    def test_list_by_source(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", source_type="keboola")
        repo.register(id="t2", name="B", source_type="bigquery")
        repo.register(id="t3", name="C", source_type="keboola")
        keboola = repo.list_by_source("keboola")
        assert len(keboola) == 2
        assert all(t["source_type"] == "keboola" for t in keboola)
        bq = repo.list_by_source("bigquery")
        assert len(bq) == 1

    def test_list_local(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", source_type="keboola", query_mode="local")
        repo.register(id="t2", name="B", source_type="bigquery", query_mode="remote")
        repo.register(id="t3", name="C", source_type="keboola", query_mode="local")
        local = repo.list_local()
        assert len(local) == 2
        local_kbc = repo.list_local(source_type="keboola")
        assert len(local_kbc) == 2

    def test_register_bigquery_remote(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(
            id="project.dataset.orders", name="orders",
            source_type="bigquery", bucket="dataset", source_table="orders",
            query_mode="remote", profile_after_sync=False,
        )
        table = repo.get("project.dataset.orders")
        assert table["query_mode"] == "remote"
        assert table["profile_after_sync"] is False


# ---- Profiles ----

class TestProfileRepository:
    def test_save_and_get(self, db_conn):
        from src.repositories.profiles import ProfileRepository
        repo = ProfileRepository(db_conn)
        profile_data = {"columns": [{"name": "id", "type": "int"}], "row_count": 1000}
        repo.save("orders", profile_data)
        profile = repo.get("orders")
        assert profile is not None
        assert profile["row_count"] == 1000

    def test_get_all(self, db_conn):
        from src.repositories.profiles import ProfileRepository
        repo = ProfileRepository(db_conn)
        repo.save("t1", {"row_count": 100})
        repo.save("t2", {"row_count": 200})
        all_profiles = repo.get_all()
        assert len(all_profiles) == 2
