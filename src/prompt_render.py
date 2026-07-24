"""Sandboxed Jinja2 environment for admin/user-authored prompt templates.

Security audit F4: the install-prompt override, workspace overlay, and prompt
previews are authored via the admin API and rendered server-side. A plain
``jinja2.Environment`` is therefore an SSTI → RCE sink — an app-Admin who is
not an OS operator can execute arbitrary Python during ``render()`` (e.g.
``{{ cycler.__init__.__globals__['os'].popen('id').read() }}``). Output
sanitizers run *after* render and cannot stop render-time code execution.

**Every** server-side render of admin/user-authored prompt content MUST go
through :func:`make_prompt_env` so the ``SandboxedEnvironment`` blocks unsafe
attribute/builtin access. Do not construct a bare ``jinja2.Environment`` for
this content in a new call site — that reintroduces the hole one path at a time
(the original F4 fix sandboxed only ``/setup`` and left the sibling render
paths open; centralizing here prevents that recurrence).

``autoescape`` stays off (these render to prompt/script/markdown text, not
HTML) and ``StrictUndefined`` is preserved so an unknown placeholder still
errors loudly rather than silently rendering empty.
"""

from __future__ import annotations

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment


def make_prompt_env() -> SandboxedEnvironment:
    """Return the sandboxed Jinja2 environment for admin-authored prompts (F4)."""
    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
