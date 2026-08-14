"""Importing already-fetched Ossie documents: content-hash no-op, invalid
documents stored without aborting the run, scoped prune of dropped models.

Fetching (git clone / upload / connection) is Task 9's `transports.py`; this
module exercises the pipeline with documents passed in directly.

Reuses the `e2e_env` DATA_DIR-isolation fixture from `tests/conftest.py`
under the `system_db` name the plan assumed, the same adaptation
`tests/test_semantic_projection.py` made for Task 7 — `tests/conftest.py` has
no fixture literally named `system_db`.
"""

import pytest

from src.repositories import semantic_model_repo
from src.semantic.importer import import_documents


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


SOURCE = {"id": "s1", "kind": "upload", "adapter": "native", "source": "git", "source_ref": "repo-a"}


def _doc(slug, metric="revenue"):
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "    metrics:\n"
        f"      - name: {metric}\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        "              expression: SUM(amount)\n"
    )


def test_unchanged_document_is_a_no_op_write(system_db):
    """Re-importing identical content must not bump updated_at."""
    import_documents(SOURCE, [_doc("retail")])
    first = semantic_model_repo().get_by_slug("retail")["updated_at"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_unchanged == 1
    assert report.models_written == 0
    assert semantic_model_repo().get_by_slug("retail")["updated_at"] == first


def test_invalid_document_is_stored_with_its_errors_and_does_not_abort_the_run(system_db):
    """One bad file must not cost the sync its good files."""
    report = import_documents(SOURCE, [_doc("retail"), "semantic_model: [oops"])

    assert report.models_written == 1
    assert len(report.invalid) == 1
    assert semantic_model_repo().get_by_slug("retail") is not None


def test_document_dropped_upstream_is_pruned(system_db):
    import_documents(SOURCE, [_doc("retail"), _doc("finance")])
    dropped = semantic_model_repo().get_by_slug("finance")["id"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_pruned == [dropped]
    assert semantic_model_repo().get_by_slug("finance") is None
    assert semantic_model_repo().get_by_slug("retail") is not None
