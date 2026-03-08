"""
Keboola Storage API data source adapter.

Wraps the existing KeboolaClient and LocalKeboolaSource
as a proper adapter following the DataSource interface.
"""

# Re-export the existing implementation under the adapter namespace
from ..data_sync import LocalKeboolaSource as KeboolaDataSource

__all__ = ["KeboolaDataSource"]
