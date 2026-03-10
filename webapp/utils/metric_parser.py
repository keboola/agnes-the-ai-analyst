"""
Metric Parser Utility
Parses YAML metric definitions and structures data for UI consumption.
"""

import yaml
import re
from pathlib import Path
from typing import Dict, List, Any, Optional


class MetricParser:
    """Parser for business metric YAML files."""

    # Category color mapping (aligned with the design system)
    CATEGORY_COLORS = {
        'finance': '#0d9668',
        'product_usage': '#b45309',
        'sales_revenue': '#0073D1',
        'weekly_leadership_kpis': '#0073D1',
        'revenue': '#0073D1',
        'customers': '#7c3aed',
        'marketing': '#b45309',
        'support': '#EA580C',
    }

    # Complexity keywords for SQL query classification
    ADVANCED_SQL_KEYWORDS = [
        'WITH', 'CTE', 'RECURSIVE', 'WINDOW', 'PARTITION',
        'allocation', 'singletenant', 'multitenant'
    ]

    def __init__(self, metrics_dir: Path):
        """
        Initialize parser with metrics directory.

        Args:
            metrics_dir: Path to directory containing metric YAML files
        """
        self.metrics_dir = Path(metrics_dir)

    def parse_metric(self, metric_path: str) -> Dict[str, Any]:
        """
        Parse a metric YAML file and return structured data for UI.

        Args:
            metric_path: Relative path to metric file (e.g., 'finance/infra_cost.yml')

        Returns:
            Dictionary with structured metric data

        Raises:
            FileNotFoundError: If metric file doesn't exist
            yaml.YAMLError: If YAML is malformed
        """
        file_path = self.metrics_dir / metric_path

        if not file_path.exists():
            raise FileNotFoundError(f"Metric file not found: {metric_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            raw_data = yaml.safe_load(f)

        # YAML files contain a list with single metric definition
        if isinstance(raw_data, list) and len(raw_data) > 0:
            metric = raw_data[0]
        else:
            metric = raw_data

        return self._structure_metric_data(metric)

    def _structure_metric_data(self, metric: Dict[str, Any]) -> Dict[str, Any]:
        """
        Structure raw metric data into UI-friendly format.

        Args:
            metric: Raw metric dictionary from YAML

        Returns:
            Structured metric data matching API response format
        """
        category = metric.get('category', 'unknown')
        notes = metric.get('notes', [])

        structured = {
            'name': metric.get('name', ''),
            'display_name': metric.get('display_name', ''),
            'category': category,
            'category_color': self.CATEGORY_COLORS.get(category, '#6B7280'),
            'metadata': {
                'type': metric.get('type', ''),
                'unit': metric.get('unit', ''),
                'grain': metric.get('grain', ''),
                'time_column': metric.get('time_column', '')
            },
            'overview': {
                'description': self._format_description(metric.get('description', '')),
                'key_insights': self._extract_key_insights(notes)
            },
            'validation': self._get_validation_info(metric.get('validation')),
            'dimensions': metric.get('dimensions', []),
            'notes': {
                'all': notes,
                'key_insights': self._extract_key_insights(notes)
            },
            'sql_examples': self._structure_sql_queries(metric),
            'technical': {
                'table': metric.get('table', ''),
                'expression': metric.get('expression', ''),
                'synonyms': metric.get('synonyms', []),
                'data_sources': self._extract_data_sources(metric)
            },
            'special_sections': {}
        }

        # Add special sections (e.g., cost_allocation_guide)
        if 'cost_allocation_guide' in metric:
            structured['special_sections']['cost_allocation_guide'] = metric['cost_allocation_guide']

        return structured

    def _format_description(self, description: str) -> str:
        """
        Format description text (convert markdown if needed).

        Args:
            description: Raw description text

        Returns:
            Formatted description (currently just strips extra whitespace)
        """
        # Remove extra whitespace and normalize line breaks
        description = re.sub(r'\s+', ' ', description.strip())
        return description

    def _extract_key_insights(self, notes: List[str], max_insights: int = 5) -> List[str]:
        """
        Extract top key insights from notes list.

        Args:
            notes: List of note strings
            max_insights: Maximum number of insights to extract

        Returns:
            List of top key insights
        """
        if not notes:
            return []

        # Return first N notes as key insights
        # In future, could use NLP to prioritize most important notes
        return notes[:max_insights]

    def _structure_sql_queries(self, metric: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        Structure SQL queries from metric data.

        Args:
            metric: Raw metric dictionary

        Returns:
            Dictionary of SQL examples with metadata
        """
        sql_examples = {}

        # Map of SQL field names to user-friendly titles
        sql_fields = {
            'sql': 'Basic Query',
            'sql_by_company': 'By Company',
            'sql_by_technology': 'By Technology',
            'sql_by_action': 'By Action',
            'sql_customer_vs_internal': 'Customer vs Internal',
            'sql_singletenant_allocation': 'Singletenant Allocation',
            'sql_multitenant_allocation': 'Multitenant Allocation'
        }

        for field, title in sql_fields.items():
            if field in metric and metric[field]:
                query = metric[field]
                complexity = self._classify_sql_complexity(query)

                sql_examples[field] = {
                    'title': title,
                    'query': query.strip(),
                    'complexity': complexity
                }

        # Dynamic discovery: auto-detect sql_* keys not in the static map
        for key in metric:
            if key.startswith('sql_') and key not in sql_fields and metric[key]:
                # Generate title from key: "sql_by_channel" -> "By Channel"
                title_parts = key.replace('sql_', '').replace('_', ' ').title()
                # Clean up "By X" pattern
                title = title_parts if title_parts.startswith('By') else title_parts
                query = metric[key]
                complexity = self._classify_sql_complexity(query)
                sql_examples[key] = {
                    'title': title,
                    'query': query.strip(),
                    'complexity': complexity
                }

        return sql_examples

    def _classify_sql_complexity(self, query: str) -> str:
        """
        Classify SQL query complexity.

        Args:
            query: SQL query string

        Returns:
            'simple' or 'advanced'
        """
        query_upper = query.upper()

        # Check for advanced patterns
        for keyword in self.ADVANCED_SQL_KEYWORDS:
            if keyword in query_upper:
                return 'advanced'

        # Check query length (>20 lines = advanced)
        if len(query.split('\n')) > 20:
            return 'advanced'

        return 'simple'

    def _get_validation_info(self, validation: Optional[Any]) -> Optional[Dict[str, Any]]:
        """
        Extract validation information.

        Args:
            validation: Validation data from YAML (can be dict or string)

        Returns:
            Structured validation info or None
        """
        if not validation:
            return None

        # Handle both dict and string formats
        if isinstance(validation, str):
            # String format: validation is the result text directly
            result_text = validation
            method = ''
        elif isinstance(validation, dict):
            # Dict format: validation has 'method' and 'result' keys
            result_text = validation.get('result', '')
            method = validation.get('method', '')
        else:
            return None

        # Extract last updated date from result text if available
        last_updated = None

        # Try to extract date from validation result (common patterns)
        date_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', result_text)
        if date_match:
            last_updated = date_match.group(1)

        return {
            'status': 'validated',
            'accuracy': self._extract_accuracy(result_text),
            'method': method,
            'result': result_text.strip(),
            'last_updated': last_updated
        }

    def _extract_accuracy(self, result_text: str) -> str:
        """
        Extract accuracy percentage from validation result text.

        Args:
            result_text: Validation result text

        Returns:
            Accuracy string (e.g., '100%') or empty string
        """
        # Look for patterns like "100%", "98.7%"
        match = re.search(r'(\d+(?:\.\d+)?%)', result_text)
        if match:
            return match.group(1)

        # Look for keywords indicating perfect match
        if any(keyword in result_text.lower() for keyword in ['exactly', 'perfectly', 'match']):
            return '100%'

        return ''

    def _extract_data_sources(self, metric: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        Extract data sources and join information from metric.

        Args:
            metric: Raw metric dictionary

        Returns:
            List of data source dictionaries
        """
        sources = []

        # Primary table
        if 'table' in metric:
            sources.append({
                'table': metric['table'],
                'type': 'primary'
            })

        # Extract join information from notes (heuristic approach)
        notes = metric.get('notes', [])
        for note in notes:
            # Look for patterns like "JOIN to company via company_id"
            join_match = re.search(r'join(?:s)? to (\w+)(?: via (\w+))?', note, re.IGNORECASE)
            if join_match:
                table_name = join_match.group(1)
                via_column = join_match.group(2)

                sources.append({
                    'table': table_name,
                    'type': 'join',
                    'via': via_column
                })

        return sources


def parse_metric_yaml(metric_path: str, metrics_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convenience function to parse a metric YAML file.

    Args:
        metric_path: Relative path to metric file
        metrics_dir: Directory containing metrics (defaults to /data/docs/metrics)

    Returns:
        Structured metric data
    """
    if metrics_dir is None:
        metrics_dir = Path('/data/docs/metrics')

    parser = MetricParser(metrics_dir)
    return parser.parse_metric(metric_path)
