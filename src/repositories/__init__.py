"""Repository layer for DuckDB state management."""
from src.db import get_system_db, get_analytics_db

__all__ = ["get_system_db", "get_analytics_db"]
