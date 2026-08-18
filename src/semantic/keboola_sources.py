"""The Keboola semantic-layer writer source value(s).

The flat-table cutover (`connectors/keboola/semantic_layer.py::_sync_one_source`)
changed the ``source`` column stamped on `metric_definitions` / `glossary_terms`
rows from ``"keboola_semantic_layer"`` (the retired flat composer) to
``"keboola_metastore"`` (the Ossie-document projector,
`src.semantic.projection.project_document`).

Every reader that matches Keboola-written rows by `source` — counts, badges,
HTML-vs-markdown detection, orphaned-ref detection — must recognize BOTH
values, so an instance upgraded across the cutover keeps rendering/counting
pre-cutover rows correctly until the one-time legacy purge removes them (and
so a downgrade or partial sync leaves nothing silently invisible). Defined
once here — importable by both ``app/`` and ``connectors/`` without either
depending on the other's module tree — rather than each consumer hard-coding
its own copy of the retired literal.
"""

from __future__ import annotations

KEBOOLA_SEMANTIC_LAYER_SOURCES = frozenset({"keboola_metastore", "keboola_semantic_layer"})
