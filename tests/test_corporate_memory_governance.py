"""
Tests for Corporate Memory Governance Phase 1.

Covers:
- Governance config loading and modes
- KM admin checks
- Status transition validation
- Admin actions: approve, reject, mandate, revoke, edit, batch
- Knowledge retrieval with governance filtering
- Voting restrictions under governance modes
- User rules generation (legacy, hybrid, mandatory_only)
- Audience group checks
- Migration of pre-governance items
- Audit log write/read/pagination/filtering
- Collector governance integration (initial status on new items)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ADMIN_EMAIL = "admin@co.com"
USER_EMAIL = "user@co.com"
OUTSIDER_EMAIL = "outsider@co.com"


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Reset module-level caches before every test."""
    import webapp.corporate_memory_service as svc

    svc._governance_config_cache = None
    svc._groups_cache = None
    yield
    svc._governance_config_cache = None
    svc._groups_cache = None


def _base_instance_config(
    *,
    governance: dict | None = None,
    users: dict | None = None,
    groups: dict | None = None,
):
    """Build a minimal instance config dict for mocking load_instance_config."""
    cfg = {
        "instance": {"name": "test"},
        "auth": {"allowed_domain": "co.com", "webapp_secret_key": "s3cret"},
        "server": {"host": "127.0.0.1", "hostname": "test.local"},
    }
    if governance is not None:
        cfg["corporate_memory"] = governance
    if users is not None:
        cfg["users"] = users
    if groups is not None:
        cfg["groups"] = groups
    return cfg


@pytest.fixture
def governance_config():
    """Standard governance block used by most tests."""
    return {
        "distribution_mode": "hybrid",
        "approval_mode": "review_queue",
        "review_period_months": 6,
    }


@pytest.fixture
def users_config():
    return {
        ADMIN_EMAIL: {"display_name": "Admin User", "km_admin": True},
        USER_EMAIL: {"display_name": "Regular User"},
    }


@pytest.fixture
def groups_config():
    return {
        "finance": {"label": "Finance Team", "members": [USER_EMAIL]},
    }


@pytest.fixture
def full_instance_config(governance_config, users_config, groups_config):
    """Complete instance config with governance, users, and groups."""
    return _base_instance_config(
        governance=governance_config,
        users=users_config,
        groups=groups_config,
    )


@pytest.fixture
def legacy_instance_config(users_config):
    """Instance config WITHOUT corporate_memory section (legacy mode)."""
    return _base_instance_config(users=users_config)


def _make_knowledge_data(items: dict | None = None) -> dict:
    """Build a knowledge.json structure."""
    return {
        "items": items or {},
        "metadata": {"last_collection": "2026-03-20T10:00:00+00:00"},
    }


