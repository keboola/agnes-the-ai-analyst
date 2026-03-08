"""
Data source adapter factory.

Creates data source instances based on adapter type configuration.
"""

from ..data_sync import DataSource


def create_data_source(adapter_type: str, **kwargs) -> DataSource:
    """Create a data source adapter instance.

    Args:
        adapter_type: Type of adapter ("keboola", "csv", "bigquery")
        **kwargs: Additional configuration for the adapter

    Returns:
        DataSource instance

    Raises:
        ValueError: If adapter type is unknown
        ImportError: If adapter dependencies are not installed
    """
    if adapter_type == "keboola":
        try:
            from .keboola_adapter import KeboolaDataSource
        except ImportError as e:
            raise ImportError(
                f"Keboola adapter requires 'kbcstorage' package. "
                f"Install with: pip install kbcstorage"
            ) from e
        return KeboolaDataSource(**kwargs)

    raise ValueError(
        f"Unknown data source adapter: '{adapter_type}'. "
        f"Available adapters: keboola"
    )
