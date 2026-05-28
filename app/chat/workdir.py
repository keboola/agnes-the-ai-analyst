"""Per-user workspace and per-session working-directory lifecycle."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from src.initial_workspace import (
    TemplateStatus,
    initialize_default_workspace,
    initialize_workspace_from_template,
)

from app.chat.persistence import ChatRepository

logger = logging.getLogger(__name__)


def _safe_email_dir(email: str) -> str:
    """Email → directory-safe slug. Lowercase, replace non-[a-z0-9_-.@] with '_'."""
    return "".join(c if c.isalnum() or c in "._-@" else "_" for c in email.lower())


class WorkdirManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        repo: ChatRepository,
        bundled_template_dir: Path,
        server_url: str,
        agnes_version: str,
        get_marketplace_sha: Callable[[], str],
        get_template_status: Callable[[], Optional[TemplateStatus]],
        fetch_template_zip: Optional[Callable[[], bytes]] = None,
    ) -> None:
        self._data_dir = data_dir
        self._repo = repo
        self._bundled_template_dir = bundled_template_dir
        self._server_url = server_url
        self._agnes_version = agnes_version
        self._get_marketplace_sha = get_marketplace_sha
        self._get_template_status = get_template_status
        self._fetch_template_zip = fetch_template_zip

    def _user_root(self, user_email: str) -> Path:
        return self._data_dir / "users" / _safe_email_dir(user_email)

    def user_workspace(self, user_email: str) -> Path:
        return self._user_root(user_email) / "workspace"

    def user_sessions_root(self, user_email: str) -> Path:
        return self._user_root(user_email) / "sessions"

    def needs_reinit(self, user_email: str) -> bool:
        row = self._repo.get_workdir(user_email)
        if row is None:
            return True
        if row.marketplace_sha != self._get_marketplace_sha():
            return True
        if row.agnes_version_at_init != self._agnes_version:
            return True
        return False

    def ensure_user_workdir(self, user_email: str) -> Path:
        ws = self.user_workspace(user_email)
        ws.mkdir(parents=True, exist_ok=True)
        sentinel = ws / ".claude" / "init-complete"
        if sentinel.exists() and not self.needs_reinit(user_email):
            return ws

        self.run_init(user_email, ws)
        return ws

    def run_init(self, user_email: str, workspace: Optional[Path] = None) -> None:
        ws = workspace or self.user_workspace(user_email)
        status = self._get_template_status()
        template_sha = None
        if status and status.configured and status.synced and self._fetch_template_zip is not None:
            zip_bytes = self._fetch_template_zip()
            initialize_workspace_from_template(
                ws, zip_bytes,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                template_source=status.template_source,
                template_sha=status.template_sha,
            )
            template_sha = status.template_sha
        else:
            initialize_default_workspace(
                ws,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                bundled_template_dir=self._bundled_template_dir,
            )

        self._repo.upsert_workdir(
            user_email=user_email,
            marketplace_sha=self._get_marketplace_sha(),
            initial_workspace_sha=template_sha,
            agnes_version=self._agnes_version,
        )
        logger.info("workdir initialized: user=%s template_sha=%s", user_email, template_sha)

    def prepare_session_dir(self, user_email: str, chat_id: str) -> Path:
        sessions_root = self.user_sessions_root(user_email)
        sessions_root.mkdir(parents=True, exist_ok=True)
        sdir = sessions_root / chat_id
        sdir.mkdir(parents=True, exist_ok=True)
        # Symlink shared workspace state into the session dir so
        # claude-agent-sdk resolves .claude/{skills,plugins,agents,commands,hooks}
        # against the per-user workspace.
        ws = self.user_workspace(user_email)
        for entry in (".claude", "CLAUDE.md", "CLAUDE.local.md", "snapshots", "scripts"):
            link = sdir / entry
            target = ws / entry
            if not target.exists():
                continue
            if not link.exists():
                link.symlink_to(target)
        (sdir / "work").mkdir(exist_ok=True)
        return sdir

    def purge_user(self, user_email: str) -> int:
        """GDPR hard-delete. Returns file count removed."""
        import shutil
        root = self._user_root(user_email)
        if not root.exists():
            return 0
        count = sum(1 for _ in root.rglob("*") if _.is_file())
        shutil.rmtree(root)
        self._repo.delete_workdir_row(user_email)
        return count
