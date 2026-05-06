"""Shared constants for the Claude Code marketplace clone.

`agnes init` (via setup_instructions) clones the per-user filtered
marketplace bare-repo to `~/.agnes/marketplace`, then registers that path
with Claude Code via `claude plugin marketplace add <path>`. The marketplace
is named "agnes" inside Claude Code's registry.

Both the clone path and the registry name are referenced from multiple
places (`agnes refresh-marketplace`, future `agnes init` automation, the
clipboard-copied setup script in `app/web/setup_instructions.py`). Having
them as constants here keeps them in sync — drift between the setup script
and the refresh command would silently break the refresh flow.

The setup-instructions clipboard text MUST keep the literal string
`~/.agnes/marketplace` for the clone target so users can copy-paste without
needing the agnes CLI to be installed yet (chicken-and-egg). The CLI side
uses `Path.home() / ".agnes" / "marketplace"` for portability.
"""

from __future__ import annotations

from pathlib import Path

# Filesystem location of the marketplace clone. Synchronized with
# `app/web/setup_instructions.py:_marketplace_block` which writes the
# literal `~/.agnes/marketplace` into the clipboard-copied setup script.
CLONE_DIR: Path = Path.home() / ".agnes" / "marketplace"

# The marketplace name as registered in Claude Code (`claude plugin
# marketplace list` shows this). Must match
# `app.marketplace_server.packager.MARKETPLACE_NAME` server-side and the
# `_MARKETPLACE_NAME` literal in `setup_instructions.py`.
MARKETPLACE_NAME: str = "agnes"
