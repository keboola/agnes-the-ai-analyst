"""
OpenMetadata data transformer.

Shared logic for parsing OpenMetadata API responses into structured dicts
suitable for YAML export and webapp display. Used by:
- src/catalog_export.py (YAML file generation)
- webapp/app.py (metric list and detail display)

Extracts metadata from OpenMetadata tag conventions:
- MetricCategory.* or Category.* -> category
- Grain.* -> grain/granularity
- Dimension.* -> dimensions list
- MetricType.* -> metric type
- Unit.* -> unit of measurement
"""

import html
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def extract_category(tags: List[Dict[str, Any]]) -> str:
    """
    Extract metric category from OpenMetadata tags.

    Looks for tagFQN prefixed with "MetricCategory." or "Category.".
    Returns the first match found, or "general" as fallback.

    Args:
        tags: List of tag dicts from OpenMetadata (each with "tagFQN" key)

    Returns:
        Category string (e.g., "finance", "marketing")
    """
    for tag in tags:
        tag_fqn = tag.get("tagFQN", "")
        if tag_fqn.startswith("MetricCategory."):
            return tag_fqn.split(".", 1)[1]
        if tag_fqn.startswith("Category."):
            return tag_fqn.split(".", 1)[1]
    return "general"


def extract_grain(raw_metric: Dict[str, Any]) -> str:
    """
    Extract metric granularity from OpenMetadata metric data.

    Checks the "granularity" field first, then falls back to Grain.* tags.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        Grain string (e.g., "monthly", "daily"), lowercase. Empty string if not found.
    """
    grain = raw_metric.get("granularity", "") or ""
    if grain:
        return grain.lower()

    for tag in raw_metric.get("tags", []):
        tag_fqn = tag.get("tagFQN", "")
        if tag_fqn.startswith("Grain."):
            return tag_fqn.split(".", 1)[1].lower()

    return ""


def extract_dimensions(tags: List[Dict[str, Any]]) -> List[str]:
    """
    Extract dimension names from OpenMetadata tags.

    Looks for tagFQN prefixed with "Dimension.".

    Args:
        tags: List of tag dicts from OpenMetadata

    Returns:
        List of dimension names (e.g., ["economic_area", "merchant_country"])
    """
    dimensions = []
    for tag in tags:
        tag_fqn = tag.get("tagFQN", "")
        if tag_fqn.startswith("Dimension."):
            dimensions.append(tag_fqn.split(".", 1)[1])
    return dimensions


def extract_expression(raw_metric: Dict[str, Any]) -> str:
    """
    Extract metric SQL expression from OpenMetadata metric data.

    Handles both dict format ({"expression": "..."}) and plain string.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        SQL expression string, or empty string if not found.
    """
    metric_expr = raw_metric.get("metricExpression", {})
    if isinstance(metric_expr, dict):
        return metric_expr.get("expression", "") or ""
    if isinstance(metric_expr, str):
        return metric_expr
    return ""


def extract_owners(raw: Dict[str, Any]) -> List[str]:
    """
    Extract owner names from OpenMetadata entity data.

    Args:
        raw: Raw entity dict with optional "owners" list

    Returns:
        List of owner name strings
    """
    names = []
    for owner in raw.get("owners", []):
        name = owner.get("name") or owner.get("displayName", "")
        if name:
            names.append(name)
    return names


def extract_metric_type(raw_metric: Dict[str, Any]) -> str:
    """
    Extract metric type from OpenMetadata metric data.

    Checks "metricType" field first, then MetricType.* tags.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        Metric type string (e.g., "sum", "count"), lowercase.
    """
    metric_type = raw_metric.get("metricType", "") or ""
    if metric_type:
        return metric_type.lower()

    for tag in raw_metric.get("tags", []):
        tag_fqn = tag.get("tagFQN", "")
        if tag_fqn.startswith("MetricType."):
            return tag_fqn.split(".", 1)[1].lower()

    return ""


