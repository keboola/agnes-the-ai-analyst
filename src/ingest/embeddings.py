"""Text embeddings for Collections retrieval.

Self-hosted, OPTIONAL. The default deployment ships WITHOUT an embedding model
(``sentence-transformers`` pulls torch — too heavy for the core image), so this
module degrades gracefully: when the model isn't importable, ``embed_texts``
returns ``None`` and retrieval falls back to lexical (BM25/term) search only.
Install with ``agnes[embeddings]`` to enable semantic search.

Dimension is fixed at 384 (``bge-small-en-v1.5``) — the ``corpus_chunks.embedding``
column is ``FLOAT[384]`` on DuckDB; the PG side validates length at write time.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

EMBED_DIM = 384
_MODEL_NAME = os.environ.get("AGNES_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

_model = None  # lazily loaded SentenceTransformer (or False once known-absent)


def _load_model():
    """Return a cached embedding model, or None if the extra isn't installed."""
    global _model
    if _model is not None:
        return _model or None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        logger.info("embeddings: sentence-transformers not installed — lexical-only retrieval")
        _model = False
        return None
    try:
        _model = SentenceTransformer(_MODEL_NAME)
        return _model
    except Exception as exc:  # pragma: no cover - model-download/runtime issues
        logger.warning("embeddings: failed to load model %s: %s", _MODEL_NAME, exc)
        _model = False
        return None


def embedding_available() -> bool:
    """True when an embedding model is loadable (the ``embeddings`` extra is in)."""
    return _load_model() is not None


def embedding_capability() -> bool:
    """Cheap availability probe that NEVER triggers a model load.

    Response-labeling paths (``retrieval_mode`` in ``src/ingest/retrieval.py``)
    call this on every search request — including ones where no chunk ranking
    runs at all — so it must not pay ``_load_model``'s instantiation/download
    cost just to compute a label. Uses the already-resolved state when
    ``_load_model`` has run (the truth), else an import-spec probe: the extra
    being importable is what separates hybrid from lexical-only deployments.
    The one edge this can mislabel is an importable extra whose model fails to
    load at runtime — the first real embed call resolves ``_model`` to False
    and subsequent labels self-correct.
    """
    if _model is not None:
        return bool(_model)
    return importlib.util.find_spec("sentence_transformers") is not None


def embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """Embed texts → list of 384-float vectors, or None when unavailable.

    Returning None (not raising) is deliberate: callers treat "no embeddings"
    as "lexical-only", never as an error.
    """
    if not texts:
        return []
    model = _load_model()
    if model is None:
        return None
    vectors = model.encode(list(texts), normalize_embeddings=True)
    return [[float(x) for x in row] for row in vectors]


def embed_query(text: str) -> Optional[List[float]]:
    """Embed a single query string, or None when unavailable."""
    out = embed_texts([text])
    if not out:
        return None
    return out[0]
