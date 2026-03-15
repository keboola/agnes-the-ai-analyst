"""
Tests for OpenMetadata transformer.

All transformer functions are pure (dict in -> dict/str/list out), so no mocks needed.
"""

import pytest

from connectors.openmetadata.transformer import (
    extract_category,
    extract_dimensions,
    extract_expression,
    extract_grain,
    extract_metric_type,
    extract_owners,
    extract_tag_names,
    extract_unit,
    metric_to_detail_dict,
    metric_to_display_dict,
    metric_to_yaml_dict,
    sanitize_filename,
    strip_html,
    table_to_yaml_dict,
)


# ---------------------------------------------------------------------------
# Helper: build a tag dict the way OpenMetadata returns them
# ---------------------------------------------------------------------------

def _tag(fqn: str, name: str = "") -> dict:
    """Build a minimal OpenMetadata tag dict."""
    tag = {"tagFQN": fqn}
    if name:
        tag["name"] = name
    return tag


# ===========================================================================
# extract_category
# ===========================================================================

class TestExtractCategory:
    def test_extract_category_from_metric_category_tag(self):
        """MetricCategory.finance tag -> 'finance'."""
        tags = [_tag("MetricCategory.finance")]
        assert extract_category(tags) == "finance"

    def test_extract_category_from_category_tag(self):
        """Category.marketing tag -> 'marketing'."""
        tags = [_tag("Category.marketing")]
        assert extract_category(tags) == "marketing"

    def test_extract_category_default(self):
        """No matching tags -> 'general'."""
        tags = [_tag("SomeOther.tag"), _tag("Tier.Tier1")]
        assert extract_category(tags) == "general"

    def test_extract_category_empty_tags(self):
        """Empty tag list -> 'general'."""
        assert extract_category([]) == "general"

    def test_extract_category_metric_category_takes_priority(self):
        """MetricCategory.* is checked before Category.* (iteration order)."""
        tags = [_tag("MetricCategory.finance"), _tag("Category.marketing")]
        assert extract_category(tags) == "finance"

    def test_extract_category_category_fallback_if_no_metric_category(self):
        """Category.* is used when MetricCategory.* is absent."""
        tags = [_tag("Tier.Tier1"), _tag("Category.operations")]
        assert extract_category(tags) == "operations"

    def test_extract_category_with_nested_dot_in_value(self):
        """MetricCategory.sub.area -> 'sub.area' (split on first dot only)."""
        tags = [_tag("MetricCategory.sub.area")]
        assert extract_category(tags) == "sub.area"

    def test_extract_category_missing_tagfqn_key(self):
        """Tag dict without tagFQN key is safely skipped."""
        tags = [{"name": "orphan"}]
        assert extract_category(tags) == "general"


# ===========================================================================
# extract_grain
# ===========================================================================

class TestExtractGrain:
    def test_extract_grain_from_field(self):
        """granularity field takes priority over tags."""
        raw = {
            "granularity": "Daily",
            "tags": [_tag("Grain.monthly")],
        }
        assert extract_grain(raw) == "daily"

    def test_extract_grain_from_tag(self):
        """Grain.monthly tag used when granularity field is absent."""
        raw = {"tags": [_tag("Grain.monthly")]}
        assert extract_grain(raw) == "monthly"

    def test_extract_grain_empty(self):
        """No grain info -> empty string."""
        raw = {"tags": [_tag("Category.finance")]}
        assert extract_grain(raw) == ""

    def test_extract_grain_no_tags_no_field(self):
        """Completely empty metric -> empty string."""
        assert extract_grain({}) == ""

    def test_extract_grain_field_is_none(self):
        """granularity=None should fall through to tags."""
        raw = {"granularity": None, "tags": [_tag("Grain.weekly")]}
        assert extract_grain(raw) == "weekly"

    def test_extract_grain_field_is_empty_string(self):
        """granularity='' should fall through to tags."""
        raw = {"granularity": "", "tags": [_tag("Grain.yearly")]}
        assert extract_grain(raw) == "yearly"

    def test_extract_grain_tag_lowercased(self):
        """Grain tag value is lowercased."""
        raw = {"tags": [_tag("Grain.QUARTERLY")]}
        assert extract_grain(raw) == "quarterly"