def extract_unit(raw_metric: Dict[str, Any]) -> str:
    """
    Extract unit of measurement from OpenMetadata metric data.

    Checks "unitOfMeasurement" field first, then Unit.* tags.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        Unit string (e.g., "USD", "count").
    """
    unit = raw_metric.get("unitOfMeasurement", "") or ""
    if unit:
        return unit

    for tag in raw_metric.get("tags", []):
        tag_fqn = tag.get("tagFQN", "")
        if tag_fqn.startswith("Unit."):
            return tag_fqn.split(".", 1)[1]

    return ""


def has_tag(tags: List[Dict[str, Any]], tag_fqn: str) -> bool:
    """
    Check if a specific tag (by FQN) is present in the tag list.

    Args:
        tags: List of tag dicts from OpenMetadata
        tag_fqn: Fully qualified tag name to check (e.g., "AIAgent.FoundryAI")

    Returns:
        True if the tag is found
    """
    return any(t.get("tagFQN", "") == tag_fqn for t in tags)


def extract_tag_names(tags: List[Dict[str, Any]]) -> List[str]:
    """
    Extract simple tag names from OpenMetadata tag list.

    Uses "name" field if present, otherwise extracts last segment of "tagFQN".

    Args:
        tags: List of tag dicts from OpenMetadata

    Returns:
        List of tag name strings
    """
    result = []
    for tag in tags:
        name = tag.get("name") or tag.get("tagFQN", "").split(".")[-1]
        if name:
            result.append(name)
    return result


def strip_html(text: str) -> str:
    """
    Strip HTML tags and decode entities from OpenMetadata descriptions.

    OpenMetadata stores descriptions as rich HTML. This converts to clean
    plain text suitable for YAML files and agent consumption.

    Handles:
    - HTML tags (<p>, <strong>, <em>, <ul>, <li>, etc.)
    - HTML entities (&nbsp;, &amp;, etc.)
    - List items (converted to "- " prefix)
    - Excessive whitespace from tag removal

    Args:
        text: Raw HTML string from OpenMetadata

    Returns:
        Clean plain text string
    """
    if not text:
        return ""

    # Convert <li> to list items before stripping tags
    result = re.sub(r"<li[^>]*>", "\n- ", text)

    # Convert block-level tags to newlines
    result = re.sub(r"<br\s*/?>", "\n", result)
    result = re.sub(r"</(?:p|div|h[1-6]|tr|ul|ol)>", "\n", result)

    # Remove all remaining HTML tags
    result = re.sub(r"<[^>]+>", "", result)

    # Decode HTML entities (&nbsp; -> space, &amp; -> &, etc.)
    result = html.unescape(result)

    # Clean up whitespace: collapse multiple spaces, strip lines
    lines = []
    for line in result.split("\n"):
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)

    return "\n".join(lines)


def sanitize_filename(name: str) -> str:
    """
    Convert metric/entity name to safe filesystem name.

    Replaces non-alphanumeric characters with underscores, collapses
    consecutive underscores, strips leading/trailing underscores, lowercases.

    Args:
        name: Raw entity name (e.g., "M1 Operational Margin")

    Returns:
        Safe filename (e.g., "m1_operational_margin")
    """
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", name)
    safe = re.sub(r"_+", "_", safe)
    return safe.strip("_").lower()


