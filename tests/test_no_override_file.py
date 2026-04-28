"""Regression test: docker-compose.override.yml must not exist (issue #87/M23).

Docker Compose auto-merges docker-compose.override.yml when present,
silently enabling dev mode on any host with the repo. The file was renamed
to docker-compose.dev.yml which requires explicit -f flag.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_override_file():
    """docker-compose.override.yml must not exist — it was renamed to .dev.yml."""
    assert not (REPO_ROOT / "docker-compose.override.yml").exists(), \
        "docker-compose.override.yml must not exist (renamed to docker-compose.dev.yml per #87/M23)"
