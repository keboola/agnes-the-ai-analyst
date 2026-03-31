#!/usr/bin/env python3
"""
Sample data generator for AI Data Analyst demo and testing.

Generates realistic synthetic e-commerce + marketing data as CSV or Parquet.
Tables: customers, products, campaigns, web_sessions, web_leads,
        orders, order_items, payments, support_tickets

Usage:
    python scripts/generate_sample_data.py --size s --output data/sample
    python scripts/generate_sample_data.py --size m --format parquet --output /data/src_data/parquet
    python scripts/generate_sample_data.py --list-sizes
"""

import argparse
import csv
import json
import logging
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Generator

try:
    from faker import Faker
except ImportError:
    print("ERROR: faker is required. Install with: pip install faker")
    sys.exit(1)

logger = logging.getLogger(__name__)

# ── Size configurations ────────────────────────────────────────────────

SIZE_CONFIGS = {
    "xs": {
        "label": "Extra Small (demo/dev)",
        "customers": 50,
        "products": 30,
        "campaigns": 10,
        "web_sessions": 500,
        "web_leads": 50,
        "orders": 100,
        "support_tickets": 30,
        "months": 3,
        "estimated_csv_mb": 1,
    },
    "s": {
        "label": "Small (testing)",
        "customers": 500,
        "products": 100,
        "campaigns": 30,
        "web_sessions": 10_000,
        "web_leads": 1_000,
        "orders": 2_000,
        "support_tickets": 500,
        "months": 12,
        "estimated_csv_mb": 15,
    },
    "m": {
        "label": "Medium (realistic)",
        "customers": 5_000,
        "products": 300,
        "campaigns": 80,
        "web_sessions": 100_000,
        "web_leads": 10_000,
        "orders": 20_000,
        "support_tickets": 5_000,
        "months": 24,
        "estimated_csv_mb": 150,
    },
    "l": {
        "label": "Large (stress test)",
        "customers": 50_000,
        "products": 1_000,
        "campaigns": 200,
        "web_sessions": 1_000_000,
        "web_leads": 100_000,
        "orders": 200_000,
        "support_tickets": 50_000,
        "months": 36,
        "estimated_csv_mb": 1500,
    },
}

# ── Domain data ────────────────────────────────────────────────────────

# Monthly seasonality multipliers (index 0 = January)
MONTHLY_SEASONALITY = [0.70, 0.75, 0.85, 0.90, 0.95, 1.00,
                       0.90, 0.85, 1.00, 1.10, 1.30, 1.50]

# Day-of-week multipliers (Monday=0 .. Sunday=6)
DOW_MULTIPLIER = [1.0, 1.0, 1.0, 1.05, 1.15, 0.80, 0.60]

# Hour-of-day weights (24 values, peak at 10-14)
HOUR_WEIGHTS = [2, 1, 1, 1, 1, 2, 4, 8, 14, 18, 20, 19,
                18, 17, 16, 15, 14, 12, 10, 8, 6, 5, 4, 3]

CUSTOMER_SEGMENTS = [
    ("b2c", 0.60),
    ("b2b_small", 0.25),
    ("b2b_enterprise", 0.15),
]

COUNTRIES = [
    ("Czech Republic", "CZ", 0.25), ("Germany", "DE", 0.15),
    ("United States", "US", 0.12),  ("United Kingdom", "GB", 0.10),
    ("France", "FR", 0.08),         ("Austria", "AT", 0.05),
    ("Poland", "PL", 0.05),         ("Netherlands", "NL", 0.05),
    ("Slovakia", "SK", 0.05),       ("Spain", "ES", 0.04),
    ("Italy", "IT", 0.03),          ("Sweden", "SE", 0.03),
]

EMAIL_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "protonmail.com", "icloud.com", "mail.com",
]

