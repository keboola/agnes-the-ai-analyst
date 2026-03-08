# Data Sources

## Overview

AI Data Analyst uses a pluggable adapter system for data sources. Configure the adapter type in `config/instance.yaml`:

```yaml
data_source:
  type: "keboola"  # Options: keboola, csv, bigquery (future)
```

## Keboola Adapter

Syncs tables from Keboola Storage API.

### Requirements

- `kbcstorage` Python package (included in requirements.txt)
- Keboola Storage API token with read access

### Configuration

In `.env`:
```
KEBOOLA_STORAGE_TOKEN=your-token-here
KEBOOLA_STACK_URL=https://connection.your-region.keboola.com
KEBOOLA_PROJECT_ID=12345
DATA_SOURCE=keboola
```

### Sync Strategies

Define in `docs/data_description.md`:

- **full_refresh**: Downloads entire table each sync
- **incremental**: Downloads only changed rows (using changedSince)
- **partitioned**: Splits data into time-based partitions (month/day/year)

### Data Description Format

```yaml
folder_mapping:
  "in.c-crm": "sales"
  "in.c-hr": "hr"

tables:
  - id: "in.c-crm.company"
    name: "company"
    description: "Company master data from CRM"
    primary_key: "id"
    sync_strategy: "full_refresh"
```

## Writing a Custom Adapter

Create a new file in `src/adapters/`:

```python
from ..data_sync import DataSource

class MyDataSource(DataSource):
    def sync_table(self, table_config, sync_state):
        # Download data, convert to Parquet
        # Return {"success": True, "rows": N, "strategy": "..."}
        pass
```

Register in `src/adapters/__init__.py`:
```python
if adapter_type == "my_source":
    from .my_adapter import MyDataSource
    return MyDataSource(**kwargs)
```