# ===========================================================================
# extract_dimensions
# ===========================================================================

class TestExtractDimensions:
    def test_extract_dimensions(self):
        """Multiple Dimension.* tags -> list of dimension names."""
        tags = [
            _tag("Dimension.economic_area"),
            _tag("Dimension.country"),
            _tag("Category.finance"),
        ]
        result = extract_dimensions(tags)
        assert result == ["economic_area", "country"]

    def test_extract_dimensions_empty(self):
        """No Dimension tags -> empty list."""
        tags = [_tag("Category.finance"), _tag("Tier.Tier1")]
        assert extract_dimensions(tags) == []

    def test_extract_dimensions_empty_list(self):
        """Empty tag list -> empty list."""
        assert extract_dimensions([]) == []

    def test_extract_dimensions_preserves_order(self):
        """Dimensions are returned in tag order."""
        tags = [_tag("Dimension.z_last"), _tag("Dimension.a_first")]
        assert extract_dimensions(tags) == ["z_last", "a_first"]


# ===========================================================================
# extract_expression
# ===========================================================================

class TestExtractExpression:
    def test_extract_expression_dict(self):
        """metricExpression as dict with 'expression' key."""
        raw = {"metricExpression": {"expression": "SUM(revenue_usd)"}}
        assert extract_expression(raw) == "SUM(revenue_usd)"

    def test_extract_expression_string(self):
        """metricExpression as plain string."""
        raw = {"metricExpression": "COUNT(DISTINCT order_id)"}
        assert extract_expression(raw) == "COUNT(DISTINCT order_id)"

    def test_extract_expression_empty(self):
        """No metricExpression -> empty string."""
        raw = {"name": "some_metric"}
        assert extract_expression(raw) == ""

    def test_extract_expression_dict_missing_key(self):
        """Dict without 'expression' key -> empty string."""
        raw = {"metricExpression": {"formula": "x + y"}}
        assert extract_expression(raw) == ""

    def test_extract_expression_dict_none_value(self):
        """Dict with expression=None -> empty string."""
        raw = {"metricExpression": {"expression": None}}
        assert extract_expression(raw) == ""

    def test_extract_expression_none(self):
        """metricExpression=None -> empty string (default {} from .get())."""
        raw = {"metricExpression": None}
        # None is not dict and not str, so returns ""
        assert extract_expression(raw) == ""

    def test_extract_expression_empty_dict(self):
        """metricExpression={} -> empty string."""
        raw = {"metricExpression": {}}
        assert extract_expression(raw) == ""


# ===========================================================================
# extract_owners
# ===========================================================================

class TestExtractOwners:
    def test_extract_owners(self):
        """owners list with name/displayName."""
        raw = {
            "owners": [
                {"name": "alice", "displayName": "Alice Smith"},
                {"name": "bob"},
            ]
        }
        assert extract_owners(raw) == ["alice", "bob"]

    def test_extract_owners_display_name_fallback(self):
        """displayName is used when name is absent."""
        raw = {
            "owners": [
                {"displayName": "Charlie Brown"},
            ]
        }
        assert extract_owners(raw) == ["Charlie Brown"]

    def test_extract_owners_empty(self):
        """No owners key -> empty list."""
        raw = {"name": "something"}
        assert extract_owners(raw) == []

    def test_extract_owners_empty_list(self):
        """Empty owners list -> empty list."""
        raw = {"owners": []}
        assert extract_owners(raw) == []

    def test_extract_owners_skips_empty_names(self):
        """Owners with no name or displayName are skipped."""
        raw = {
            "owners": [
                {"email": "no-name@example.com"},
                {"name": "", "displayName": ""},
                {"name": "valid_user"},
            ]
        }
        assert extract_owners(raw) == ["valid_user"]

    def test_extract_owners_name_none_falls_to_display_name(self):
        """name=None falls back to displayName."""
        raw = {
            "owners": [{"name": None, "displayName": "Fallback Name"}]
        }
        assert extract_owners(raw) == ["Fallback Name"]