PRODUCT_CATEGORIES = {
    "Electronics": {
        "items": [
            "Wireless Headphones", "USB-C Charger 65W", "Smart Watch",
            "Webcam 4K", "Bluetooth Speaker", "Noise-Cancelling Earbuds",
            "Mechanical Keyboard", "27in Monitor QHD", "Laptop Stand",
            "Power Bank 20000mAh", "Smart Home Hub", "LED Desk Lamp",
            "External SSD 1TB", "Wireless Charging Pad", "Action Camera",
        ],
        "price_range": (19.99, 1299.99),
        "cost_ratio": (0.40, 0.65),
    },
    "Clothing": {
        "items": [
            "Oxford Shirt Classic", "Slim Chino Pants", "Merino Sweater",
            "Leather Belt Premium", "Running Sneakers", "Denim Jacket",
            "Polo Shirt Casual", "Winter Down Jacket", "Cotton T-Shirt",
            "Formal Dress Shoes", "Yoga Leggings", "Crossbody Bag",
            "Wool Blend Coat", "Sport Shorts Quick-Dry", "Canvas Tote Bag",
        ],
        "price_range": (9.99, 299.99),
        "cost_ratio": (0.30, 0.55),
    },
    "Home & Garden": {
        "items": [
            "Ceramic Mug Set", "Bamboo Cutting Board", "Steel Water Bottle",
            "Indoor Plant Pot Set", "LED String Lights 10m", "Bath Towel Set",
            "Memory Foam Pillow", "Scented Candle Set", "Kitchen Knife Set 5pc",
            "Garden Tool Set", "Bedside Lamp", "Throw Blanket Fleece",
            "Wall Clock Minimal", "Spice Rack Organizer", "Herb Garden Kit",
        ],
        "price_range": (7.99, 199.99),
        "cost_ratio": (0.35, 0.55),
    },
    "Sports & Outdoors": {
        "items": [
            "Yoga Mat Premium", "Resistance Bands Set", "Insulated Bottle",
            "Hiking Backpack 40L", "Speed Jump Rope", "Foam Roller 45cm",
            "Camping Hammock", "Cycling Gloves", "Tennis Balls 4-Pack",
            "Swim Goggles Anti-Fog", "Adjustable Dumbbells", "Running Armband",
            "Compact Sleeping Bag", "Compression Socks", "Fishing Tackle Box",
        ],
        "price_range": (8.99, 249.99),
        "cost_ratio": (0.35, 0.60),
    },
    "Books & Media": {
        "items": [
            "Data Science Handbook", "Leadership in Practice", "Creative Writing",
            "Python Programming", "World History Atlas", "Cooking Masterclass",
            "Mindfulness Journal", "Photography Basics", "Financial Planning",
            "Sci-Fi Novel Collection", "Art Supplies Set", "Board Game Classic",
            "Puzzle 1000 Pieces", "Drawing Pencil Set 24pc", "Travel Guide Europe",
        ],
        "price_range": (5.99, 79.99),
        "cost_ratio": (0.25, 0.45),
    },
    "Beauty & Health": {
        "items": [
            "Moisturizer SPF30", "Organic Shampoo 500ml", "Electric Toothbrush",
            "Vitamin D3 Supplements", "Essential Oil Set 6pk", "Hair Dryer Pro",
            "Sunscreen SPF50", "Protein Powder Vanilla", "Face Mask Pack 10",
            "Hand Cream Repair", "Body Lotion Hydrating", "Beard Grooming Set",
            "Collagen Drink Mix", "Makeup Brush Set 12pc", "Bath Bomb Gift Set",
        ],
        "price_range": (4.99, 149.99),
        "cost_ratio": (0.20, 0.45),
    },
}

PRODUCT_VARIANTS = ["Pro", "Ultra", "Lite", "Plus", "Mini", "Max"]
PRODUCT_COLORS = ["Black", "White", "Blue", "Red", "Green", "Grey"]

CAMPAIGN_CHANNELS = [
    ("email", 0.20),
    ("paid_search", 0.22),
    ("paid_social", 0.18),
    ("organic_social", 0.12),
    ("display", 0.12),
    ("affiliate", 0.08),
    ("retargeting", 0.08),
]

CAMPAIGN_TEMPLATES = [
    "Spring Sale", "Summer Clearance", "Back to School", "Black Friday",
    "Holiday Season", "New Year Push", "Flash Sale", "Product Launch",
    "Loyalty Rewards", "Newsletter Blast", "Retargeting Wave",
    "Brand Awareness", "Category Spotlight", "Win-Back", "Early Access",
]

LEAD_SOURCES = [
    ("newsletter_signup", 0.30),
    ("contact_form", 0.25),
    ("demo_request", 0.15),
    ("content_download", 0.20),
    ("webinar_registration", 0.10),
]

DEVICES = [("desktop", 0.45), ("mobile", 0.45), ("tablet", 0.10)]
BROWSERS = [("Chrome", 0.64), ("Safari", 0.19), ("Firefox", 0.08),
            ("Edge", 0.07), ("Other", 0.02)]

LANDING_PAGES = [
    "/", "/products", "/products/electronics", "/products/clothing",
    "/products/home-garden", "/sale", "/new-arrivals", "/about",
    "/blog", "/blog/tips", "/blog/reviews", "/contact",
]

ORDER_STATUSES = [
    ("delivered", 0.58), ("shipped", 0.15), ("confirmed", 0.10),
    ("pending", 0.04), ("cancelled", 0.08), ("returned", 0.05),
]

ORDER_CHANNELS = [
    ("web", 0.55), ("mobile_app", 0.35), ("phone", 0.05), ("api", 0.05),
]

