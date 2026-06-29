"""Snapshot regression guard for the install-prompt renderer.

Captures the rendered output of ``resolve_lines`` against a known
fixture in ``tests/snapshots/install_prompt_default.txt``. The fixture
was generated post-A1.2 (manifest-driven, bundled-seed-backed) and any
unintended drift surfaces as a single diff instead of breaking dozens
of substring assertions.

To regenerate the fixture intentionally::

    .venv/bin/python -c "
    from app.web.setup_instructions import resolve_lines
    out = '\\n'.join(resolve_lines(
        'agnes-X.Y.Z-py3-none-any.whl',
        server_host='SERVER_HOST',
        instance_brand='BRAND',
        workspace_dir='WORKSPACE',
    )) + '\\n'
    open('tests/snapshots/install_prompt_default.txt', 'w').write(out)
    "

Commit the regenerated fixture in the same PR that changes the
rendering so reviewers see the user-visible diff in plain text.
"""

from __future__ import annotations

from pathlib import Path


_FIXTURE_PATH = (
    Path(__file__).parent / "snapshots" / "install_prompt_default.txt"
)


def test_install_prompt_matches_snapshot():
    """Render with deterministic inputs and compare against the fixture
    line-by-line so a diff narrows the regression to one section.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    rendered = "\n".join(
        resolve_lines(
            "agnes-X.Y.Z-py3-none-any.whl",
            server_host="SERVER_HOST",
            instance_brand="BRAND",
            workspace_dir="WORKSPACE",
        )
    ) + "\n"

    expected = _FIXTURE_PATH.read_text(encoding="utf-8")
    if rendered != expected:
        # Surface the first differing line + context so the reviewer
        # immediately sees which section changed.
        rendered_lines = rendered.splitlines()
        expected_lines = expected.splitlines()
        diff_lines = []
        for i, (r, e) in enumerate(zip(rendered_lines, expected_lines)):
            if r != e:
                diff_lines.append(f"  line {i + 1}: expected {e!r}, got {r!r}")
        if len(rendered_lines) != len(expected_lines):
            diff_lines.append(
                f"  length: expected {len(expected_lines)} lines, got {len(rendered_lines)}"
            )
        raise AssertionError(
            "install-prompt snapshot drift:\n"
            + "\n".join(diff_lines[:25])
            + (
                "\n  (showing first 25 differences; regenerate fixture if intentional — see module docstring)"
                if len(diff_lines) > 25
                else ""
            )
        )