# ===========================================================================
# extract_metric_type
# ===========================================================================

class TestExtractMetricType:
    def test_extract_metric_type_from_field(self):
        """metricType field takes priority."""
        raw = {
            "metricType": "SUM",
            "tags": [_tag("MetricType.count")],
        }
        assert extract_metric_type(raw) == "sum"

    def test_extract_metric_type_from_tag(self):
        """MetricType.* tag used when field is absent."""
        raw = {"tags": [_tag("MetricType.ratio")]}
        assert extract_metric_type(raw) == "ratio"

    def test_extract_metric_type_empty(self):
        """No metric type info -> empty string."""
        raw = {"tags": [_tag("Category.finance")]}
        assert extract_metric_type(raw) == ""

    def test_extract_metric_type_field_none(self):
        """metricType=None falls through to tags."""
        raw = {"metricType": None, "tags": [_tag("MetricType.average")]}
        assert extract_metric_type(raw) == "average"

    def test_extract_metric_type_lowercased(self):
        """Metric type from field is lowercased."""
        raw = {"metricType": "COUNT", "tags": []}
        assert extract_metric_type(raw) == "count"

    def test_extract_metric_type_tag_lowercased(self):
        """Metric type from tag is lowercased."""
        raw = {"tags": [_tag("MetricType.PERCENTAGE")]}
        assert extract_metric_type(raw) == "percentage"


# ===========================================================================
# extract_unit
# ===========================================================================

class TestExtractUnit:
    def test_extract_unit_from_field(self):
        """unitOfMeasurement field takes priority."""
        raw = {
            "unitOfMeasurement": "USD",
            "tags": [_tag("Unit.EUR")],
        }
        assert extract_unit(raw) == "USD"

    def test_extract_unit_from_tag(self):
        """Unit.* tag used when field is absent."""
        raw = {"tags": [_tag("Unit.count")]}
        assert extract_unit(raw) == "count"

    def test_extract_unit_empty(self):
        """No unit info -> empty string."""
        raw = {"tags": [_tag("Category.finance")]}
        assert extract_unit(raw) == ""

    def test_extract_unit_field_none(self):
        """unitOfMeasurement=None falls through to tags."""
        raw = {"unitOfMeasurement": None, "tags": [_tag("Unit.percent")]}
        assert extract_unit(raw) == "percent"

    def test_extract_unit_field_empty_string(self):
        """unitOfMeasurement='' falls through to tags."""
        raw = {"unitOfMeasurement": "", "tags": [_tag("Unit.GBP")]}
        assert extract_unit(raw) == "GBP"

    def test_extract_unit_preserves_case(self):
        """Unit value from field is NOT lowercased (unlike metric_type)."""
        raw = {"unitOfMeasurement": "USD", "tags": []}
        assert extract_unit(raw) == "USD"


# ===========================================================================
# extract_tag_names
# ===========================================================================

class TestExtractTagNames:
    def test_extract_tag_names_with_name_field(self):
        """Tags with 'name' field use that value."""
        tags = [
            {"name": "finance", "tagFQN": "Category.finance"},
            {"name": "Tier1", "tagFQN": "Tier.Tier1"},
        ]
        assert extract_tag_names(tags) == ["finance", "Tier1"]

    def test_extract_tag_names_from_fqn(self):
        """Tags without 'name' extract last segment of tagFQN."""
        tags = [
            {"tagFQN": "Category.finance"},
            {"tagFQN": "Tier.Tier1"},
        ]
        assert extract_tag_names(tags) == ["finance", "Tier1"]

    def test_extract_tag_names_empty(self):
        """Empty tag list -> empty list."""
        assert extract_tag_names([]) == []

    def test_extract_tag_names_mixed(self):
        """Mix of tags with and without 'name' field."""
        tags = [
            {"name": "explicit_name", "tagFQN": "Category.something_else"},
            {"tagFQN": "Dimension.country"},
        ]
        result = extract_tag_names(tags)
        assert result == ["explicit_name", "country"]

    def test_extract_tag_names_no_name_no_fqn(self):
        """Tag without name or tagFQN is skipped (empty string)."""
        tags = [{"description": "orphan tag"}]
        # tagFQN defaults to "" -> split(".")[-1] is "" -> falsy, skipped
        assert extract_tag_names(tags) == []


