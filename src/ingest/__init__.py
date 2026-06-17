"""Collections ingestion (Slice 3) — turn uploaded files into queryable knowledge.

Tabular files become DuckDB tables registered in ``table_registry``; prose
documents become ``corpus_chunks`` rows (text only — embeddings are Slice 4).
The router ``ingest_file`` (``runner.py``) drives the per-file lifecycle.
"""
