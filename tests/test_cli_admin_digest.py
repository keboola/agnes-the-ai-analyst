"""CLI tests for `agnes admin digest` subcommands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestList:
    def test_list_text(self):
        rows = [
            {"id": "kd_1", "slug": "arch", "title": "Architecture", "status": "fresh", "generated_at": "2026-07-01"},
            {"id": "kd_2", "slug": "onboarding", "title": "Onboarding", "status": "pending", "generated_at": None},
        ]
        with patch(
            "cli.commands.admin_digest.api_get",
            return_value=_resp(200, {"items": rows}),
        ):
            result = runner.invoke(app, ["admin", "digest", "list"])
        assert result.exit_code == 0
        assert "Architecture" in result.output
        assert "Onboarding" in result.output
        assert "Maintained digests: 2" in result.output

    def test_list_json(self):
        rows = [{"id": "kd_1", "slug": "arch", "title": "Architecture", "status": "fresh"}]
        with patch(
            "cli.commands.admin_digest.api_get",
            return_value=_resp(200, {"items": rows}),
        ):
            result = runner.invoke(app, ["admin", "digest", "list", "--json"])
        data = json.loads(result.output)
        assert data[0]["slug"] == "arch"


class TestShow:
    def test_show_fresh(self):
        d = {
            "id": "kd_1",
            "slug": "arch",
            "title": "Architecture",
            "source_corpus_ids": ["col_a"],
            "model": "claude-x",
            "generated_at": "2026-07-01",
            "status": "fresh",
            "status_reason": None,
            "instructions": "Summarize the architecture.",
            "output_md": "# Architecture\n\nSome content.",
        }
        with patch(
            "cli.commands.admin_digest.api_get",
            return_value=_resp(200, d),
        ):
            result = runner.invoke(app, ["admin", "digest", "show", "kd_1"])
        assert result.exit_code == 0
        assert "Some content." in result.output
        assert "STALE" not in result.output

    def test_show_stale_prints_reason_prominently(self):
        d = {
            "id": "kd_1",
            "slug": "arch",
            "title": "Architecture",
            "source_corpus_ids": [],
            "model": None,
            "generated_at": "2026-07-01",
            "status": "stale",
            "status_reason": "LLM timeout",
            "instructions": "Summarize.",
            "output_md": "# Old content",
        }
        with patch(
            "cli.commands.admin_digest.api_get",
            return_value=_resp(200, d),
        ):
            result = runner.invoke(app, ["admin", "digest", "show", "kd_1"])
        assert result.exit_code == 0
        assert "STALE — LLM timeout" in result.output
        # Old content is still shown — never wiped.
        assert "Old content" in result.output

    def test_show_resolves_slug_to_id(self):
        get_responses = [
            _resp(404, {"detail": "knowledge_digest_not_found"}),
            _resp(200, {"items": [{"id": "kd_42", "slug": "arch", "title": "Architecture"}]}),
            _resp(
                200,
                {
                    "id": "kd_42",
                    "slug": "arch",
                    "title": "Architecture",
                    "source_corpus_ids": [],
                    "model": None,
                    "generated_at": None,
                    "status": "pending",
                    "status_reason": None,
                    "instructions": "x",
                    "output_md": None,
                },
            ),
        ]
        with patch("cli.commands.admin_digest.api_get", side_effect=get_responses):
            result = runner.invoke(app, ["admin", "digest", "show", "arch"])
        assert result.exit_code == 0
        assert "kd_42" in result.output


class TestCreate:
    def test_create_success_with_instructions(self):
        with patch(
            "cli.commands.admin_digest.api_post",
            return_value=_resp(201, {"id": "kd_new"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "digest",
                    "create",
                    "--slug",
                    "arch",
                    "--title",
                    "Architecture",
                    "--instructions",
                    "Summarize the architecture.",
                    "--source",
                    "col_a",
                    "--source",
                    "col_b",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {
            "slug": "arch",
            "title": "Architecture",
            "instructions": "Summarize the architecture.",
            "source_corpus_ids": ["col_a", "col_b"],
        }
        assert "kd_new" in result.output

    def test_create_success_with_instructions_file(self, tmp_path):
        f = tmp_path / "instructions.txt"
        f.write_text("Summarize from a file.\n", encoding="utf-8")
        with patch(
            "cli.commands.admin_digest.api_post",
            return_value=_resp(201, {"id": "kd_new"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "digest",
                    "create",
                    "--slug",
                    "arch",
                    "--title",
                    "Architecture",
                    "--instructions-file",
                    str(f),
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["instructions"] == "Summarize from a file."

    def test_create_requires_instructions(self):
        result = runner.invoke(
            app,
            ["admin", "digest", "create", "--slug", "arch", "--title", "Architecture"],
        )
        assert result.exit_code == 2

    def test_create_rejects_both_instructions_forms(self, tmp_path):
        f = tmp_path / "instructions.txt"
        f.write_text("From file", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "admin",
                "digest",
                "create",
                "--slug",
                "arch",
                "--title",
                "Architecture",
                "--instructions",
                "Inline",
                "--instructions-file",
                str(f),
            ],
        )
        assert result.exit_code == 2

    def test_create_slug_conflict(self):
        with patch(
            "cli.commands.admin_digest.api_post",
            return_value=_resp(409, {"detail": "slug_exists"}),
        ):
            result = runner.invoke(
                app,
                ["admin", "digest", "create", "--slug", "dup", "--title", "X", "--instructions", "i"],
            )
        assert result.exit_code == 1
        assert "slug_exists" in result.output


class TestEdit:
    def test_edit_updates_title(self):
        with (
            patch(
                "cli.commands.admin_digest.api_get",
                return_value=_resp(200, {"id": "kd_1", "slug": "arch", "title": "old"}),
            ),
            patch(
                "cli.commands.admin_digest.api_put",
                return_value=_resp(200, {}),
            ) as m,
        ):
            result = runner.invoke(app, ["admin", "digest", "edit", "kd_1", "--title", "New"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"title": "New"}

    def test_edit_resolves_slug_to_id(self):
        get_responses = [
            _resp(404, {"detail": "knowledge_digest_not_found"}),
            _resp(200, {"items": [{"id": "kd_42", "slug": "arch", "title": "Architecture"}]}),
        ]
        with (
            patch(
                "cli.commands.admin_digest.api_get",
                side_effect=get_responses,
            ),
            patch(
                "cli.commands.admin_digest.api_put",
                return_value=_resp(200, {}),
            ) as m_put,
        ):
            result = runner.invoke(app, ["admin", "digest", "edit", "arch", "--title", "X"])
        assert result.exit_code == 0
        assert "kd_42" in m_put.call_args.args[0]

    def test_edit_no_fields_fails(self):
        result = runner.invoke(app, ["admin", "digest", "edit", "kd_1"])
        assert result.exit_code == 2
        assert "at least one" in result.output.lower()

    def test_edit_updates_sources(self):
        with (
            patch(
                "cli.commands.admin_digest.api_get",
                return_value=_resp(200, {"id": "kd_1", "slug": "arch", "title": "old"}),
            ),
            patch(
                "cli.commands.admin_digest.api_put",
                return_value=_resp(200, {}),
            ) as m,
        ):
            result = runner.invoke(
                app,
                ["admin", "digest", "edit", "kd_1", "--source", "col_c"],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"source_corpus_ids": ["col_c"]}


class TestDelete:
    def test_delete_requires_confirm_without_yes(self):
        with (
            patch(
                "cli.commands.admin_digest.api_get",
                return_value=_resp(200, {"id": "kd_1", "slug": "arch", "title": "n"}),
            ),
            patch(
                "cli.commands.admin_digest.api_delete",
            ) as m,
        ):
            result = runner.invoke(app, ["admin", "digest", "delete", "kd_1"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_delete_with_yes_calls_delete(self):
        with (
            patch(
                "cli.commands.admin_digest.api_get",
                return_value=_resp(200, {"id": "kd_1", "slug": "arch", "title": "n"}),
            ),
            patch(
                "cli.commands.admin_digest.api_delete",
                return_value=_resp(204),
            ) as m,
        ):
            result = runner.invoke(app, ["admin", "digest", "delete", "kd_1", "--yes"])
        assert result.exit_code == 0
        m.assert_called_once()
