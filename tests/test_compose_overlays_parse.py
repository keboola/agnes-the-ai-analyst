"""Every docker-compose overlay in the repo must be valid YAML with no
duplicate mapping keys.

Why this guard exists: `docker-compose.host-mount.yml` shipped with two
`apps-runner:` blocks under `services:` (the second added by a PR that did not
notice the service was already overridden 40 lines above). Compose rejects a
duplicate mapping key outright — `failed to parse …: mapping key "apps-runner"
already defined` — so EVERY `docker compose` invocation failed on every
deployed VM, including the one the auto-upgrader runs. Instances froze on
whatever image they had.

The blast radius is the reason this is a repo-level guard rather than a deploy
check: `agnes-auto-upgrade.sh` re-fetches these overlays from the OSS `main`
branch on every 5-minute tick, so a malformed file reaches running instances
within minutes of merge, with no release and no rollback path.

PyYAML is the wrong tool by default — `yaml.safe_load` silently keeps the LAST
duplicate key instead of raising, which is exactly how this reached main. The
loader below rejects duplicates the way compose-go does, and tolerates the
Compose-spec tags (`!override`, `!reset`) that carry no meaning to PyYAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class _DuplicateKeyError(Exception):
    pass


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader that raises on duplicate mapping keys and passes Compose tags."""


def _no_duplicate_keys(loader: ComposeLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                f"(first defined earlier in the same mapping)"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


def _passthrough_tag(loader: ComposeLoader, tag_suffix: str, node: yaml.Node):
    """`!override` / `!reset` are Compose-spec merge tags — structure only."""
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return _no_duplicate_keys(loader, node)
    return loader.construct_scalar(node)


ComposeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)
ComposeLoader.add_multi_constructor("!", _passthrough_tag)


def _compose_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("docker-compose*.yml"))


def test_compose_files_are_discovered() -> None:
    """Guard the guard: a glob that matches nothing would pass every case below."""
    found = _compose_files()
    assert len(found) >= 5, f"expected the compose overlays at the repo root, found {found}"
    names = {p.name for p in found}
    assert "docker-compose.yml" in names
    assert "docker-compose.host-mount.yml" in names


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_compose_overlay_parses_without_duplicate_keys(path: Path) -> None:
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)
    except _DuplicateKeyError as exc:
        pytest.fail(
            f"{path.name}: {exc}\n"
            "Compose rejects a duplicate mapping key outright, and these overlays are "
            "fetched from main by every deployed instance — a duplicate here stops the "
            "fleet from upgrading. Merge the two blocks into one service entry."
        )
    except yaml.YAMLError as exc:
        pytest.fail(f"{path.name} is not valid YAML: {exc}")

    assert isinstance(doc, dict), f"{path.name}: top level must be a mapping"


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_compose_overlay_service_names_are_unique(path: Path) -> None:
    """A duplicate `services:` key is the specific shape that broke the fleet —
    assert it on the raw text too, so the check survives any future loader swap."""
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: dict[str, int] = {}
    in_services = False
    for i, line in enumerate(lines, start=1):
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            in_services = line.startswith("services:")
            continue
        if not in_services:
            continue
        # A service key is exactly two spaces deep and not a comment.
        if line.startswith("  ") and not line.startswith("   ") and not line.lstrip().startswith("#"):
            stripped = line.strip()
            if stripped.endswith(":"):
                name = stripped[:-1]
                assert name not in seen, (
                    f"{path.name}: service {name!r} defined twice "
                    f"(lines {seen[name]} and {i}) — compose refuses to parse the file"
                )
                seen[name] = i