def _make_item(
    item_id: str = "km_abc123",
    *,
    title: str = "Test Rule",
    content: str = "Do the thing correctly.",
    status: str = "pending",
    category: str = "data_analysis",
    audience: str = "all",
    **extra,
) -> dict:
    """Build a single knowledge item dict."""
    item = {
        "id": item_id,
        "title": title,
        "content": content,
        "category": category,
        "tags": ["test"],
        "source_users": [USER_EMAIL],
        "status": status,
        "extracted_at": "2026-03-20T10:00:00+00:00",
        "updated_at": "2026-03-20T10:00:00+00:00",
        "approved_by": None,
        "approved_at": None,
        "mandatory_reason": None,
        "audience": audience,
        "review_by": None,
        "edited_by": None,
        "edited_at": None,
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# Helper: set up file-backed service state in tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def service_env(tmp_path, full_instance_config):
    """Patch module-level paths and config loader for the service module.

    Returns a dict with helper functions to read back the written files.
    """
    knowledge_path = tmp_path / "knowledge.json"
    votes_path = tmp_path / "votes.json"
    audit_path = tmp_path / "audit.jsonl"

    def _setup(
        knowledge: dict | None = None,
        votes: dict | None = None,
        instance_config: dict | None = None,
    ):
        if knowledge is not None:
            knowledge_path.write_text(json.dumps(knowledge), encoding="utf-8")
        if votes is not None:
            votes_path.write_text(json.dumps(votes), encoding="utf-8")

        cfg = instance_config or full_instance_config

        patches = [
            patch("webapp.corporate_memory_service.KNOWLEDGE_FILE", knowledge_path),
            patch("webapp.corporate_memory_service.VOTES_FILE", votes_path),
            patch("webapp.corporate_memory_service.AUDIT_FILE", audit_path),
            patch("webapp.corporate_memory_service.load_instance_config", return_value=cfg),
        ]
        for p in patches:
            p.start()

        return patches

    def _read_knowledge():
        return json.loads(knowledge_path.read_text(encoding="utf-8"))

    def _read_votes():
        if votes_path.exists():
            return json.loads(votes_path.read_text(encoding="utf-8"))
        return {}

    def _read_audit_lines():
        if not audit_path.exists():
            return []
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    ctx = {
        "setup": _setup,
        "read_knowledge": _read_knowledge,
        "read_votes": _read_votes,
        "read_audit": _read_audit_lines,
        "knowledge_path": knowledge_path,
        "votes_path": votes_path,
        "audit_path": audit_path,
    }

    yield ctx

    # Stop all patches
    patch.stopall()


# ===================================================================
# TestGovernanceConfig
# ===================================================================


class TestGovernanceConfig:
    """Tests for get_governance_mode / get_approval_mode helpers."""

    def test_governance_mode_legacy(self, service_env, legacy_instance_config):
        from webapp.corporate_memory_service import get_governance_mode

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=legacy_instance_config,
        )
        assert get_governance_mode() is None

    def test_governance_mode_hybrid(self, service_env, full_instance_config):
        from webapp.corporate_memory_service import get_governance_mode

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=full_instance_config,
        )
        assert get_governance_mode() == "hybrid"

    def test_governance_mode_mandatory_only(self, service_env, users_config, groups_config):
        from webapp.corporate_memory_service import get_governance_mode

        cfg = _base_instance_config(
            governance={"distribution_mode": "mandatory_only"},
            users=users_config,
            groups=groups_config,
        )
        service_env["setup"](knowledge=_make_knowledge_data(), instance_config=cfg)
        assert get_governance_mode() == "mandatory_only"

    def test_approval_mode_default(self, service_env, full_instance_config):
        from webapp.corporate_memory_service import get_approval_mode

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=full_instance_config,
        )
        assert get_approval_mode() == "review_queue"

    def test_approval_mode_auto_publish(self, service_env, users_config, groups_config):
        from webapp.corporate_memory_service import get_approval_mode

        cfg = _base_instance_config(
            governance={
                "distribution_mode": "hybrid",
                "approval_mode": "auto_publish",
            },
            users=users_config,
            groups=groups_config,
        )
        service_env["setup"](knowledge=_make_knowledge_data(), instance_config=cfg)
        assert get_approval_mode() == "auto_publish"


# ===================================================================
# TestKmAdmin
# ===================================================================


class TestKmAdmin:
    """Tests for is_km_admin()."""

    def test_is_km_admin_true(self, service_env, full_instance_config):
        from webapp.corporate_memory_service import is_km_admin

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=full_instance_config,
        )
        assert is_km_admin(ADMIN_EMAIL) is True

    def test_is_km_admin_false(self, service_env, full_instance_config):
        from webapp.corporate_memory_service import is_km_admin

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=full_instance_config,
        )
        assert is_km_admin(USER_EMAIL) is False

    def test_is_km_admin_user_not_in_config(self, service_env, full_instance_config):
        from webapp.corporate_memory_service import is_km_admin

        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=full_instance_config,
        )
        assert is_km_admin(OUTSIDER_EMAIL) is False

    def test_is_km_admin_no_governance_config(self, service_env):
        from webapp.corporate_memory_service import is_km_admin

        # Config with NO users section at all → everyone is False
        cfg = _base_instance_config()
        service_env["setup"](
            knowledge=_make_knowledge_data(),
            instance_config=cfg,
        )
        assert is_km_admin(USER_EMAIL) is False
        assert is_km_admin(ADMIN_EMAIL) is False


# ===================================================================
# TestTransitionValidation
# ===================================================================


