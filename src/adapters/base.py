"""
Base data source interface.

The DataSource ABC is defined in data_sync.py and re-exported here
for convenient access: `from src.adapters.base import DataSource`
"""

from ..data_sync import DataSource

__all__ = ["DataSource"]
