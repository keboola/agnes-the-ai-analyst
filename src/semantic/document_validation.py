"""Validation of Apache Ossie semantic-model documents.

The JSON schema in ``schema/osi-schema.json`` is vendored from the upstream
Apache Ossie repository (incubating) and pinned deliberately: the project
describes its pre-release schema as mutable, so an upgrade is a reviewed
change with its own test run, never an implicit dependency bump.

Vendored from apache/ossie @ 88e0011148283302c9a04cd0287e00e0b9d87354
(``core-spec/osi-schema.json`` and ``tests/fixtures/ossie/tpcds_semantic_model.yaml``
were fetched from this same commit so the vendored example is guaranteed to
validate against the vendored schema).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from jsonschema import Draft202012Validator

_SCHEMA_DIR = Path(__file__).parent / "schema"
SPEC_VERSION: str = (_SCHEMA_DIR / "VERSION").read_text().strip()
_SCHEMA: Dict[str, Any] = json.loads((_SCHEMA_DIR / "osi-schema.json").read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass
class ValidationResult:
    ok: bool
    spec_version: str = SPEC_VERSION
    errors: List[str] = field(default_factory=list)
    parsed: Optional[Dict[str, Any]] = None


def validate_document(text: str) -> ValidationResult:
    """Parse and schema-check one Ossie document.

    Never raises on bad input — a malformed document is a result with
    ``ok=False``, because every caller stores the failure rather than
    aborting a whole sync over one bad file.
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, errors=[f"YAML parse error: {exc}"])

    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, errors=["Document root must be a mapping"])

    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in _VALIDATOR.iter_errors(parsed)
    ]
    if errors:
        return ValidationResult(ok=False, errors=sorted(errors))
    return ValidationResult(ok=True, parsed=parsed)
