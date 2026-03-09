"""
AI Data Analyst - Data Sync Engine

Downloads data from configured sources, converts to Parquet files,
and syncs to analysts via rsync.

Main modules:
- config: Configuration management and data_description.md parsing
- data_sync: Data synchronization orchestration and DataSource ABC
- parquet_manager: CSV -> Parquet conversion and file management

Data source connectors live in connectors/ (e.g. connectors/keboola/).

Note: DuckDB management is handled by scripts/duckdb_manager.py (analyst-side)
"""

__version__ = "0.1.0"