PAYMENT_METHODS = [
    ("credit_card", 0.38), ("debit_card", 0.20), ("paypal", 0.18),
    ("bank_transfer", 0.12), ("apple_pay", 0.08), ("invoice", 0.04),
]

TICKET_CATEGORIES = [
    ("question", 0.28), ("complaint", 0.18), ("return_request", 0.14),
    ("shipping", 0.16), ("technical_issue", 0.12), ("refund", 0.12),
]

TICKET_PRIORITIES = [
    ("low", 0.38), ("medium", 0.35), ("high", 0.20), ("critical", 0.07),
]

TICKET_SUBJECTS = {
    "question": [
        "Delivery time estimate", "Product compatibility", "Return policy",
        "Bulk order pricing", "Warranty coverage", "Size guide help",
    ],
    "complaint": [
        "Item arrived damaged", "Wrong product received", "Poor quality",
        "Missing items in order", "Packaging insufficient", "Late delivery",
    ],
    "return_request": [
        "Does not match description", "Changed my mind", "Duplicate order",
        "Size does not fit", "Defective product", "Better price elsewhere",
    ],
    "shipping": [
        "Package not delivered", "Tracking not updating", "Wrong address",
        "Expedited shipping request", "International shipping", "Lost package",
    ],
    "technical_issue": [
        "Cannot complete checkout", "Payment error", "Login problem",
        "Page not loading", "Mobile app crash", "Coupon not working",
    ],
    "refund": [
        "Cancelled order refund", "Partial refund request", "Overcharged",
        "Refund not received", "Billing discrepancy", "Double charged",
    ],
}

TICKET_CHANNELS = [
    ("email", 0.40), ("chat", 0.30), ("phone", 0.15), ("web_form", 0.15),
]

# ── Parquet schema definitions (used by ParquetManager) ────────────────

TABLE_SCHEMAS = {
    "customers": {
        "dtypes": {"is_active": "Int64"},
        "date_columns": ["registration_date"],
    },
    "products": {
        "dtypes": {
            "price": "float64", "cost": "float64",
            "weight_kg": "float64", "is_active": "Int64",
        },
        "date_columns": ["created_at"],
    },
    "campaigns": {
        "dtypes": {
            "budget": "float64", "spend": "float64",
            "impressions": "Int64", "clicks": "Int64",
        },
        "date_columns": ["start_date", "end_date"],
    },
    "web_sessions": {
        "dtypes": {
            "duration_seconds": "Int64", "pages_viewed": "Int64",
            "is_bounce": "Int64",
        },
        "parse_dates": ["started_at"],
    },
    "web_leads": {
        "parse_dates": ["created_at", "converted_at"],
    },
    "orders": {
        "dtypes": {
            "items_total": "float64", "discount_amount": "float64",
            "shipping_amount": "float64", "total_amount": "float64",
        },
        "parse_dates": ["created_at"],
    },
    "order_items": {
        "dtypes": {
            "quantity": "Int64", "unit_price": "float64",
            "discount_percent": "Int64", "line_total": "float64",
        },
    },
    "payments": {
        "dtypes": {"amount": "float64"},
        "parse_dates": ["created_at", "completed_at"],
    },
    "support_tickets": {
        "dtypes": {"satisfaction_score": "Int64"},
        "parse_dates": ["created_at", "first_response_at", "resolved_at"],
    },
}


# ── Generator ──────────────────────────────────────────────────────────