# ===========================================================================
# strip_html
# ===========================================================================

class TestStripHtml:
    def test_strip_simple_tags(self):
        assert strip_html("<p>Hello world</p>") == "Hello world"

    def test_strip_nested_tags(self):
        result = strip_html("<p><strong>Bold</strong> and <em>italic</em></p>")
        assert result == "Bold and italic"

    def test_decode_html_entities(self):
        result = strip_html("price&nbsp;&amp;&nbsp;value")
        assert "price" in result
        assert "&" in result
        assert "value" in result
        assert "&nbsp;" not in result
        assert "&amp;" not in result

    def test_list_items(self):
        result = strip_html('<ul><li class="x">First</li><li>Second</li></ul>')
        assert "- First" in result
        assert "- Second" in result

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_none_like(self):
        assert strip_html("") == ""

    def test_plain_text_unchanged(self):
        assert strip_html("No HTML here") == "No HTML here"

    def test_real_openmetadata_description(self):
        """Test with actual OpenMetadata HTML output."""
        html_desc = (
            '<p><strong>Business name: </strong>Live Deals</p>'
            '<p><strong>Purpose:</strong></p>'
            '<p>The&nbsp;<em>Live deals</em>&nbsp;metric measures the&nbsp;breadth '
            'of active, purchasable supply on Groupon.</p>'
        )
        result = strip_html(html_desc)
        assert "<" not in result
        assert "&nbsp;" not in result
        assert "Live Deals" in result
        assert "Live deals" in result
        assert "purchasable supply" in result

    def test_collapses_whitespace(self):
        result = strip_html("<p>  too   many   spaces  </p>")
        assert result == "too many spaces"

    def test_br_tags(self):
        result = strip_html("line1<br/>line2<br>line3")
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result


# sanitize_filename
# ===========================================================================

class TestSanitizeFilename:
    def test_sanitize_filename(self):
        """Spaces and mixed case -> underscores and lowercase."""
        assert sanitize_filename("M1 Operational Margin") == "m1_operational_margin"

    def test_sanitize_filename_special_chars(self):
        """Special characters replaced with underscores."""
        assert sanitize_filename("Revenue (USD) - Net") == "revenue_usd_net"

    def test_sanitize_filename_multiple_underscores_collapsed(self):
        """Consecutive underscores are collapsed."""
        assert sanitize_filename("foo---bar___baz") == "foo_bar_baz"

    def test_sanitize_filename_leading_trailing_stripped(self):
        """Leading and trailing underscores are stripped."""
        assert sanitize_filename("__hello__") == "hello"

    def test_sanitize_filename_already_clean(self):
        """Already clean name passes through unchanged."""
        assert sanitize_filename("total_revenue") == "total_revenue"

    def test_sanitize_filename_numbers(self):
        """Numbers are preserved."""
        assert sanitize_filename("M1+VFM Margin 2024") == "m1_vfm_margin_2024"

    def test_sanitize_filename_empty_string(self):
        """Empty string -> empty string."""
        assert sanitize_filename("") == ""

    def test_sanitize_filename_only_special_chars(self):
        """String of only special chars -> empty string."""
        assert sanitize_filename("@#$%") == ""


# ===========================================================================
# metric_to_yaml_dict
# ===========================================================================

