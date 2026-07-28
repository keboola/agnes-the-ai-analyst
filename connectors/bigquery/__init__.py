"""
BigQuery connector - data source adapter for Google BigQuery.

Syncs tables from BigQuery using the BigQuery Storage API,
converting query results directly to Parquet files via PyArrow
(no CSV intermediate step).

Enable by setting data_source.type: "bigquery" in config/instance.yaml
and providing BIGQUERY_PROJECT environment variable.
Uses Application Default Credentials (ADC) for authentication.
"""