class SampleDataGenerator:
    """Generates realistic synthetic e-commerce data as CSV or Parquet."""

    def __init__(self, size: str, seed: int, output_dir: Path,
                 output_format: str = "csv"):
        self.cfg = SIZE_CONFIGS[size]
        self.size_name = size
        self.rng = random.Random(seed)
        self.fake = Faker(["en_US", "de_DE", "cs_CZ", "fr_FR"])
        Faker.seed(seed)
        self.output_dir = output_dir
        self.output_format = output_format  # "csv", "parquet", or "both"
        self.row_counts: dict[str, int] = {}

        # Time range
        months = self.cfg["months"]
        self.end_date = date(2026, 3, 1)
        self.start_date = self.end_date - timedelta(days=months * 30)
        self.total_days = (self.end_date - self.start_date).days

        # Pre-compute day weights for temporal distribution
        self._days: list[date] = []
        self._day_weights: list[float] = []
        for i in range(self.total_days):
            d = self.start_date + timedelta(days=i)
            growth = 1.0 + 0.5 * (i / max(self.total_days, 1))
            season = MONTHLY_SEASONALITY[d.month - 1]
            dow = DOW_MULTIPLIER[d.weekday()]
            self._days.append(d)
            self._day_weights.append(growth * season * dow)

        # Reference data (populated during generation)
        self._customer_ids: list[str] = []
        self._customer_reg_dates: dict[str, date] = {}
        self._customer_segments: dict[str, str] = {}
        self._product_ids: list[str] = []
        self._product_prices: dict[str, float] = {}
        self._product_categories: dict[str, str] = {}
        self._campaign_ids: list[str] = []
        self._campaign_ranges: dict[str, tuple[date, date]] = {}
        self._order_ids: list[str] = []
        self._order_customers: dict[str, str] = {}
        self._order_dates: dict[str, date] = {}
        self._order_statuses: dict[str, str] = {}
        self._order_totals: dict[str, float] = {}

    # ── Helpers ─────────────────────────────────────────────────

    def _weighted_choice(self, options: list[tuple[str, float]]) -> str:
        """Pick from [(value, weight), ...] using instance RNG."""
        values, weights = zip(*options)
        return self.rng.choices(values, weights=weights, k=1)[0]

    def _random_date(self) -> date:
        """Random date weighted by growth + seasonality + day-of-week."""
        return self.rng.choices(self._days, weights=self._day_weights, k=1)[0]

    def _random_datetime(self, d: date | None = None) -> str:
        """Random datetime string. If d is None, pick a weighted random date."""
        if d is None:
            d = self._random_date()
        hour = self.rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
        minute = self.rng.randint(0, 59)
        second = self.rng.randint(0, 59)
        return f"{d} {hour:02d}:{minute:02d}:{second:02d}"

    def _random_date_after(self, start: date, max_days: int = 30) -> date:
        """Random date between start and start + max_days (capped at end_date)."""
        end = min(start + timedelta(days=max_days), self.end_date)
        delta = (end - start).days
        if delta <= 0:
            return start
        return start + timedelta(days=self.rng.randint(0, delta))

    def _write_table(self, name: str, fields: list[str],
                     rows: list[dict] | Generator) -> int:
        """Write CSV table from list or generator of dicts."""
        path = self.output_dir / f"{name}.csv"
        count = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
                if count % 250_000 == 0:
                    logger.info(f"  ... {count:,} rows written")
        self.row_counts[name] = count
        return count

    # ── Table generators ───────────────────────────────────────

    def _generate_customers(self) -> None:
        n = self.cfg["customers"]
        logger.info(f"  Generating {n:,} customers...")

        country_vals = [(c[0], c[2]) for c in COUNTRIES]
        rows = []
        for i in range(n):
            cid = f"C-{i + 1:06d}"
            segment = self._weighted_choice(CUSTOMER_SEGMENTS)
            reg_date = self._random_date()
            first = self.fake.first_name()
            last = self.fake.last_name()
            country = self._weighted_choice(country_vals)

            if segment.startswith("b2b"):
                company = self.fake.company()
                domain = company.lower().split()[0].replace(",", "") + ".com"
                email = f"{first.lower()}.{last.lower()}@{domain}"
            else:
                company = ""
                domain = self.rng.choice(EMAIL_DOMAINS)
                email = f"{first.lower()}.{last.lower()}@{domain}"

            rows.append({
                "customer_id": cid,
                "email": email,
                "first_name": first,
                "last_name": last,
                "company": company,
                "country": country,
                "city": self.fake.city(),
                "segment": segment,
                "registration_date": str(reg_date),
                "is_active": self.rng.choices([1, 0], weights=[0.85, 0.15])[0],
            })
            self._customer_ids.append(cid)
            self._customer_reg_dates[cid] = reg_date
            self._customer_segments[cid] = segment

        self._write_table("customers", list(rows[0].keys()), rows)

    def _generate_products(self) -> None:
        n = self.cfg["products"]
        logger.info(f"  Generating {n:,} products...")

        # Build product pool: base items + variants for larger sizes
        pool: list[tuple[str, str, str]] = []  # (name, category, subcategory)
        categories = list(PRODUCT_CATEGORIES.keys())
        for cat in categories:
            for item in PRODUCT_CATEGORIES[cat]["items"]:
                pool.append((item, cat, cat))

        # Add variants if we need more than base pool
        while len(pool) < n:
            cat = self.rng.choice(categories)
            item = self.rng.choice(PRODUCT_CATEGORIES[cat]["items"])
            variant = self.rng.choice(PRODUCT_VARIANTS)
            color = self.rng.choice(PRODUCT_COLORS)
            name = f"{item} {variant} - {color}"
            pool.append((name, cat, cat))

        self.rng.shuffle(pool)
        pool = pool[:n]

        rows = []
        for i, (name, category, _subcat) in enumerate(pool):
            pid = f"P-{i + 1:05d}"
            cat_cfg = PRODUCT_CATEGORIES[category]
            price = round(self.rng.uniform(*cat_cfg["price_range"]), 2)
            cost_ratio = self.rng.uniform(*cat_cfg["cost_ratio"])
            cost = round(price * cost_ratio, 2)

            rows.append({
                "product_id": pid,
                "sku": f"SKU-{self.rng.randint(10000, 99999)}",
                "name": name,
                "category": category,
                "price": price,
                "cost": cost,
                "weight_kg": round(self.rng.uniform(0.1, 15.0), 2),
                "is_active": self.rng.choices([1, 0], weights=[0.90, 0.10])[0],
                "created_at": str(self._random_date()),
            })
            self._product_ids.append(pid)
            self._product_prices[pid] = price
            self._product_categories[pid] = category

        self._write_table("products", list(rows[0].keys()), rows)

    def _generate_campaigns(self) -> None:
        n = self.cfg["campaigns"]
        logger.info(f"  Generating {n:,} campaigns...")

        rows = []
        for i in range(n):
            cid = f"CMP-{i + 1:04d}"
            channel = self._weighted_choice(CAMPAIGN_CHANNELS)
            start = self._random_date()
            duration = self.rng.randint(7, 60)
            end = min(start + timedelta(days=duration), self.end_date)
            is_past = end < self.end_date - timedelta(days=7)

            budget = round(self.rng.uniform(500, 25000), 2)
            spend_ratio = self.rng.uniform(0.6, 1.1) if is_past else self.rng.uniform(0.2, 0.7)
            spend = round(budget * min(spend_ratio, 1.0), 2)
            impressions = int(spend * self.rng.uniform(80, 500))
            ctr = self.rng.uniform(0.005, 0.08)
            clicks = int(impressions * ctr)

            template = self.rng.choice(CAMPAIGN_TEMPLATES)
            name = f"{template} - {channel.replace('_', ' ').title()} {start.year}"

            status = "completed" if is_past else self.rng.choice(["active", "paused"])

            rows.append({
                "campaign_id": cid,
                "name": name,
                "channel": channel,
                "status": status,
                "budget": budget,
                "spend": spend,
                "impressions": impressions,
                "clicks": clicks,
                "start_date": str(start),
                "end_date": str(end),
                "target_segment": self._weighted_choice(CUSTOMER_SEGMENTS),
            })
            self._campaign_ids.append(cid)
            self._campaign_ranges[cid] = (start, end)

        self._write_table("campaigns", list(rows[0].keys()), rows)

    def _generate_web_sessions(self) -> None:
        n = self.cfg["web_sessions"]
        logger.info(f"  Generating {n:,} web sessions...")

        fields = [
            "session_id", "visitor_id", "customer_id", "campaign_id",
            "started_at", "duration_seconds", "pages_viewed",
            "device_type", "browser", "country", "landing_page", "is_bounce",
        ]
        country_vals = [(c[0], c[2]) for c in COUNTRIES]

        def gen_rows() -> Generator[dict[str, Any], None, None]:
            for i in range(n):
                sid = f"S-{i + 1:08d}"
                d = self._random_date()

                # 40% sessions from logged-in customers
                customer_id = ""
                if self.rng.random() < 0.40 and self._customer_ids:
                    customer_id = self.rng.choice(self._customer_ids)

                # 25% sessions attributed to a campaign
                campaign_id = ""
                if self.rng.random() < 0.25 and self._campaign_ids:
                    # Pick a campaign that was active on this date
                    candidates = [
                        c for c in self._campaign_ids
                        if self._campaign_ranges[c][0] <= d <= self._campaign_ranges[c][1]
                    ]
                    if candidates:
                        campaign_id = self.rng.choice(candidates)

                is_bounce = self.rng.random() < 0.35
                if is_bounce:
                    duration = self.rng.randint(5, 30)
                    pages = 1
                else:
                    duration = self.rng.randint(30, 900)
                    pages = self.rng.randint(2, 15)

                yield {
                    "session_id": sid,
                    "visitor_id": f"V-{self.rng.randint(1, n // 3):08d}",
                    "customer_id": customer_id,
                    "campaign_id": campaign_id,
                    "started_at": self._random_datetime(d),
                    "duration_seconds": duration,
                    "pages_viewed": pages,
                    "device_type": self._weighted_choice(DEVICES),
                    "browser": self._weighted_choice(BROWSERS),
                    "country": self._weighted_choice(country_vals),
                    "landing_page": self.rng.choice(LANDING_PAGES),
                    "is_bounce": int(is_bounce),
                }

        self._write_table("web_sessions", fields, gen_rows())

    def _generate_web_leads(self) -> None:
        n = self.cfg["web_leads"]
        logger.info(f"  Generating {n:,} web leads...")

        fields = [
            "lead_id", "customer_id", "email", "source", "campaign_id",
            "created_at", "status", "converted_at",
        ]
        lead_statuses = [
            ("new", 0.35), ("contacted", 0.20), ("qualified", 0.15),
            ("converted", 0.18), ("lost", 0.12),
        ]

        rows = []
        for i in range(n):
            lid = f"L-{i + 1:06d}"
            d = self._random_date()
            status = self._weighted_choice(lead_statuses)

            # 55% from existing customers
            customer_id = ""
            email = self.fake.email()
            if self.rng.random() < 0.55 and self._customer_ids:
                customer_id = self.rng.choice(self._customer_ids)

            campaign_id = ""
            if self.rng.random() < 0.40 and self._campaign_ids:
                campaign_id = self.rng.choice(self._campaign_ids)

            converted_at = ""
            if status == "converted":
                converted_at = self._random_datetime(
                    self._random_date_after(d, max_days=14)
                )

            rows.append({
                "lead_id": lid,
                "customer_id": customer_id,
                "email": email,
                "source": self._weighted_choice(LEAD_SOURCES),
                "campaign_id": campaign_id,
                "created_at": self._random_datetime(d),
                "status": status,
                "converted_at": converted_at,
            })

        self._write_table("web_leads", fields, rows)

    def _generate_orders_and_items(self) -> None:
        n_orders = self.cfg["orders"]
        logger.info(f"  Generating {n_orders:,} orders + order items...")

        # Customer activity weights (Pareto-like distribution)
        activity = [self.rng.paretovariate(1.2) for _ in self._customer_ids]

        order_fields = [
            "order_id", "customer_id", "created_at", "status",
            "items_total", "discount_amount", "shipping_amount",
            "total_amount", "channel",
        ]
        item_fields = [
            "order_item_id", "order_id", "product_id", "quantity",
            "unit_price", "discount_percent", "line_total",
        ]

        order_rows = []
        item_rows = []
        item_seq = 0

        for i in range(n_orders):
            oid = f"ORD-{i + 1:07d}"
            cust_id = self.rng.choices(self._customer_ids, weights=activity, k=1)[0]
            reg_date = self._customer_reg_dates[cust_id]
            segment = self._customer_segments[cust_id]

            # Order date: after customer registration
            order_date = self._random_date_after(reg_date,
                                                 max_days=(self.end_date - reg_date).days)
            status = self._weighted_choice(ORDER_STATUSES)

            # B2B orders tend to have more items
            max_items = 8 if segment.startswith("b2b") else 5
            item_weights = list(range(max_items, 0, -1))  # favor fewer items
            n_items = self.rng.choices(range(1, max_items + 1), weights=item_weights, k=1)[0]

            items_total = 0.0
            for _j in range(n_items):
                item_seq += 1
                pid = self.rng.choice(self._product_ids)
                qty = self.rng.choices([1, 2, 3, 4, 5],
                                       weights=[60, 20, 10, 5, 5], k=1)[0]
                if segment == "b2b_enterprise":
                    qty *= self.rng.randint(1, 5)
                unit_price = self._product_prices[pid]
                disc_pct = self.rng.choices(
                    [0, 5, 10, 15, 20],
                    weights=[50, 20, 15, 10, 5], k=1
                )[0]
                line_total = round(unit_price * qty * (1 - disc_pct / 100), 2)
                items_total += line_total

                item_rows.append({
                    "order_item_id": f"OI-{item_seq:08d}",
                    "order_id": oid,
                    "product_id": pid,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "discount_percent": disc_pct,
                    "line_total": line_total,
                })

            discount_amount = round(items_total * self.rng.uniform(0, 0.05), 2)
            shipping = round(self.rng.uniform(0, 15.99), 2) if items_total < 100 else 0.0
            total = round(items_total - discount_amount + shipping, 2)

            order_rows.append({
                "order_id": oid,
                "customer_id": cust_id,
                "created_at": self._random_datetime(order_date),
                "status": status,
                "items_total": round(items_total, 2),
                "discount_amount": discount_amount,
                "shipping_amount": shipping,
                "total_amount": total,
                "channel": self._weighted_choice(ORDER_CHANNELS),
            })
            self._order_ids.append(oid)
            self._order_customers[oid] = cust_id
            self._order_dates[oid] = order_date
            self._order_statuses[oid] = status
            self._order_totals[oid] = total

        self._write_table("orders", order_fields, order_rows)
        self._write_table("order_items", item_fields, item_rows)

    def _generate_payments(self) -> None:
        logger.info(f"  Generating payments for {len(self._order_ids):,} orders...")

        fields = [
            "payment_id", "order_id", "customer_id", "amount", "currency",
            "method", "status", "created_at", "completed_at",
        ]

        rows = []
        seq = 0
        for oid in self._order_ids:
            cust_id = self._order_customers[oid]
            segment = self._customer_segments[cust_id]
            order_date = self._order_dates[oid]
            order_status = self._order_statuses[oid]
            amount = self._order_totals[oid]

            # B2B more likely to use invoice/bank_transfer
            if segment.startswith("b2b") and self.rng.random() < 0.40:
                method = self.rng.choice(["bank_transfer", "invoice"])
            else:
                method = self._weighted_choice(PAYMENT_METHODS)

            # 5% chance of a failed payment attempt first
            if self.rng.random() < 0.05:
                seq += 1
                rows.append({
                    "payment_id": f"PAY-{seq:07d}",
                    "order_id": oid,
                    "customer_id": cust_id,
                    "amount": amount,
                    "currency": "EUR",
                    "method": method,
                    "status": "failed",
                    "created_at": self._random_datetime(order_date),
                    "completed_at": "",
                })

            seq += 1
            if order_status == "cancelled":
                pay_status = "cancelled"
                completed = ""
            elif order_status == "returned":
                pay_status = "refunded"
                completed = self._random_datetime(
                    self._random_date_after(order_date, max_days=14)
                )
            else:
                pay_status = "completed"
                completed = self._random_datetime(
                    self._random_date_after(order_date, max_days=3)
                )

            rows.append({
                "payment_id": f"PAY-{seq:07d}",
                "order_id": oid,
                "customer_id": cust_id,
                "amount": amount,
                "currency": "EUR",
                "method": method,
                "status": pay_status,
                "created_at": self._random_datetime(order_date),
                "completed_at": completed,
            })

        self._write_table("payments", fields, rows)

    def _generate_support_tickets(self) -> None:
        n = self.cfg["support_tickets"]
        logger.info(f"  Generating {n:,} support tickets...")

        fields = [
            "ticket_id", "customer_id", "order_id", "category", "priority",
            "status", "channel", "subject", "created_at", "first_response_at",
            "resolved_at", "satisfaction_score",
        ]

        rows = []
        for i in range(n):
            tid = f"TKT-{i + 1:06d}"
            cust_id = self.rng.choice(self._customer_ids)
            category = self._weighted_choice(TICKET_CATEGORIES)
            priority = self._weighted_choice(TICKET_PRIORITIES)
            subject = self.rng.choice(TICKET_SUBJECTS[category])
            d = self._random_date()

            # 60% linked to an order
            order_id = ""
            if self.rng.random() < 0.60 and self._order_ids:
                # Pick an order from this customer if possible
                cust_orders = [
                    o for o in self._order_ids
                    if self._order_customers[o] == cust_id
                ]
                if cust_orders:
                    order_id = self.rng.choice(cust_orders)
                else:
                    order_id = self.rng.choice(self._order_ids)

            # Status progression
            is_resolved = self.rng.random() < 0.75
            if is_resolved:
                status = self.rng.choice(["resolved", "closed"])
            else:
                status = self.rng.choice(["open", "in_progress", "waiting_customer"])

            # Response and resolution times based on priority
            response_hours = {
                "critical": (0.5, 4), "high": (1, 12),
                "medium": (4, 48), "low": (8, 96),
            }
            rh = response_hours[priority]
            first_response = ""
            resolved_at = ""
            satisfaction = ""

            if status not in ("open",):
                resp_delta = timedelta(hours=self.rng.uniform(*rh))
                first_response = self._random_datetime(
                    min(d + timedelta(days=int(resp_delta.total_seconds() // 86400)),
                        self.end_date)
                )

            if is_resolved:
                resolve_days = self.rng.randint(1, 14)
                resolved_at = self._random_datetime(
                    self._random_date_after(d, max_days=resolve_days)
                )
                # Satisfaction: skewed toward 4-5 for resolved
                satisfaction = self.rng.choices(
                    [1, 2, 3, 4, 5],
                    weights=[5, 8, 15, 35, 37], k=1
                )[0]

            rows.append({
                "ticket_id": tid,
                "customer_id": cust_id,
                "order_id": order_id,
                "category": category,
                "priority": priority,
                "status": status,
                "channel": self._weighted_choice(TICKET_CHANNELS),
                "subject": subject,
                "created_at": self._random_datetime(d),
                "first_response_at": first_response,
                "resolved_at": resolved_at,
                "satisfaction_score": satisfaction,
            })

        self._write_table("support_tickets", fields, rows)

    # ── Parquet conversion ─────────────────────────────────────

    def _convert_to_parquet(self, parquet_dir: Path) -> None:
        """Convert generated CSVs to Parquet using DuckDB."""
        import duckdb

        parquet_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"  Converting to Parquet -> {parquet_dir}/")

        conn = duckdb.connect()
        for csv_path in sorted(self.output_dir.glob("*.csv")):
            table_name = csv_path.stem
            parquet_path = parquet_dir / f"{table_name}.parquet"

            conn.execute(
                f"COPY (SELECT * FROM read_csv_auto('{csv_path}')) "
                f"TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )

            # Report stats
            row_count = conn.execute(
                f"SELECT count(*) FROM '{parquet_path}'"
            ).fetchone()[0]
            parquet_size = parquet_path.stat().st_size
            csv_size = csv_path.stat().st_size
            ratio = csv_size / parquet_size if parquet_size > 0 else 0
            logger.info(
                f"    {table_name}: {row_count:,} rows, "
                f"{parquet_size / 1024:.0f} KB "
                f"({ratio:.1f}x compression)"
            )
        conn.close()

    # ── Orchestration ──────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """Generate all tables and return manifest data."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()

        fmt_label = self.output_format.upper()
        logger.info(f"Generating sample data (size: {self.size_name}, format: {fmt_label})")
        logger.info(f"  Period: {self.start_date} to {self.end_date} "
                     f"({self.cfg['months']} months)")
        logger.info(f"  Output: {self.output_dir}/")

        # Phase 1: Generate CSVs (always needed as intermediate)
        csv_dir = self.output_dir
        if self.output_format == "parquet":
            # CSVs go to a temp subdir, only Parquet files in output
            csv_dir = self.output_dir / "_csv_tmp"
            csv_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir = csv_dir  # temporarily redirect CSV writes

        self._generate_customers()
        self._generate_products()
        self._generate_campaigns()
        self._generate_web_sessions()
        self._generate_web_leads()
        self._generate_orders_and_items()
        self._generate_payments()
        self._generate_support_tickets()

        # Phase 2: Convert to Parquet if requested
        if self.output_format == "parquet":
            parquet_dir = csv_dir.parent  # the original output_dir
            self._convert_to_parquet(parquet_dir)
            # Clean up temp CSVs
            import shutil
            shutil.rmtree(csv_dir)
            self.output_dir = parquet_dir  # restore for manifest
        elif self.output_format == "both":
            parquet_dir = self.output_dir / "parquet"
            self._convert_to_parquet(parquet_dir)

        elapsed = time.time() - t0
        total_rows = sum(self.row_counts.values())

        manifest = {
            "generator": "generate_sample_data.py",
            "size": self.size_name,
            "format": self.output_format,
            "seed": self.rng.getstate()[1][0],
            "date_range": {
                "start": str(self.start_date),
                "end": str(self.end_date),
            },
            "tables": self.row_counts,
            "total_rows": total_rows,
            "elapsed_seconds": round(elapsed, 1),
        }
        manifest_path = self.output_dir / "_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info("")
        logger.info(f"Done! {len(self.row_counts)} tables, "
                     f"{total_rows:,} total rows in {elapsed:.1f}s")
        logger.info(f"Manifest: {manifest_path}")
        return manifest


# ── CLI ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic e-commerce sample data as CSV files."
    )
    parser.add_argument(
        "--size", choices=SIZE_CONFIGS.keys(), default="s",
        help="Data size preset (default: s)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/sample"),
        help="Output directory for CSV files (default: data/sample)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--format", choices=["csv", "parquet", "both"], default="csv",
        help="Output format: csv, parquet (via ParquetManager), or both (default: csv)",
    )
    parser.add_argument(
        "--list-sizes", action="store_true",
        help="Show available size presets and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.list_sizes:
        print("\nAvailable size presets:\n")
        print(f"  {'Size':<6} {'Label':<24} {'Customers':>10} {'Products':>10} "
              f"{'Sessions':>10} {'Orders':>10} {'~CSV MB':>8}")
        print(f"  {'─' * 6} {'─' * 24} {'─' * 10} {'─' * 10} "
              f"{'─' * 10} {'─' * 10} {'─' * 8}")
        for key, cfg in SIZE_CONFIGS.items():
            print(f"  {key:<6} {cfg['label']:<24} {cfg['customers']:>10,} "
                  f"{cfg['products']:>10,} {cfg['web_sessions']:>10,} "
                  f"{cfg['orders']:>10,} {cfg['estimated_csv_mb']:>7,}")
        print()
        return

    gen = SampleDataGenerator(
        size=args.size, seed=args.seed,
        output_dir=args.output, output_format=args.format,
    )
    gen.run()


if __name__ == "__main__":
    main()