def metric_to_yaml_dict(raw_metric: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform raw OpenMetadata metric into YAML-compatible dict.

    Output format is compatible with MetricParser._structure_metric_data()
    and can be written directly as YAML for Claude Code agent consumption.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        Dict with keys: name, display_name, category, type, unit, grain,
        time_column, table, expression, description, dimensions, notes, synonyms
    """
    tags = raw_metric.get("tags", [])
    name = raw_metric.get("name", "")
    display_name = raw_metric.get("displayName", name)
    fqn = raw_metric.get("fullyQualifiedName", "")

    owner_names = extract_owners(raw_metric)
    notes = []
    if fqn:
        notes.append(f"Source: OpenMetadata catalog (FQN: {fqn})")
    if owner_names:
        notes.append(f"Owners: {', '.join(owner_names)}")

    return {
        "name": sanitize_filename(name),
        "display_name": display_name,
        "category": extract_category(tags),
        "type": extract_metric_type(raw_metric),
        "unit": extract_unit(raw_metric),
        "grain": extract_grain(raw_metric),
        "time_column": "",
        "table": "",
        "expression": extract_expression(raw_metric),
        "description": strip_html(raw_metric.get("description", "") or ""),
        "dimensions": extract_dimensions(tags),
        "notes": notes,
        "synonyms": [],
    }


def metric_to_display_dict(raw_metric: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse raw OpenMetadata metric for metric list display in webapp.

    Returns a lightweight dict for listing metrics (not full detail).
    Description is stripped of HTML and truncated for list view.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API

    Returns:
        Dict with keys: name, display_name, description, grain, category, path
    """
    fqn = raw_metric.get("fullyQualifiedName", "")
    name = raw_metric.get("name", "")
    display_name = raw_metric.get("displayName", name)
    description = raw_metric.get("description", "") or ""
    tags = raw_metric.get("tags", [])

    # Strip HTML and truncate for list excerpt
    clean_desc = strip_html(description)
    if len(clean_desc) > 150:
        clean_desc = clean_desc[:147] + "..."

    return {
        "name": name,
        "display_name": display_name,
        "description": clean_desc,
        "grain": extract_grain(raw_metric),
        "category": extract_category(tags),
        "path": f"catalog:{fqn}",
    }


def metric_to_detail_dict(raw_metric: Dict[str, Any], category_colors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Convert raw OpenMetadata metric into MetricParser-compatible detail dict for modal display.

    Args:
        raw_metric: Raw metric dict from OpenMetadata API
        category_colors: Optional mapping of category -> CSS color hex

    Returns:
        Dict matching MetricParser._structure_metric_data() output format
    """
    if category_colors is None:
        category_colors = {}

    tags = raw_metric.get("tags", [])
    name = raw_metric.get("name", "")
    display_name = raw_metric.get("displayName", name)
    description = raw_metric.get("description", "") or ""
    category = extract_category(tags)
    expression = extract_expression(raw_metric)

    return {
        "name": name,
        "display_name": display_name,
        "category": category,
        "category_color": category_colors.get(category, "#6B7280"),
        "metadata": {
            "type": extract_metric_type(raw_metric),
            "unit": extract_unit(raw_metric),
            "grain": extract_grain(raw_metric),
            "time_column": "",
        },
        "overview": {
            "description": strip_html(description),
            "description_html": description,
            "key_insights": [],
        },
        "validation": None,
        "dimensions": extract_dimensions(tags),
        "notes": {
            "all": [],
            "key_insights": [],
        },
        "sql_examples": {
            "expression": {
                "title": "Metric Expression",
                "query": expression,
                "complexity": "simple",
            }
        } if expression else {},
        "technical": {
            "table": "",
            "expression": expression,
            "synonyms": [],
            "data_sources": [],
        },
        "special_sections": {},
    }


def table_to_yaml_dict(raw_table: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform raw OpenMetadata table response into YAML-compatible dict.

    Extracts table description, column metadata, owners, tags, and tier.
    Reuses parsing logic from CatalogEnricher._parse_table_response().

    Args:
        raw_table: Raw table dict from OpenMetadata /api/v1/tables/name/{fqn}

    Returns:
        Dict with keys: name, fqn, description, owners, tags, tier, columns
    """
    fqn = raw_table.get("fullyQualifiedName", "")
    name = raw_table.get("name", "")
    description = strip_html(raw_table.get("description", "") or "")
    tags = raw_table.get("tags", [])

    # Parse columns
    columns = []
    for col in raw_table.get("columns", []):
        col_entry = {
            "name": col.get("name", ""),
            "type": col.get("dataType", ""),
            "description": strip_html(col.get("description", "") or ""),
        }
        columns.append(col_entry)

    # Parse tier from tags (Tier.Tier1 etc.) or extension
    tier = None
    extension = raw_table.get("extension", {})
    if extension:
        tier = extension.get("tier") or extension.get("Tier")
    if not tier:
        for tag in tags:
            tag_fqn = tag.get("tagFQN", "")
            if tag_fqn.startswith("Tier."):
                tier = tag_fqn.split(".", 1)[1]
                break

    return {
        "name": name,
        "fqn": fqn,
        "description": description.strip(),
        "owners": extract_owners(raw_table),
        "tags": extract_tag_names(tags),
        "tier": tier or "",
        "columns": columns,
    }