class TestMetricToYamlDict:
    def test_metric_to_yaml_dict(self):
        """Full transformation with all fields populated."""
        raw = {
            "name": "M1 Operational Margin",
            "displayName": "M1 Operational Margin",
            "fullyQualifiedName": "catalog.metrics.m1_margin",
            "description": "  Gross margin after operational costs  ",
            "granularity": "Monthly",
            "metricType": "ratio",
            "unitOfMeasurement": "USD",
            "metricExpression": {"expression": "SUM(m1_margin_usd)"},
            "tags": [
                _tag("MetricCategory.finance"),
                _tag("Dimension.economic_area"),
                _tag("Dimension.country"),
            ],
            "owners": [
                {"name": "alice", "displayName": "Alice Smith"},
            ],
        }
        result = metric_to_yaml_dict(raw)

        assert result["name"] == "m1_operational_margin"
        assert result["display_name"] == "M1 Operational Margin"
        assert result["category"] == "finance"
        assert result["type"] == "ratio"
        assert result["unit"] == "USD"
        assert result["grain"] == "monthly"
        assert result["time_column"] == ""
        assert result["table"] == ""
        assert result["expression"] == "SUM(m1_margin_usd)"
        assert result["description"] == "Gross margin after operational costs"
        assert result["dimensions"] == ["economic_area", "country"]
        assert result["synonyms"] == []
        # Notes contain FQN and owner info
        assert any("catalog.metrics.m1_margin" in n for n in result["notes"])
        assert any("alice" in n for n in result["notes"])

    def test_metric_to_yaml_dict_minimal(self):
        """Minimal metric with empty/missing fields."""
        raw = {"name": "simple"}
        result = metric_to_yaml_dict(raw)

        assert result["name"] == "simple"
        assert result["display_name"] == "simple"
        assert result["category"] == "general"
        assert result["type"] == ""
        assert result["unit"] == ""
        assert result["grain"] == ""
        assert result["expression"] == ""
        assert result["description"] == ""
        assert result["dimensions"] == []
        assert result["synonyms"] == []
        # No FQN -> no source note; no owners -> no owners note
        assert result["notes"] == []

    def test_metric_to_yaml_dict_notes_with_fqn_only(self):
        """Notes include FQN source but no owners when owners missing."""
        raw = {
            "name": "test",
            "fullyQualifiedName": "catalog.test",
        }
        result = metric_to_yaml_dict(raw)
        assert len(result["notes"]) == 1
        assert "FQN: catalog.test" in result["notes"][0]

    def test_metric_to_yaml_dict_description_stripped(self):
        """Description whitespace is stripped."""
        raw = {
            "name": "test",
            "description": "\n  Some description with spaces  \n",
        }
        result = metric_to_yaml_dict(raw)
        assert result["description"] == "Some description with spaces"

    def test_metric_to_yaml_dict_description_none(self):
        """description=None -> empty string."""
        raw = {"name": "test", "description": None}
        result = metric_to_yaml_dict(raw)
        assert result["description"] == ""


# ===========================================================================
# metric_to_display_dict
# ===========================================================================

class TestMetricToDisplayDict:
    def test_metric_to_display_dict(self):
        """Full display dict with all fields."""
        raw = {
            "name": "total_revenue",
            "displayName": "Total Revenue",
            "fullyQualifiedName": "catalog.metrics.total_revenue",
            "description": "Total revenue in USD",
            "granularity": "Daily",
            "tags": [_tag("MetricCategory.finance")],
        }
        result = metric_to_display_dict(raw)

        assert result["name"] == "total_revenue"
        assert result["display_name"] == "Total Revenue"
        assert result["description"] == "Total revenue in USD"
        assert result["grain"] == "daily"
        assert result["category"] == "finance"
        assert result["path"] == "catalog:catalog.metrics.total_revenue"

    def test_metric_to_display_dict_minimal(self):
        """Minimal metric produces valid display dict."""
        raw = {"name": "bare"}
        result = metric_to_display_dict(raw)

        assert result["name"] == "bare"
        assert result["display_name"] == "bare"
        assert result["description"] == ""
        assert result["grain"] == ""
        assert result["category"] == "general"
        assert result["path"] == "catalog:"

    def test_metric_to_display_dict_display_name_fallback(self):
        """displayName falls back to name when absent."""
        raw = {"name": "revenue_net"}
        assert metric_to_display_dict(raw)["display_name"] == "revenue_net"

    def test_metric_to_display_dict_description_none(self):
        """description=None -> empty string."""
        raw = {"name": "test", "description": None}
        assert metric_to_display_dict(raw)["description"] == ""


