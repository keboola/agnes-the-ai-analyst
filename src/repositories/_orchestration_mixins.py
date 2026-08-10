"""Backend-neutral repository mixins.

Some repository methods are pure orchestration on top of the repo's own
public CRUD (``create`` / ``save`` / ``list``) — they touch the filesystem
(YAML / JSON import-export) but never the database directly. Those bodies are
*identical* for DuckDB and Postgres, so duplicating them into both ``X.py``
and ``X_pg.py`` only invites the drift class that bit us in #499/#513 (a
method living on the DuckDB repo but missing on the PG one, crashing once a
Postgres-backed instance goes live).

Defining them once here and mixing them into both repo classes makes the
parity structural: there is only one implementation, so it cannot diverge.
The mixins depend solely on public methods the cross-engine parity guard
(``tests/db_pg/test_repo_method_parity.py``) already pins on both backends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


class MetricYamlMixin:
    """`import_from_yaml` / `export_to_yaml` for the metrics repo.

    Uses only ``self.create(...)`` and ``self.list()`` — both backends expose
    them with identical signatures (guarded), so this works unchanged on
    DuckDB and Postgres.
    """

    def import_from_yaml(self, path: Union[str, Path]) -> int:
        """Import metrics from a YAML file or directory of YAML files.

        Upsert-only: the registry can grow but never shrink. Use
        :meth:`reconcile_from_yaml` when the directory is meant to be the
        source of truth.

        Args:
            path: Path to a single .yml file or a directory containing */*.yml files.

        Returns:
            Number of metrics imported.
        """
        return len(self.reconcile_from_yaml(path)["written"])

    def reconcile_from_yaml(
        self,
        path: Union[str, Path],
        *,
        source_ref: Optional[str] = None,
        prune: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, List[str]]:
        """Import metrics, optionally making the registry MATCH the directory.

        ``import_from_yaml`` only ever adds, so a metric deleted upstream stays
        forever and a rename leaves both ids behind (#1219). ``prune=True``
        removes the rows this importer previously wrote that the directory no
        longer contains.

        **What prune may delete is keyed on the writer, never on the id.** The
        scope is ``source='yaml_import'`` — so a metric an admin wrote in the
        UI (``manual``) or another importer created (``keboola_semantic_layer``)
        is out of reach by construction, not by luck. ``source_ref`` narrows it
        further: two YAML exports into one instance both carry
        ``source='yaml_import'``, so without a second key the later import
        would delete the earlier one's metrics. Passing it also stamps the rows,
        so the scope exists from the first run.

        A rename is indistinguishable from delete + create at this level — the
        id is ``category/name`` — which is precisely why ``dry_run=True``
        exists: it returns the same report and writes nothing.

        Returns a report of metric ids: ``added`` (not previously in scope),
        ``updated`` (already there), ``written`` (both, in file order) and
        ``deleted`` (pruned, empty unless ``prune``).
        """
        path = Path(path)
        files: List[Path] = []

        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = sorted(path.glob("*/*.yml"))

        # Ids this importer owns right now — the only rows prune may remove.
        in_scope = {
            m["id"]
            for m in self.list()
            if (m.get("source") or "") == "yaml_import"
            and (source_ref is None or (m.get("source_ref") or "") == source_ref)
        }

        written: List[str] = []
        for file_path in files:
            # Infer category from parent directory name
            category_from_dir = file_path.parent.name

            with open(file_path, "r") as f:
                raw = yaml.safe_load(f)

            # Support both list-wrapped [{...}] and plain {...} formats
            if isinstance(raw, list):
                metrics_data = raw
            elif isinstance(raw, dict):
                metrics_data = [raw]
            else:
                continue

            for data in metrics_data:
                if not isinstance(data, dict):
                    continue

                name = data.get("name")
                if not name:
                    continue

                category = data.get("category") or category_from_dir

                # Build id as "category/name"
                metric_id = f"{category}/{name}"

                # Map YAML 'table' -> DB 'table_name'
                table_name = data.get("table") or data.get("table_name")

                # Collect sql_by_* keys into sql_variants dict
                # e.g. sql_by_channel → {"by_channel": "..."}
                sql_variants: Dict[str, str] = {}
                for key, value in data.items():
                    if key.startswith("sql_by_"):
                        variant_key = key[len("sql_") :]  # strip 'sql_' prefix → 'by_channel'
                        sql_variants[variant_key] = value

                written.append(metric_id)
                if dry_run:
                    continue

                self.create(
                    id=metric_id,
                    name=name,
                    display_name=data.get("display_name", name),
                    category=category,
                    sql=data.get("sql", ""),
                    description=data.get("description"),
                    type=data.get("type", "sum"),
                    unit=data.get("unit"),
                    grain=data.get("grain", "monthly"),
                    table_name=table_name,
                    tables=data.get("tables"),
                    expression=data.get("expression"),
                    time_column=data.get("time_column"),
                    dimensions=data.get("dimensions"),
                    filters=data.get("filters"),
                    synonyms=data.get("synonyms"),
                    notes=data.get("notes"),
                    sql_variants=sql_variants if sql_variants else None,
                    validation=data.get("validation"),
                    source="yaml_import",
                    source_ref=source_ref,
                )

        added = [mid for mid in written if mid not in in_scope]
        updated = [mid for mid in written if mid in in_scope]
        deleted = sorted(in_scope - set(written)) if prune else []

        if not dry_run:
            for metric_id in deleted:
                self.delete(metric_id)

        return {"added": added, "updated": updated, "written": written, "deleted": deleted}

    def export_to_yaml(self, output_dir: Union[str, Path]) -> int:
        """Export all metrics to YAML files under output_dir/{category}/{name}.yml.

        Args:
            output_dir: Root directory for the exported YAML files.

        Returns:
            Number of metrics exported.
        """
        output_dir = Path(output_dir)
        metrics = self.list()
        count = 0

        for metric in metrics:
            category = metric.get("category") or "uncategorized"
            name = metric.get("name") or metric["id"].split("/")[-1]

            category_dir = output_dir / category
            category_dir.mkdir(parents=True, exist_ok=True)

            # Build the YAML dict — map table_name back to table
            data: Dict[str, Any] = {"name": name}
            if metric.get("display_name"):
                data["display_name"] = metric["display_name"]
            data["category"] = category
            if metric.get("type"):
                data["type"] = metric["type"]
            if metric.get("unit"):
                data["unit"] = metric["unit"]
            if metric.get("grain"):
                data["grain"] = metric["grain"]
            if metric.get("time_column"):
                data["time_column"] = metric["time_column"]
            # Use 'table' (not 'table_name') in YAML output
            if metric.get("table_name"):
                data["table"] = metric["table_name"]
            if metric.get("expression"):
                data["expression"] = metric["expression"]
            if metric.get("description"):
                data["description"] = metric["description"]
            if metric.get("dimensions"):
                data["dimensions"] = metric["dimensions"]
            if metric.get("filters"):
                data["filters"] = metric["filters"]
            if metric.get("synonyms"):
                data["synonyms"] = metric["synonyms"]
            if metric.get("notes"):
                data["notes"] = metric["notes"]
            if metric.get("sql"):
                data["sql"] = metric["sql"]

            # Expand sql_variants back to sql_by_* keys
            sql_variants = metric.get("sql_variants")
            if sql_variants:
                if isinstance(sql_variants, str):
                    try:
                        sql_variants = json.loads(sql_variants)
                    except (json.JSONDecodeError, ValueError):
                        sql_variants = {}
                if isinstance(sql_variants, dict):
                    for variant_key, variant_sql in sql_variants.items():
                        # variant_key is e.g. 'by_channel' → YAML key 'sql_by_channel'
                        data[f"sql_{variant_key}"] = variant_sql

            # Handle validation JSON
            validation = metric.get("validation")
            if validation:
                if isinstance(validation, str):
                    try:
                        validation = json.loads(validation)
                    except (json.JSONDecodeError, ValueError):
                        validation = None
                if validation:
                    data["validation"] = validation

            out_file = category_dir / f"{name}.yml"
            with open(out_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            count += 1

        return count


class ColumnMetadataImportMixin:
    """`import_proposal` for the column-metadata repo.

    Uses only ``self.save(...)`` — identical on both backends (guarded).
    """

    def import_proposal(self, proposal_path: str) -> int:
        """Import a proposal JSON file.

        Format:
            {
                "tables": {
                    "orders": {
                        "columns": {
                            "id": {"basetype": "STRING", "description": "...", "confidence": "high"}
                        }
                    }
                }
            }

        Sets source="ai_enrichment". Returns count of columns imported.
        """
        with open(proposal_path, "r", encoding="utf-8") as f:
            proposal = json.load(f)

        count = 0
        tables = proposal.get("tables", {})
        for table_id, table_data in tables.items():
            columns = table_data.get("columns", {})
            for column_name, col_data in columns.items():
                self.save(
                    table_id=table_id,
                    column_name=column_name,
                    basetype=col_data.get("basetype"),
                    description=col_data.get("description"),
                    confidence=col_data.get("confidence", "high"),
                    source="ai_enrichment",
                )
                count += 1
        return count
