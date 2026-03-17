# AI Data Analyst - Development Makefile
#
# Usage:
#   make              - show help
#   make test         - run all tests
#   make test-config  - run config-related tests only
#   make validate-config CONFIG_DIR=path/to/config  - validate data_description.md
#   make lint         - placeholder for future linting

SHELL   := /bin/bash
PYTHON  := .venv/bin/python
PYTEST  := .venv/bin/pytest

# Optional: path to config directory containing data_description.md
# Default: config/ (relative to project root)
CONFIG_DIR ?= config

.PHONY: help test test-config validate-config lint

# Default target
help:
	@echo "Available targets:"
	@echo "  make test              Run all pytest tests"
	@echo "  make test-config       Run config and scheduler tests only"
	@echo "  make validate-config   Validate data_description.md parsing"
	@echo "                         Optional: CONFIG_DIR=path/to/config (default: config/)"
	@echo "  make lint              Placeholder for future linting"
	@echo ""
	@echo "Prerequisites: Python virtualenv at .venv/ with dependencies installed"

test:
	$(PYTEST)

test-config:
	$(PYTEST) tests/test_config_query_mode.py tests/test_config_sync_schedule.py tests/test_scheduler.py -v

define VALIDATE_SCRIPT
import os, sys, re, tempfile, shutil
from pathlib import Path

config_dir = Path(os.environ.get("CONFIG_DIR", "config"))
config_file = config_dir / "data_description.md"
if not config_file.exists():
    print("FAIL: %s not found" % config_file, file=sys.stderr)
    sys.exit(1)

# Ensure docs/data_description.md exists so Config._find_project_root() works.
# If CONFIG_DIR points elsewhere, create a temporary symlink.
docs_path = Path("docs/data_description.md")
created_symlink = False
if not docs_path.exists():
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.symlink_to(config_file.resolve())
    created_symlink = True

try:
    from src.config import Config
    c = Config()
    names = ", ".join(t.name for t in c.tables)
    print("OK: parsed %d table(s): %s" % (len(c.tables), names))
except Exception as e:
    print("FAIL: %s" % e, file=sys.stderr)
    sys.exit(1)
finally:
    if created_symlink:
        docs_path.unlink(missing_ok=True)
endef
export VALIDATE_SCRIPT

validate-config:
	@echo "Validating data_description.md in CONFIG_DIR=$(CONFIG_DIR) ..."
	@CONFIG_DIR=$(CONFIG_DIR) $(PYTHON) -c "$$VALIDATE_SCRIPT"

lint:
	@echo "Linting not configured yet. Add ruff, flake8, or similar here."