# ===========================================================================
# metric_to_detail_dict
# ===========================================================================

class TestMetricToDetailDict:
    def _full_raw_metric(self) -> dict:
        """Build a fully-populated raw metric for reuse."""
        return {
            "name": "m1_margin",
            "displayName": "M1 Margin",
            "fullyQualifiedName": "catalog.metrics.m1_margin",
            "description": "M1 operational margin in USD",
            "granularity": "Monthly",
            "metricType": "ratio",
            "unitOfMeasurement": "USD",
            "metricExpression": {"expression": "SUM(m1_margin_usd)"},
            "tags": [
                _tag("MetricCategory.finance"),
                _tag("Dimension.economic_area"),
                _tag("Dimension.country"),
            ],
        }

    def test_metric_to_detail_dict(self):
        """Full detail dict with all sections populated."""
        raw = self._full_raw_metric()
        result = metric_to_detail_dict(raw)

        assert result["name"] == "m1_margin"
        assert result["display_name"] == "M1 Margin"
        assert result["category"] == "finance"
        # Default color when no category_colors provided
        assert result["category_color"] == "#6B7280"

        # metadata section
        assert result["metadata"]["type"] == "ratio"
        assert result["metadata"]["unit"] == "USD"
        assert result["metadata"]["grain"] == "monthly"
        assert result["metadata"]["time_column"] == ""

        # overview section
        assert result["overview"]["description"] == "M1 operational margin in USD"
        assert result["overview"]["key_insights"] == []

        # dimensions
        assert result["dimensions"] == ["economic_area", "country"]

        # sql_examples (expression present)
        assert "expression" in result["sql_examples"]
        assert result["sql_examples"]["expression"]["query"] == "SUM(m1_margin_usd)"
        assert result["sql_examples"]["expression"]["title"] == "Metric Expression"
        assert result["sql_examples"]["expression"]["complexity"] == "simple"

        # technical
        assert result["technical"]["expression"] == "SUM(m1_margin_usd)"
        assert result["technical"]["table"] == ""
        assert result["technical"]["synonyms"] == []
        assert result["technical"]["data_sources"] == []

        # other sections
        assert result["validation"] is None
        assert result["notes"] == {"all": [], "key_insights": []}
        assert result["special_sections"] == {}

    def test_metric_to_detail_dict_with_colors(self):
        """category_colors parameter maps category to color."""
        raw = self._full_raw_metric()
        colors = {"finance": "#10B981", "marketing": "#F59E0B"}
        result = metric_to_detail_dict(raw, category_colors=colors)

        assert result["category_color"] == "#10B981"

    def test_metric_to_detail_dict_color_fallback(self):
        """Unknown category falls back to default gray."""
        raw = self._full_raw_metric()
        colors = {"marketing": "#F59E0B"}
        result = metric_to_detail_dict(raw, category_colors=colors)

        assert result["category_color"] == "#6B7280"

    def test_metric_to_detail_dict_no_expression(self):
        """sql_examples is empty dict when no expression."""
        raw = {"name": "test", "tags": []}
        result = metric_to_detail_dict(raw)

        assert result["sql_examples"] == {}
        assert result["technical"]["expression"] == ""

    def test_metric_to_detail_dict_minimal(self):
        """Minimal metric produces valid detail dict with all sections."""
        raw = {"name": "bare"}
        result = metric_to_detail_dict(raw)

        assert result["name"] == "bare"
        assert result["display_name"] == "bare"
        assert result["category"] == "general"
        assert result["category_color"] == "#6B7280"
        assert result["metadata"]["type"] == ""
        assert result["metadata"]["unit"] == ""
        assert result["metadata"]["grain"] == ""
        assert result["overview"]["description"] == ""
        assert result["dimensions"] == []
        assert result["sql_examples"] == {}

    def test_metric_to_detail_dict_description_stripped(self):
        """Description whitespace is stripped in detail dict."""
        raw = {
            "name": "test",
            "description": "  leading and trailing spaces  ",
            "tags": [],
        }
        result = metric_to_detail_dict(raw)
        assert result["overview"]["description"] == "leading and trailing spaces"