class TestTransitionValidation:
    """Tests for _validate_transition()."""

    def test_pending_to_approved(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("pending", "approved") is True

    def test_pending_to_mandatory(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("pending", "mandatory") is True

    def test_pending_to_revoked(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("pending", "revoked") is False

    def test_approved_to_mandatory(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("approved", "mandatory") is True

    def test_mandatory_to_revoked(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("mandatory", "revoked") is True

    def test_rejected_to_approved(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("rejected", "approved") is True

    def test_approved_to_pending(self):
        from webapp.corporate_memory_service import _validate_transition

        assert _validate_transition("approved", "pending") is False


# ===================================================================
# TestApproveItem
# ===================================================================


class TestApproveItem:
    """Tests for approve_item()."""

    def test_approve_success(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = approve_item(ADMIN_EMAIL, "km_abc123")
        assert ok is True
        assert "approved" in msg.lower()

        data = service_env["read_knowledge"]()
        approved = data["items"]["km_abc123"]
        assert approved["status"] == "approved"
        assert approved["approved_by"] == ADMIN_EMAIL
        assert approved["approved_at"] is not None
        assert approved["review_by"] is not None
        assert approved["updated_at"] is not None

    def test_approve_wrong_status(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="revoked")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = approve_item(ADMIN_EMAIL, "km_abc123")
        # revoked -> approved IS valid per VALID_TRANSITIONS
        assert ok is True

    def test_approve_from_mandatory_invalid(self, service_env):
        """mandatory -> approved IS valid (demoting from mandatory)."""
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="mandatory")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = approve_item(ADMIN_EMAIL, "km_abc123")
        assert ok is True

    def test_approve_item_not_found(self, service_env):
        from webapp.corporate_memory_service import approve_item

        service_env["setup"](knowledge=_make_knowledge_data())

        ok, msg = approve_item(ADMIN_EMAIL, "km_nonexistent")
        assert ok is False
        assert "not found" in msg.lower()

    def test_approve_not_admin(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = approve_item(USER_EMAIL, "km_abc123")
        assert ok is False
        assert "permission" in msg.lower()

    def test_approve_writes_audit_log(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        approve_item(ADMIN_EMAIL, "km_abc123")

        entries = service_env["read_audit"]()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["admin"] == ADMIN_EMAIL
        assert entry["action"] == "approved"
        assert entry["item_id"] == "km_abc123"
        assert entry["details"]["previous_status"] == "pending"
        assert "timestamp" in entry


# ===================================================================
# TestRejectItem
# ===================================================================


class TestRejectItem:
    """Tests for reject_item()."""

    def test_reject_success(self, service_env):
        from webapp.corporate_memory_service import reject_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = reject_item(ADMIN_EMAIL, "km_abc123")
        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["status"] == "rejected"
        assert data["items"]["km_abc123"]["rejected_by"] == ADMIN_EMAIL

    def test_reject_with_reason(self, service_env):
        from webapp.corporate_memory_service import reject_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = reject_item(ADMIN_EMAIL, "km_abc123", reason="Not relevant")
        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["rejection_reason"] == "Not relevant"

        entries = service_env["read_audit"]()
        assert entries[0]["details"]["reason"] == "Not relevant"


# ===================================================================
# TestMandateItem
# ===================================================================


class TestMandateItem:
    """Tests for mandate_item()."""

    def test_mandate_success(self, service_env):
        from webapp.corporate_memory_service import mandate_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        with patch("webapp.corporate_memory_service._regenerate_rules_for_audience"):
            ok, msg = mandate_item(
                ADMIN_EMAIL, "km_abc123",
                mandatory_reason="Company policy",
                audience="group:finance",
            )

        assert ok is True

        data = service_env["read_knowledge"]()
        item_data = data["items"]["km_abc123"]
        assert item_data["status"] == "mandatory"
        assert item_data["mandatory_reason"] == "Company policy"
        assert item_data["audience"] == "group:finance"
        assert item_data["approved_by"] == ADMIN_EMAIL
        assert item_data["review_by"] is not None

    def test_mandate_missing_reason(self, service_env):
        from webapp.corporate_memory_service import mandate_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = mandate_item(ADMIN_EMAIL, "km_abc123", mandatory_reason="")
        assert ok is False
        assert "mandatory_reason" in msg.lower()

    def test_mandate_invalid_audience(self, service_env):
        from webapp.corporate_memory_service import mandate_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = mandate_item(
            ADMIN_EMAIL, "km_abc123",
            mandatory_reason="Reason",
            audience="invalid_format",
        )
        assert ok is False
        assert "invalid audience" in msg.lower()

    def test_mandate_triggers_rule_regeneration(self, service_env):
        from webapp.corporate_memory_service import mandate_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        with patch(
            "webapp.corporate_memory_service._regenerate_rules_for_audience"
        ) as mock_regen:
            mandate_item(
                ADMIN_EMAIL, "km_abc123",
                mandatory_reason="Policy",
                audience="all",
            )
            mock_regen.assert_called_once_with("all")


# ===================================================================
# TestRevokeItem
# ===================================================================


class TestRevokeItem:
    """Tests for revoke_item()."""

    def test_revoke_success(self, service_env):
        from webapp.corporate_memory_service import revoke_item

        item = _make_item(status="mandatory")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        with patch("webapp.corporate_memory_service.regenerate_all_user_rules"):
            ok, msg = revoke_item(ADMIN_EMAIL, "km_abc123")

        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["status"] == "revoked"
        assert data["items"]["km_abc123"]["revoked_by"] == ADMIN_EMAIL

    def test_revoke_not_mandatory(self, service_env):
        from webapp.corporate_memory_service import revoke_item

        item = _make_item(status="approved")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = revoke_item(ADMIN_EMAIL, "km_abc123")
        assert ok is False
        assert "cannot transition" in msg.lower()

    def test_revoke_triggers_rule_regeneration(self, service_env):
        from webapp.corporate_memory_service import revoke_item

        item = _make_item(status="mandatory")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        with patch(
            "webapp.corporate_memory_service.regenerate_all_user_rules"
        ) as mock_regen:
            revoke_item(ADMIN_EMAIL, "km_abc123")
            mock_regen.assert_called_once()


# ===================================================================
# TestEditItem
# ===================================================================


class TestEditItem:
    """Tests for edit_item()."""

    def test_edit_title_only(self, service_env):
        from webapp.corporate_memory_service import edit_item

        item = _make_item(status="approved")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = edit_item(ADMIN_EMAIL, "km_abc123", title="New Title")
        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["title"] == "New Title"
        assert data["items"]["km_abc123"]["content"] == "Do the thing correctly."

    def test_edit_content_only(self, service_env):
        from webapp.corporate_memory_service import edit_item

        item = _make_item(status="approved")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = edit_item(ADMIN_EMAIL, "km_abc123", content="Updated content.")
        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["content"] == "Updated content."
        assert data["items"]["km_abc123"]["title"] == "Test Rule"

    def test_edit_both(self, service_env):
        from webapp.corporate_memory_service import edit_item

        item = _make_item(status="approved")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = edit_item(
            ADMIN_EMAIL, "km_abc123",
            title="New Title",
            content="New content.",
        )
        assert ok is True

        data = service_env["read_knowledge"]()
        assert data["items"]["km_abc123"]["title"] == "New Title"
        assert data["items"]["km_abc123"]["content"] == "New content."
        assert data["items"]["km_abc123"]["edited_by"] == ADMIN_EMAIL
        assert data["items"]["km_abc123"]["edited_at"] is not None

    def test_edit_nothing_provided(self, service_env):
        from webapp.corporate_memory_service import edit_item

        item = _make_item(status="approved")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        ok, msg = edit_item(ADMIN_EMAIL, "km_abc123")
        assert ok is False
        assert "at least one" in msg.lower()

    def test_edit_writes_audit_with_old_new_values(self, service_env):
        from webapp.corporate_memory_service import edit_item

        item = _make_item(status="approved", title="Old Title", content="Old content")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        edit_item(
            ADMIN_EMAIL, "km_abc123",
            title="New Title",
            content="New content",
        )

        entries = service_env["read_audit"]()
        assert len(entries) == 1
        details = entries[0]["details"]
        assert details["old_title"] == "Old Title"
        assert details["new_title"] == "New Title"
        assert details["old_content"] == "Old content"
        assert details["new_content"] == "New content"


# ===================================================================
# TestBatchAction
# ===================================================================


class TestBatchAction:
    """Tests for batch_action()."""

    def test_batch_approve_all_success(self, service_env):
        from webapp.corporate_memory_service import batch_action

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="pending"),
            "km_003": _make_item("km_003", status="pending"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        result = batch_action(ADMIN_EMAIL, ["km_001", "km_002", "km_003"], "approve")
        assert set(result["success"]) == {"km_001", "km_002", "km_003"}
        assert result["failed"] == []

    def test_batch_partial_failure(self, service_env):
        from webapp.corporate_memory_service import batch_action

        items = {
            "km_001": _make_item("km_001", status="pending"),
            # km_002 does not exist
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        result = batch_action(ADMIN_EMAIL, ["km_001", "km_002"], "approve")
        assert result["success"] == ["km_001"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "km_002"

    def test_batch_invalid_action(self, service_env):
        from webapp.corporate_memory_service import batch_action

        service_env["setup"](knowledge=_make_knowledge_data())

        result = batch_action(ADMIN_EMAIL, ["km_001"], "delete")
        assert result["success"] == []
        assert len(result["failed"]) == 1
        assert "invalid action" in result["failed"][0]["error"].lower()


# ===================================================================
# TestGetKnowledge
# ===================================================================


class TestGetKnowledge:
    """Tests for get_knowledge() with governance filtering."""

    def test_legacy_mode_no_filtering(self, service_env, legacy_instance_config, users_config):
        from webapp.corporate_memory_service import get_knowledge

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="approved"),
        }
        # Legacy config has users but NO corporate_memory section
        cfg = _base_instance_config(users=users_config)
        service_env["setup"](knowledge=_make_knowledge_data(items), instance_config=cfg)

        result = get_knowledge()
        # Legacy mode: no status filtering, both items visible
        assert result["total"] == 2

    def test_governance_mode_filters_pending(self, service_env):
        from webapp.corporate_memory_service import get_knowledge

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="approved"),
            "km_003": _make_item("km_003", status="mandatory"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        result = get_knowledge()
        # Default governance filtering: approved + mandatory only
        assert result["total"] == 2
        ids = {i["id"] for i in result["items"]}
        assert "km_001" not in ids
        assert "km_002" in ids
        assert "km_003" in ids

    def test_admin_can_see_pending(self, service_env):
        from webapp.corporate_memory_service import get_knowledge

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="approved"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        result = get_knowledge(include_statuses={"pending", "approved", "mandatory"})
        assert result["total"] == 2

    def test_mandatory_items_have_is_mandatory_flag(self, service_env):
        from webapp.corporate_memory_service import get_knowledge

        items = {
            "km_001": _make_item("km_001", status="mandatory", mandatory_reason="Policy"),
            "km_002": _make_item("km_002", status="approved"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        result = get_knowledge()
        for item in result["items"]:
            if item["id"] == "km_001":
                assert item["is_mandatory"] is True
                assert item["mandatory_reason"] == "Policy"
            else:
                assert item["is_mandatory"] is False


# ===================================================================
# TestVoteGovernance
# ===================================================================


class TestVoteGovernance:
    """Tests for vote() under different governance modes."""

    def test_vote_disabled_mandatory_only(self, service_env, users_config, groups_config):
        from webapp.corporate_memory_service import vote

        cfg = _base_instance_config(
            governance={"distribution_mode": "mandatory_only"},
            users=users_config,
            groups=groups_config,
        )
        items = {"km_001": _make_item("km_001", status="approved")}
        service_env["setup"](knowledge=_make_knowledge_data(items), instance_config=cfg)

        ok, msg = vote(USER_EMAIL, "km_001", 1)
        assert ok is False
        assert "disabled" in msg.lower()

    def test_vote_allowed_hybrid(self, service_env):
        from webapp.corporate_memory_service import vote

        items = {"km_001": _make_item("km_001", status="approved")}
        service_env["setup"](knowledge=_make_knowledge_data(items), votes={})

        with patch("webapp.corporate_memory_service._regenerate_user_rules"):
            ok, msg = vote(USER_EMAIL, "km_001", 1)
        assert ok is True

    def test_vote_allowed_legacy(self, service_env, users_config):
        from webapp.corporate_memory_service import vote

        cfg = _base_instance_config(users=users_config)
        items = {"km_001": _make_item("km_001", status="approved")}
        service_env["setup"](
            knowledge=_make_knowledge_data(items),
            votes={},
            instance_config=cfg,
        )

        with patch("webapp.corporate_memory_service._regenerate_user_rules"):
            ok, msg = vote(USER_EMAIL, "km_001", 1)
        assert ok is True


# ===================================================================
# TestGetUserRules
# ===================================================================


class TestGetUserRules:
    """Tests for get_user_rules() across governance modes."""

    def test_legacy_mode_upvoted_only(self, service_env, users_config):
        from webapp.corporate_memory_service import get_user_rules

        cfg = _base_instance_config(users=users_config)
        items = {
            "km_001": _make_item("km_001", status="approved"),
            "km_002": _make_item("km_002", status="approved"),
        }
        votes = {USER_EMAIL: {"km_001": 1}}
        service_env["setup"](
            knowledge=_make_knowledge_data(items),
            votes=votes,
            instance_config=cfg,
        )

        rules = get_user_rules(USER_EMAIL)
        assert len(rules) == 1
        assert rules[0]["id"] == "km_001"

    def test_mandatory_included_for_all_users(self, service_env):
        from webapp.corporate_memory_service import get_user_rules

        items = {
            "km_001": _make_item("km_001", status="mandatory", audience="all"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items), votes={})

        rules = get_user_rules(USER_EMAIL)
        assert len(rules) == 1
        assert rules[0]["id"] == "km_001"

    def test_hybrid_mandatory_plus_upvoted(self, service_env):
        from webapp.corporate_memory_service import get_user_rules

        items = {
            "km_mand": _make_item("km_mand", status="mandatory", audience="all"),
            "km_appr": _make_item("km_appr", status="approved"),
            "km_pend": _make_item("km_pend", status="pending"),
        }
        votes = {USER_EMAIL: {"km_appr": 1, "km_pend": 1}}
        service_env["setup"](knowledge=_make_knowledge_data(items), votes=votes)

        rules = get_user_rules(USER_EMAIL)
        rule_ids = {r["id"] for r in rules}
        # mandatory + approved upvoted; pending upvoted NOT included
        assert "km_mand" in rule_ids
        assert "km_appr" in rule_ids
        assert "km_pend" not in rule_ids

    def test_mandatory_only_no_upvoted(self, service_env, users_config, groups_config):
        from webapp.corporate_memory_service import get_user_rules

        cfg = _base_instance_config(
            governance={"distribution_mode": "mandatory_only"},
            users=users_config,
            groups=groups_config,
        )
        items = {
            "km_mand": _make_item("km_mand", status="mandatory", audience="all"),
            "km_appr": _make_item("km_appr", status="approved"),
        }
        votes = {USER_EMAIL: {"km_appr": 1}}
        service_env["setup"](
            knowledge=_make_knowledge_data(items),
            votes=votes,
            instance_config=cfg,
        )

        rules = get_user_rules(USER_EMAIL)
        assert len(rules) == 1
        assert rules[0]["id"] == "km_mand"

    def test_audience_group_filtering(self, service_env):
        from webapp.corporate_memory_service import get_user_rules

        items = {
            "km_fin": _make_item("km_fin", status="mandatory", audience="group:finance"),
            "km_all": _make_item("km_all", status="mandatory", audience="all"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items), votes={})

        # USER_EMAIL is a finance group member
        rules_user = get_user_rules(USER_EMAIL)
        rule_ids_user = {r["id"] for r in rules_user}
        assert "km_fin" in rule_ids_user
        assert "km_all" in rule_ids_user

        # OUTSIDER_EMAIL is NOT a member
        rules_outsider = get_user_rules(OUTSIDER_EMAIL)
        rule_ids_outsider = {r["id"] for r in rules_outsider}
        assert "km_fin" not in rule_ids_outsider
        assert "km_all" in rule_ids_outsider


# ===================================================================
# TestCheckAudience
# ===================================================================


class TestCheckAudience:
    """Tests for _check_audience()."""

    def test_audience_all(self, service_env):
        from webapp.corporate_memory_service import _check_audience

        service_env["setup"](knowledge=_make_knowledge_data())

        assert _check_audience({"audience": "all"}, USER_EMAIL) is True

    def test_audience_none(self, service_env):
        from webapp.corporate_memory_service import _check_audience

        service_env["setup"](knowledge=_make_knowledge_data())

        assert _check_audience({}, USER_EMAIL) is True
        assert _check_audience({"audience": None}, USER_EMAIL) is True

    def test_audience_group_member(self, service_env):
        from webapp.corporate_memory_service import _check_audience

        service_env["setup"](knowledge=_make_knowledge_data())

        assert _check_audience({"audience": "group:finance"}, USER_EMAIL) is True

    def test_audience_group_not_member(self, service_env):
        from webapp.corporate_memory_service import _check_audience

        service_env["setup"](knowledge=_make_knowledge_data())

        assert _check_audience({"audience": "group:finance"}, OUTSIDER_EMAIL) is False

    def test_audience_group_not_found(self, service_env):
        from webapp.corporate_memory_service import _check_audience

        service_env["setup"](knowledge=_make_knowledge_data())

        assert _check_audience({"audience": "group:nonexistent"}, USER_EMAIL) is False


# ===================================================================
# TestMigration
# ===================================================================


class TestMigration:
    """Tests for migrate_existing_items()."""

    def test_migrate_adds_status_to_items(self, service_env):
        from webapp.corporate_memory_service import migrate_existing_items

        # Items without a "status" field (pre-governance)
        items = {
            "km_old1": {
                "id": "km_old1",
                "title": "Old Rule 1",
                "content": "Content 1",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
                "extracted_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
            },
            "km_old2": {
                "id": "km_old2",
                "title": "Old Rule 2",
                "content": "Content 2",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
                "extracted_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
            },
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        count = migrate_existing_items()
        assert count == 2

        data = service_env["read_knowledge"]()
        for item_id in ["km_old1", "km_old2"]:
            assert data["items"][item_id]["status"] == "approved"
            assert data["items"][item_id]["approved_by"] == "migration"
            assert data["items"][item_id]["approved_at"] is not None
            assert data["items"][item_id]["review_by"] is not None

    def test_migrate_idempotent(self, service_env):
        from webapp.corporate_memory_service import migrate_existing_items

        items = {
            "km_old": {
                "id": "km_old",
                "title": "Old Rule",
                "content": "Content",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
                "extracted_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
            },
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        count1 = migrate_existing_items()
        assert count1 == 1

        # Second run: items already have status → 0 migrated
        count2 = migrate_existing_items()
        assert count2 == 0

    def test_migrate_writes_audit_entries(self, service_env):
        from webapp.corporate_memory_service import migrate_existing_items

        items = {
            "km_old": {
                "id": "km_old",
                "title": "Old Rule",
                "content": "Content",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
                "extracted_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
            },
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        migrate_existing_items()

        entries = service_env["read_audit"]()
        assert len(entries) == 1
        assert entries[0]["admin"] == "migration"
        assert entries[0]["action"] == "migration_auto_approved"
        assert entries[0]["item_id"] == "km_old"

    def test_migrate_preserves_existing_status(self, service_env):
        from webapp.corporate_memory_service import migrate_existing_items

        items = {
            "km_existing": _make_item("km_existing", status="mandatory"),
            "km_new": {
                "id": "km_new",
                "title": "No Status",
                "content": "Content",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
                "extracted_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
            },
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        count = migrate_existing_items()
        assert count == 1  # only km_new was migrated

        data = service_env["read_knowledge"]()
        assert data["items"]["km_existing"]["status"] == "mandatory"
        assert data["items"]["km_new"]["status"] == "approved"


# ===================================================================
# TestAuditLog
# ===================================================================


class TestAuditLog:
    """Tests for audit log writing and reading."""

    def test_audit_log_written_on_approve(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        approve_item(ADMIN_EMAIL, "km_abc123")

        entries = service_env["read_audit"]()
        assert len(entries) == 1
        assert entries[0]["action"] == "approved"

    def test_audit_log_format(self, service_env):
        from webapp.corporate_memory_service import approve_item

        item = _make_item(status="pending")
        service_env["setup"](knowledge=_make_knowledge_data({"km_abc123": item}))

        approve_item(ADMIN_EMAIL, "km_abc123")

        entries = service_env["read_audit"]()
        entry = entries[0]
        assert isinstance(entry["timestamp"], str)
        assert isinstance(entry["admin"], str)
        assert isinstance(entry["action"], str)
        assert isinstance(entry["item_id"], str)
        assert isinstance(entry["details"], dict)
        # Verify timestamp is ISO format (parseable)
        datetime.fromisoformat(entry["timestamp"])

    def test_get_audit_log_paginated(self, service_env):
        from webapp.corporate_memory_service import approve_item, get_audit_log, reject_item

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="pending"),
            "km_003": _make_item("km_003", status="pending"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        approve_item(ADMIN_EMAIL, "km_001")
        approve_item(ADMIN_EMAIL, "km_002")
        approve_item(ADMIN_EMAIL, "km_003")

        # Page 0, 2 per page
        result = get_audit_log(page=0, per_page=2)
        assert len(result["entries"]) == 2
        assert result["total"] == 3
        assert result["page"] == 0

        # Page 1
        result = get_audit_log(page=1, per_page=2)
        assert len(result["entries"]) == 1

    def test_get_audit_log_filtered_by_action(self, service_env):
        from webapp.corporate_memory_service import approve_item, get_audit_log, reject_item

        items = {
            "km_001": _make_item("km_001", status="pending"),
            "km_002": _make_item("km_002", status="pending"),
        }
        service_env["setup"](knowledge=_make_knowledge_data(items))

        approve_item(ADMIN_EMAIL, "km_001")
        reject_item(ADMIN_EMAIL, "km_002")

        result = get_audit_log(action="approved")
        assert result["total"] == 1
        assert result["entries"][0]["action"] == "approved"

        result = get_audit_log(action="rejected")
        assert result["total"] == 1
        assert result["entries"][0]["action"] == "rejected"


# ===================================================================
# TestCollectorGovernance
# ===================================================================


class TestCollectorGovernance:
    """Tests for governance fields in the collector's _process_catalog_response."""

    def test_new_items_get_pending_status(self):
        from services.corporate_memory.collector import _process_catalog_response

        response_items = [
            {
                "existing_id": None,
                "title": "New Rule",
                "content": "Do something",
                "category": "workflow",
                "tags": ["test"],
                "source_users": [USER_EMAIL],
            },
        ]
        existing = {"items": {}}

        result = _process_catalog_response(
            response_items, existing, initial_status="pending",
        )

        assert len(result) == 1
        item = list(result.values())[0]
        assert item["status"] == "pending"
        assert item["approved_by"] is None
        assert item["audience"] == "all"

    def test_new_items_get_approved_status(self):
        from services.corporate_memory.collector import _process_catalog_response

        response_items = [
            {
                "existing_id": None,
                "title": "New Rule",
                "content": "Do something",
                "category": "workflow",
                "tags": ["test"],
                "source_users": [USER_EMAIL],
            },
        ]
        existing = {"items": {}}

        result = _process_catalog_response(
            response_items, existing, initial_status="approved",
        )

        item = list(result.values())[0]
        assert item["status"] == "approved"

    def test_existing_items_preserve_governance_fields(self):
        from services.corporate_memory.collector import _process_catalog_response

        existing = {
            "items": {
                "km_exist": {
                    "id": "km_exist",
                    "title": "Old Title",
                    "content": "Old content",
                    "category": "workflow",
                    "tags": ["old"],
                    "source_users": [USER_EMAIL],
                    "extracted_at": "2026-03-01T00:00:00+00:00",
                    "updated_at": "2026-03-01T00:00:00+00:00",
                    "status": "mandatory",
                    "approved_by": ADMIN_EMAIL,
                    "approved_at": "2026-03-10T00:00:00+00:00",
                    "mandatory_reason": "Company policy",
                    "audience": "group:finance",
                    "review_by": "2026-09-10T00:00:00+00:00",
                    "edited_by": None,
                    "edited_at": None,
                },
            },
        }

        response_items = [
            {
                "existing_id": "km_exist",
                "title": "Updated Title",
                "content": "Updated content",
                "category": "workflow",
                "tags": ["new"],
                "source_users": [USER_EMAIL, ADMIN_EMAIL],
            },
        ]

        result = _process_catalog_response(
            response_items, existing, initial_status="pending",
        )

        item = result["km_exist"]
        # Title/content updated by LLM
        assert item["title"] == "Updated Title"
        assert item["content"] == "Updated content"
        # Governance fields preserved from existing item
        assert item["status"] == "mandatory"
        assert item["approved_by"] == ADMIN_EMAIL
        assert item["mandatory_reason"] == "Company policy"
        assert item["audience"] == "group:finance"

    def test_no_governance_config_legacy_behavior(self):
        """Without governance config, initial_status is 'approved' (legacy)."""
        from services.corporate_memory.collector import _process_catalog_response

        response_items = [
            {
                "existing_id": None,
                "title": "A Rule",
                "content": "Content",
                "category": "data_analysis",
                "tags": [],
                "source_users": [USER_EMAIL],
            },
        ]
        existing = {"items": {}}

        # Legacy mode: collector passes initial_status="approved"
        result = _process_catalog_response(
            response_items, existing, initial_status="approved",
        )

        item = list(result.values())[0]
        assert item["status"] == "approved"

    def test_items_pending_stat_counted(self):
        """Verify that collect_all counts pending items in stats."""
        from services.corporate_memory.collector import _process_catalog_response

        response_items = [
            {
                "existing_id": None,
                "title": f"Rule {i}",
                "content": f"Content {i}",
                "category": "workflow",
                "tags": [],
                "source_users": [USER_EMAIL],
            }
            for i in range(3)
        ]
        existing = {"items": {}}

        result = _process_catalog_response(
            response_items, existing, initial_status="pending",
        )

        pending_count = sum(
            1 for item in result.values() if item.get("status") == "pending"
        )
        assert pending_count == 3
