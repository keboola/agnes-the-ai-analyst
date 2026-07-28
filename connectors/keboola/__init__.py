"""
Keboola connector - data source adapter for Keboola Storage API.

Syncs tables from Keboola Storage via the Storage API, converting
CSV exports to Parquet files with full type metadata.

Enable by setting data_source.type: "keboola" in config/instance.yaml
and providing KEBOOLA_* environment variables.
"""
