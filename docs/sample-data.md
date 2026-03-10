# Sample Data Generator

Generate realistic synthetic e-commerce and marketing data for demo, testing, and development without connecting a real data source adapter.

## Quick Start

```bash
# Install dependency
pip install faker

# Generate small dataset (default)
python scripts/generate_sample_data.py --size s --output data/sample

# List available sizes
python scripts/generate_sample_data.py --list-sizes
```

## Data Model

9 interrelated tables covering the full e-commerce funnel:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  campaigns   │     │  customers   │     │   products   │
│  CMP-0001    │     │  C-000001    │     │   P-00001    │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       ▼                    ▼                    │
┌──────────────┐     ┌──────────────┐            │
│ web_sessions │     │  web_leads   │            │
│  S-00000001  │     │  L-000001    │            │
└──────────────┘     └──────────────┘            │
                            │                    │
                            ▼                    ▼
                     ┌──────────────┐     ┌──────────────┐
                     │   orders     │────▶│ order_items  │
                     │ ORD-0000001  │     │ OI-00000001  │
                     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────┴───────┐
                     ▼              ▼
              ┌──────────────┐ ┌──────────────┐
              │  payments    │ │   support    │
              │ PAY-0000001  │ │   tickets    │
              └──────────────┘ │ TKT-000001   │
                               └──────────────┘
```

### Table Reference

| Table | Key Columns | Foreign Keys |
|-------|-------------|--------------|
| **customers** | customer_id, email, segment, country, registration_date | - |
| **products** | product_id, name, category, price, cost | - |
| **campaigns** | campaign_id, channel, budget, spend, impressions, clicks | - |
| **web_sessions** | session_id, started_at, duration_seconds, device_type | customer_id?, campaign_id? |
| **web_leads** | lead_id, source, status, converted_at | customer_id?, campaign_id? |
| **orders** | order_id, status, total_amount, channel | customer_id |
| **order_items** | order_item_id, quantity, unit_price, line_total | order_id, product_id |
| **payments** | payment_id, amount, method, status | order_id, customer_id |
| **support_tickets** | ticket_id, category, priority, satisfaction_score | customer_id, order_id? |

`?` = nullable (not every record has a value)

### Customer Segments

- **b2c** (60%): Individual consumers, smaller order values
- **b2b_small** (25%): Small business buyers, moderate volumes
- **b2b_enterprise** (15%): Large buyers, high quantities, invoice payments

### Product Categories

Electronics, Clothing, Home & Garden, Sports & Outdoors, Books & Media, Beauty & Health

Each category has distinct price ranges and cost margins for realistic profitability analysis.

## Size Presets

| Size | Customers | Products | Sessions | Orders | Tickets | ~CSV | ~Time |
|------|-----------|----------|----------|--------|---------|------|-------|
| **xs** | 50 | 30 | 500 | 100 | 30 | 1 MB | <1s |
| **s** | 500 | 100 | 10K | 2K | 500 | 15 MB | <1s |
| **m** | 5,000 | 300 | 100K | 20K | 5K | 150 MB | ~7s |
| **l** | 50,000 | 1,000 | 1M | 200K | 50K | 1.5 GB | ~3min |

- **xs** - local development, quick iteration
- **s** - unit/integration testing, CI
- **m** - realistic demo, performance testing
- **l** - stress testing, production-like volumes

## CLI Options

```
python scripts/generate_sample_data.py [OPTIONS]

  --size {xs,s,m,l}   Data size preset (default: s)
  --output PATH        Output directory (default: data/sample)
  --seed INT           Random seed for reproducibility (default: 42)
  --list-sizes         Show presets and exit
```

## Convert to Parquet

After generating CSVs, convert to Parquet for analytical use:

```bash
python -c "
import pandas as pd
from pathlib import Path

csv_dir = Path('data/sample')
parquet_dir = Path('data/sample/parquet')
parquet_dir.mkdir(exist_ok=True)

for f in sorted(csv_dir.glob('*.csv')):
    df = pd.read_csv(f)
    out = parquet_dir / f'{f.stem}.parquet'
    df.to_parquet(out, index=False)
    print(f'  {f.stem}: {len(df):,} rows -> {out}')
"
```

## Load into DuckDB

```bash
python -c "
import duckdb
from pathlib import Path

db = duckdb.connect('data/sample/analytics.duckdb')
parquet_dir = Path('data/sample/parquet')

for f in sorted(parquet_dir.glob('*.parquet')):
    table = f.stem
    db.execute(f'CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(\"{f}\")')
    count = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    print(f'  {table}: {count:,} rows')

db.close()
print('Database: data/sample/analytics.duckdb')
"
```

## Built-in Analytical Patterns

The generator creates data with discoverable patterns for realistic analysis:

- **Seasonality**: Q4 traffic and orders ~2x higher than Q1
- **Growth trend**: 50% increase in activity over the time period
- **Channel effectiveness**: paid_search has highest click-through rates
- **Customer lifetime**: Pareto distribution (20% of customers generate 80% of orders)
- **Segment differences**: B2B enterprise has 3-5x higher order values
- **Product mix**: Electronics = high revenue / lower margin, Books = low revenue / high margin
- **Support correlation**: 60% of tickets linked to specific orders

## Reproducibility

Same `--seed` always produces identical output. The default seed is 42.

```bash
# These two commands produce the same files
python scripts/generate_sample_data.py --size s --seed 42 --output run1
python scripts/generate_sample_data.py --size s --seed 42 --output run2
diff -r run1 run2  # no differences
```

## Server Deployment

To use sample data on a deployed server (instead of connecting a data adapter):

```bash
# On the server
cd /opt/data-analyst/repo

# Generate Parquet files directly using project's ParquetManager
# (snappy compression, proper column types, metadata embedding)
/opt/data-analyst/.venv/bin/python scripts/generate_sample_data.py \
    --size m --format parquet --output /data/src_data/parquet --seed 42

# Set correct permissions
chown -R root:data-ops /data/src_data/parquet
chmod -R 2775 /data/src_data/parquet
```
