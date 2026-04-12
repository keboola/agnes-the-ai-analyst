"""Faker-based test data factories with deterministic seed."""

import hashlib
import hmac
import json
import uuid
from typing import Any

from faker import Faker

Faker.seed(42)
_fake = Faker()


class UserFactory:
    """Factory for user dicts matching UserRepository.create() signature."""

    @staticmethod
    def build(role: str = "analyst", **overrides) -> dict[str, Any]:
        """Build a user dict.

        Returns keys: id, email, name, role.
        Pass keyword overrides to replace any field.
        """
        data = {
            "id": str(uuid.uuid4()),
            "email": _fake.unique.email(),
            "name": _fake.name(),
            "role": role,
        }
        data.update(overrides)
        return data


class TableRegistryFactory:
    """Factory for table_registry entry dicts."""

    _SOURCE_TYPES = ["keboola", "bigquery", "csv"]
    _QUERY_MODES = ["local", "remote"]
    _SCHEDULES = ["0 * * * *", "0 6 * * *", "*/30 * * * *"]

    @staticmethod
    def build(**overrides) -> dict[str, Any]:
        """Build a table registry dict.

        Returns keys: name, source_type, bucket, source_table,
        query_mode, sync_schedule, description.
        """
        source_type = overrides.pop("source_type", _fake.random_element(TableRegistryFactory._SOURCE_TYPES))
        data = {
            "name": _fake.unique.slug().replace("-", "_"),
            "source_type": source_type,
            "bucket": f"in.c-{_fake.word()}",
            "source_table": _fake.word() + "_data",
            "query_mode": _fake.random_element(TableRegistryFactory._QUERY_MODES),
            "sync_schedule": _fake.random_element(TableRegistryFactory._SCHEDULES),
            "description": _fake.sentence(),
        }
        data["source_type"] = source_type
        data.update(overrides)
        return data


class KnowledgeItemFactory:
    """Factory for knowledge item dicts."""

    _CATEGORIES = ["business", "technical", "process", "metrics"]

    @staticmethod
    def build(**overrides) -> dict[str, Any]:
        """Build a knowledge item dict.

        Returns keys: title, content, category, tags.
        """
        data = {
            "title": _fake.sentence(nb_words=6).rstrip("."),
            "content": _fake.paragraph(nb_sentences=4),
            "category": _fake.random_element(KnowledgeItemFactory._CATEGORIES),
            "tags": [_fake.word() for _ in range(_fake.random_int(1, 4))],
        }
        data.update(overrides)
        return data


class WebhookEventFactory:
    """Factory for webhook event payloads."""

    @staticmethod
    def build_jira_event(
        event_type: str = "jira:issue_updated",
        issue_key: str | None = None,
        **overrides,
    ) -> dict[str, Any]:
        """Build a Jira webhook event payload dict.

        Args:
            event_type: Jira webhook event name, e.g. 'jira:issue_created'.
            issue_key: Issue key like 'PROJ-123'. Generated if not provided.
            **overrides: Top-level keys to override in the payload.

        Returns a dict matching the Jira webhook JSON structure.
        """
        if issue_key is None:
            project = _fake.lexify("????").upper()
            issue_key = f"{project}-{_fake.random_int(1, 9999)}"

        project_key = issue_key.split("-")[0]

        payload: dict[str, Any] = {
            "webhookEvent": event_type,
            "timestamp": _fake.unix_time() * 1000,
            "issue": {
                "id": str(_fake.random_int(10000, 99999)),
                "key": issue_key,
                "self": f"https://jira.example.com/rest/api/2/issue/{issue_key}",
                "fields": {
                    "summary": _fake.sentence(nb_words=8).rstrip("."),
                    "status": {
                        "name": _fake.random_element(["To Do", "In Progress", "Done"]),
                        "id": str(_fake.random_int(1, 10)),
                    },
                    "issuetype": {
                        "name": _fake.random_element(["Bug", "Story", "Task", "Epic"]),
                        "id": str(_fake.random_int(1, 10)),
                    },
                    "priority": {
                        "name": _fake.random_element(["Low", "Medium", "High", "Critical"]),
                    },
                    "assignee": {
                        "displayName": _fake.name(),
                        "emailAddress": _fake.email(),
                        "accountId": _fake.uuid4(),
                    },
                    "reporter": {
                        "displayName": _fake.name(),
                        "emailAddress": _fake.email(),
                        "accountId": _fake.uuid4(),
                    },
                    "project": {
                        "key": project_key,
                        "name": f"{project_key} Project",
                        "id": str(_fake.random_int(10000, 99999)),
                    },
                    "created": _fake.iso8601(),
                    "updated": _fake.iso8601(),
                    "description": _fake.paragraph(nb_sentences=2),
                    "labels": [_fake.word() for _ in range(_fake.random_int(0, 3))],
                },
            },
            "user": {
                "displayName": _fake.name(),
                "emailAddress": _fake.email(),
                "accountId": _fake.uuid4(),
            },
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def sign_payload(payload: dict[str, Any], secret: str) -> str:
        """Return HMAC-SHA256 signature string for a webhook payload.

        The signature is computed over the JSON-serialised payload (compact,
        sorted keys) and returned as a hex digest, matching the common Jira
        webhook signature scheme: 'sha256=<hex>'.
        """
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={sig}"
