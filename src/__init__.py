"""
AI Data Analyst - Data Sync Engine

Downloads data from configured sources, converts to Parquet files,
and syncs to analysts via rsync.

Main modules:
- config: Configuration management and data_description.md parsing
- adapters: Pluggable data source adapters (Keboola, CSV, etc.)
- parquet_manager: CSV -> Parquet conversion and file management
- data_sync: Data synchronization orchestration

Note: DuckDB management is handled by scripts/duckdb_manager.py (analyst-side)
"""

__version__ = "0.1.0"
