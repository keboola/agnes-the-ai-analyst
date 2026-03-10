"""Tests for the sample data generator."""

import csv
import json
import pytest
from pathlib import Path

from scripts.generate_sample_data import SampleDataGenerator, SIZE_CONFIGS


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Temporary output directory for generated CSV files."""
    return tmp_path / "sample_data"


class TestSizeConfigs:
    """Verify size configuration integrity."""

    def test_all_sizes_have_required_keys(self):
        required = {
            "customers", "products", "campaigns", "web_sessions",
            "web_leads", "orders", "support_tickets", "months",
        }
        for size, cfg in SIZE_CONFIGS.items():
            missing = required - set(cfg.keys())
            assert not missing, f"Size '{size}' missing keys: {missing}"

    def test_sizes_scale_monotonically(self):
        """Each size should be strictly larger than the previous one."""
        sizes = list(SIZE_CONFIGS.keys())
        for key in ["customers", "products", "orders", "web_sessions"]:
            values = [SIZE_CONFIGS[s][key] for s in sizes]
            assert values == sorted(values), (
                f"{key} does not scale monotonically across sizes"
            )


class TestXSGeneration:
    """Full generation test with xs size (fast)."""

    @pytest.fixture(autouse=True)
    def generate(self, output_dir: Path):
        self.output_dir = output_dir
        gen = SampleDataGenerator(size="xs", seed=42, output_dir=output_dir)
        self.manifest = gen.run()

    def test_all_csv_files_created(self):
        expected = {
            "customers", "products", "campaigns", "web_sessions",
            "web_leads", "orders", "order_items", "payments",
            "support_tickets",
        }
        csv_files = {p.stem for p in self.output_dir.glob("*.csv")}
        assert expected == csv_files

    def test_manifest_created(self):
        manifest_path = self.output_dir / "_manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["size"] == "xs"
        assert "tables" in data
        assert data["total_rows"] > 0

    def test_row_counts_match_config(self):
        """Row counts for directly specified tables should match config."""
        cfg = SIZE_CONFIGS["xs"]
        for table in ["customers", "products", "campaigns", "web_sessions",
                       "web_leads", "orders", "support_tickets"]:
            assert self.manifest["tables"][table] == cfg[table], (
                f"{table}: expected {cfg[table]}, got {self.manifest['tables'][table]}"
            )

    def test_order_items_derived(self):
        """Order items should be > orders (most orders have multiple items)."""
        assert self.manifest["tables"]["order_items"] > self.manifest["tables"]["orders"]

    def test_payments_at_least_one_per_order(self):
        """Payments should be >= orders (some have failed retries)."""
        assert self.manifest["tables"]["payments"] >= self.manifest["tables"]["orders"]

    def test_csv_headers_not_empty(self):
        """Every CSV should have a header and at least one data row."""
        for csv_path in self.output_dir.glob("*.csv"):
            with open(csv_path) as f:
                reader = csv.reader(f)
                header = next(reader)
                assert len(header) > 0, f"{csv_path.name}: empty header"
                first_row = next(reader, None)
                assert first_row is not None, f"{csv_path.name}: no data rows"


class TestReferentialIntegrity:
    """Verify foreign key relationships across tables."""

    @pytest.fixture(autouse=True)
    def generate(self, output_dir: Path):
        self.output_dir = output_dir
        gen = SampleDataGenerator(size="xs", seed=123, output_dir=output_dir)
        gen.run()
        self.tables = {}
        for csv_path in output_dir.glob("*.csv"):
            with open(csv_path) as f:
                self.tables[csv_path.stem] = list(csv.DictReader(f))

    def _get_ids(self, table: str, column: str) -> set[str]:
        return {row[column] for row in self.tables[table]}

    def _get_fk_values(self, table: str, column: str) -> set[str]:
        return {row[column] for row in self.tables[table] if row[column]}

    def test_orders_reference_valid_customers(self):
        customer_ids = self._get_ids("customers", "customer_id")
        order_customer_ids = self._get_fk_values("orders", "customer_id")
        orphans = order_customer_ids - customer_ids
        assert not orphans, f"Orders reference non-existent customers: {orphans}"

    def test_order_items_reference_valid_orders(self):
        order_ids = self._get_ids("orders", "order_id")
        item_order_ids = self._get_fk_values("order_items", "order_id")
        orphans = item_order_ids - order_ids
        assert not orphans, f"Order items reference non-existent orders: {orphans}"

    def test_order_items_reference_valid_products(self):
        product_ids = self._get_ids("products", "product_id")
        item_product_ids = self._get_fk_values("order_items", "product_id")
        orphans = item_product_ids - product_ids
        assert not orphans, f"Order items reference non-existent products: {orphans}"

    def test_payments_reference_valid_orders(self):
        order_ids = self._get_ids("orders", "order_id")
        payment_order_ids = self._get_fk_values("payments", "order_id")
        orphans = payment_order_ids - order_ids
        assert not orphans, f"Payments reference non-existent orders: {orphans}"

    def test_support_tickets_reference_valid_customers(self):
        customer_ids = self._get_ids("customers", "customer_id")
        ticket_customer_ids = self._get_fk_values("support_tickets", "customer_id")
        orphans = ticket_customer_ids - customer_ids
        assert not orphans, f"Tickets reference non-existent customers: {orphans}"


class TestDeterminism:
    """Verify reproducibility with same seed."""

    def test_same_seed_produces_same_output(self, tmp_path: Path):
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        gen1 = SampleDataGenerator(size="xs", seed=99, output_dir=dir1)
        gen1.run()

        gen2 = SampleDataGenerator(size="xs", seed=99, output_dir=dir2)
        gen2.run()

        for csv_path in dir1.glob("*.csv"):
            content1 = csv_path.read_text()
            content2 = (dir2 / csv_path.name).read_text()
            assert content1 == content2, f"{csv_path.name} differs between runs"

    def test_different_seed_produces_different_output(self, tmp_path: Path):
        dir1 = tmp_path / "seed1"
        dir2 = tmp_path / "seed2"

        gen1 = SampleDataGenerator(size="xs", seed=1, output_dir=dir1)
        gen1.run()

        gen2 = SampleDataGenerator(size="xs", seed=2, output_dir=dir2)
        gen2.run()

        content1 = (dir1 / "customers.csv").read_text()
        content2 = (dir2 / "customers.csv").read_text()
        assert content1 != content2