# ===========================================================================
# table_to_yaml_dict
# ===========================================================================

class TestTableToYamlDict:
    def test_table_to_yaml_dict(self):
        """Full table with columns, owners, tags, tier."""
        raw = {
            "name": "order_economics",
            "fullyQualifiedName": "bigquery.prj.dataset.order_economics",
            "description": "  Order-level economics data  ",
            "columns": [
                {
                    "name": "order_id",
                    "dataType": "STRING",
                    "description": "Unique order identifier",
                },
                {
                    "name": "revenue_usd",
                    "dataType": "FLOAT64",
                    "description": "  Revenue in USD  ",
                },
                {
                    "name": "created_at",
                    "dataType": "TIMESTAMP",
                    "description": None,
                },
            ],
            "tags": [
                {"name": "FoundryAI", "tagFQN": "AIAgent.FoundryAI"},
                {"tagFQN": "Tier.Tier1"},
            ],
            "owners": [
                {"name": "data_team", "displayName": "Data Team"},
            ],
        }
        result = table_to_yaml_dict(raw)

        assert result["name"] == "order_economics"
        assert result["fqn"] == "bigquery.prj.dataset.order_economics"
        assert result["description"] == "Order-level economics data"
        assert result["owners"] == ["data_team"]
        assert result["tags"] == ["FoundryAI", "Tier1"]
        assert result["tier"] == "Tier1"

        # Columns
        assert len(result["columns"]) == 3
        assert result["columns"][0] == {
            "name": "order_id",
            "type": "STRING",
            "description": "Unique order identifier",
        }
        assert result["columns"][1] == {
            "name": "revenue_usd",
            "type": "FLOAT64",
            "description": "Revenue in USD",
        }
        # description=None -> empty string after strip
        assert result["columns"][2]["description"] == ""

    def test_table_to_yaml_dict_minimal(self):
        """Minimal table with empty fields."""
        raw = {"name": "empty_table"}
        result = table_to_yaml_dict(raw)

        assert result["name"] == "empty_table"
        assert result["fqn"] == ""
        assert result["description"] == ""
        assert result["owners"] == []
        assert result["tags"] == []
        assert result["tier"] == ""
        assert result["columns"] == []

    def test_table_to_yaml_dict_tier_from_extension(self):
        """Tier extracted from extension field (priority over tags)."""
        raw = {
            "name": "test",
            "extension": {"tier": "Gold"},
            "tags": [{"tagFQN": "Tier.Silver"}],
        }
        result = table_to_yaml_dict(raw)
        assert result["tier"] == "Gold"

    def test_table_to_yaml_dict_tier_from_extension_capital(self):
        """Tier extracted from extension with capital 'Tier' key."""
        raw = {
            "name": "test",
            "extension": {"Tier": "Platinum"},
            "tags": [],
        }
        result = table_to_yaml_dict(raw)
        assert result["tier"] == "Platinum"

    def test_table_to_yaml_dict_tier_from_tag_fallback(self):
        """Tier from tag when extension is absent."""
        raw = {
            "name": "test",
            "tags": [{"tagFQN": "Tier.Tier2"}],
        }
        result = table_to_yaml_dict(raw)
        assert result["tier"] == "Tier2"

    def test_table_to_yaml_dict_no_tier(self):
        """No tier info -> empty string."""
        raw = {
            "name": "test",
            "tags": [{"tagFQN": "Category.finance"}],
        }
        result = table_to_yaml_dict(raw)
        assert result["tier"] == ""

    def test_table_to_yaml_dict_column_missing_fields(self):
        """Columns with missing fields get empty defaults."""
        raw = {
            "name": "test",
            "columns": [{}],
        }
        result = table_to_yaml_dict(raw)
        assert result["columns"] == [
            {"name": "", "type": "", "description": ""},
        ]

    def test_table_to_yaml_dict_description_none(self):
        """description=None -> empty string."""
        raw = {"name": "test", "description": None}
        result = table_to_yaml_dict(raw)
        assert result["description"] == ""
