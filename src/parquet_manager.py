"""
Parquet File Manager

Parquet file management:
1. CSV -> Parquet conversion with data type application
2. Compression (snappy) for space saving
3. Metadata embedding (table_id, export_date)
4. Information about existing Parquet files
5. Merge/upsert operations for incremental sync

Parquet format advantages:
- Columnar storage -> faster analytical queries
- Compression -> smaller size than CSV
- Schema enforcement -> type safety
- Metadata support -> self-documenting
"""

import logging
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


logger = logging.getLogger(__name__)


def convert_date_columns_to_date32(table: pa.Table, date_columns: List[str]) -> pa.Table:
    """
    Convert timestamp/string columns to DATE32 type.

    Extracted from csv_to_parquet() for reuse in partitioned sync.
    Invalid date values (like '0000-00-00') are converted to NULL, type stays DATE32.

    Args:
        table: PyArrow table to convert
        date_columns: List of column names to convert to DATE32

    Returns:
        PyArrow table with converted date columns
    """
    if not date_columns:
        return table

    schema_fields = []
    for i, field in enumerate(table.schema):
        if field.name in date_columns:
            schema_fields.append(pa.field(field.name, pa.date32()))
        else:
            schema_fields.append(field)

    # Cast columns to DATE32
    columns = []
    for i, field in enumerate(table.schema):
        if field.name in date_columns:
            col = table.column(i)

            # Skip if all nulls - nothing to convert, just cast type
            if col.null_count == len(col):
                columns.append(pa.nulls(len(col), type=pa.date32()))
                continue

            # If column is string type, use pandas for robust parsing with errors='coerce'
            # This converts invalid dates to NaT (NULL) while keeping the DATE type
            if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
                # Convert to pandas, parse dates with coerce (invalid -> NaT)
                series = col.to_pandas()
                parsed = pd.to_datetime(series, errors='coerce', format='mixed')

                # Count invalid values for logging
                invalid_count = parsed.isna().sum() - series.isna().sum()
                if invalid_count > 0:
                    # Find examples of invalid values
                    invalid_mask = series.notna() & parsed.isna()
                    examples = series[invalid_mask].head(3).tolist()
                    logger.warning(
                        f"Column '{field.name}': {invalid_count} invalid date values converted to NULL. "
                        f"Examples: {examples}"
                    )

                # Convert to date only (remove time component) and then to PyArrow
                date_series = parsed.dt.date
                date_array = pa.array(date_series, type=pa.date32())
                columns.append(date_array)
            else:
                # Column is already timestamp/date type, just cast
                try:
                    columns.append(col.cast(pa.date32()))
                except Exception as e:
                    logger.warning(
                        f"Column '{field.name}': Failed to cast to date32, keeping original type. Error: {e}"
                    )
                    columns.append(col)
                    schema_fields[i] = field
        else:
            columns.append(table.column(i))

    # Rebuild table with new schema
    new_schema = pa.schema(schema_fields, metadata=table.schema.metadata)
    return pa.Table.from_arrays(columns, schema=new_schema)


def apply_schema_to_table(table: pa.Table, target_schema: pa.Schema) -> pa.Table:
    """
    Apply target schema to PyArrow table, handling type mismatches gracefully.

    This function:
    - Casts null-type columns to proper types (prevents DuckDB schema mismatch)
    - Attempts safe casts for other type mismatches
    - Keeps original type on cast failure (logs warning, no data loss)
    - Preserves columns not in target schema with their inferred type

    Args:
        table: PyArrow table to cast
        target_schema: Target PyArrow schema

    Returns:
        PyArrow table with applied schema
    """
    if not target_schema:
        return table

    # Build mapping of column names to target types
    target_types = {field.name: field.type for field in target_schema}

    columns = []
    schema_fields = []

    for i, field in enumerate(table.schema):
        col = table.column(i)
        col_name = field.name

        # Column not in target schema - keep as-is
        if col_name not in target_types:
            columns.append(col)
            schema_fields.append(field)
            continue

        target_type = target_types[col_name]

        # Case 1: Column has null type -> create typed null array
        if pa.types.is_null(col.type):
            columns.append(pa.nulls(len(col), type=target_type))
            schema_fields.append(pa.field(col_name, target_type))
            logger.debug(f"Column '{col_name}': converted null type to {target_type}")
            continue

        # Case 2: Column type matches target -> keep as-is
        if col.type == target_type:
            columns.append(col)
            schema_fields.append(pa.field(col_name, target_type))
            continue

        # Case 3: Type mismatch -> try safe cast
        try:
            casted = col.cast(target_type, safe=True)
            columns.append(casted)
            schema_fields.append(pa.field(col_name, target_type))
            logger.debug(f"Column '{col_name}': cast from {col.type} to {target_type}")
        except Exception as e:
            # Cast failed -> keep original type, log warning
            logger.warning(
                f"Column '{col_name}': cannot cast from {col.type} to {target_type}, "
                f"keeping original type. Error: {e}"
            )
            columns.append(col)
            schema_fields.append(field)

    # Rebuild table with new schema
    new_schema = pa.schema(schema_fields, metadata=table.schema.metadata)
    return pa.Table.from_arrays(columns, schema=new_schema)


def _convert_column(series: pd.Series, dtype: str, col_name: str = "") -> pd.Series:
    """
    Convert pandas Series to dtype, handling empty strings.

    Empty strings become NA/NaN for non-string types.
    Logs warning if invalid (non-empty) values are found.

    Args:
        series: Input pandas Series
        dtype: Target dtype (e.g., "Int64", "float64", "boolean")
        col_name: Column name for logging

    Returns:
        Converted Series
    """
    # Replace empty strings with NA for non-string types
    if dtype != "object":
        series = series.replace('', pd.NA)

    # Numeric types - use errors='coerce' but log invalid values
    if dtype in ("Int64", "float64", "Float64"):
        # Count non-null values before conversion
        non_null_before = series.notna().sum()

        converted = pd.to_numeric(series, errors='coerce')

        # Count how many became NA after conversion (excluding already NA)
        non_null_after = converted.notna().sum()
        invalid_count = non_null_before - non_null_after

        if invalid_count > 0:
            # Find examples of invalid values
            invalid_mask = series.notna() & converted.isna()
            examples = series[invalid_mask].head(3).tolist()
            logger.warning(
                f"Column '{col_name}': {invalid_count} invalid values converted to NULL. "
                f"Examples: {examples}"
            )

        return converted.astype(dtype)

    # Boolean type - map string representations
    if dtype == "boolean":
        # If pandas already parsed as bool, just convert to nullable boolean
        if series.dtype == bool or series.dtype == 'object' and series.dropna().apply(lambda x: isinstance(x, bool)).all():
            return series.astype(dtype)

        bool_map = {
            'true': True, 'false': False,
            'True': True, 'False': False,
            'TRUE': True, 'FALSE': False,
            '1': True, '0': False,
            'yes': True, 'no': False,
            'Yes': True, 'No': False,
            'YES': True, 'NO': False,
        }
        # Log unknown values (non-empty strings that aren't in bool_map)
        known_values = set(bool_map.keys())
        non_na_values = series.dropna()
        unknown = non_na_values[~non_na_values.isin(known_values)]
        if len(unknown) > 0:
            examples = unknown.head(3).tolist()
            logger.warning(
                f"Column '{col_name}': {len(unknown)} unknown boolean values converted to NULL. "
                f"Examples: {examples}"
            )
        return series.map(bool_map).astype(dtype)

    # Default: direct conversion
    return series.astype(dtype)


class ParquetManager:
    """
    Parquet file manager.

    Provides methods for:
    - CSV -> Parquet conversion
    - Getting information about Parquet files
    - Merge/upsert for incremental sync
    """

    def __init__(self):
        """Initialize Parquet manager."""
        # Compression codec - snappy is fast and has good compression ratio
        self.compression = "snappy"

    def csv_to_parquet(
        self,
        csv_path: Path,
        parquet_path: Path,
        dtypes: Optional[Dict[str, str]] = None,
        table_id: Optional[str] = None,
        parse_dates: Optional[List[str]] = None,
        date_columns: Optional[List[str]] = None,
        pyarrow_schema: Optional[pa.Schema] = None
    ) -> Dict[str, Any]:
        """
        Convert CSV file to Parquet format.

        Args:
            csv_path: Path to source CSV file
            parquet_path: Path where to save Parquet file
            dtypes: Dictionary with data types for columns (pandas dtypes)
            table_id: Table ID for metadata
            parse_dates: List of columns with dates/timestamps to parse
            date_columns: List of DATE-only columns (without time) to convert to PyArrow DATE32
            pyarrow_schema: Optional PyArrow schema to enforce (prevents null-type columns)

        Returns:
            Dictionary with conversion information:
            - rows: Number of rows
            - columns: Number of columns
            - file_size_bytes: Parquet file size
            - compression_ratio: Compression ratio (CSV size / Parquet size)

        Raises:
            Exception: If conversion fails
        """
        logger.info(f"Converting CSV -> Parquet: {csv_path.name}")

        try:
            # Load CSV into pandas DataFrame
            # IMPORTANT: Use dtype=str to prevent pandas from guessing types
            # We apply our own types from Keboola metadata using _convert_column
            read_kwargs = {"dtype": str}

            # Get actual column names from CSV header first
            with open(csv_path, 'r') as f:
                header_line = f.readline().strip()
                csv_columns = set(col.strip('"') for col in header_line.split(','))

            # Parse datetime columns - only those that exist in CSV
            if parse_dates:
                valid_parse_dates = [col for col in parse_dates if col in csv_columns]
                if valid_parse_dates:
                    read_kwargs["parse_dates"] = valid_parse_dates
            elif dtypes:
                # Auto-detect datetime columns from dtypes (only existing columns)
                datetime_cols = [
                    col for col, dtype in dtypes.items()
                    if "datetime" in dtype and col in csv_columns
                ]
                if datetime_cols:
                    read_kwargs["parse_dates"] = datetime_cols

            df = pd.read_csv(csv_path, **read_kwargs)

            logger.debug(f"CSV loaded: {len(df)} rows, {len(df.columns)} columns")

            # Apply dtypes using _convert_column to handle empty strings
            if dtypes:
                for col, dtype in dtypes.items():
                    if col in df.columns and "datetime" not in dtype:
                        try:
                            df[col] = _convert_column(df[col], dtype, col_name=col)
                        except Exception as e:
                            logger.warning(
                                f"Failed to apply dtype {dtype} to column {col}: {e}"
                            )

            # Add metadata as custom schema metadata
            metadata = {
                "created_at": datetime.now().isoformat(),
            }
            if table_id:
                metadata["table_id"] = table_id

            # Convert to PyArrow Table
            # PyArrow preserves pandas dtypes and adds metadata
            table = pa.Table.from_pandas(df)

            # Convert DATE columns from timestamp/string to DATE32
            if date_columns:
                table = convert_date_columns_to_date32(table, date_columns)

            # Apply explicit schema (prevents null-type columns)
            if pyarrow_schema:
                table = apply_schema_to_table(table, pyarrow_schema)

            # Add custom metadata
            existing_metadata = table.schema.metadata or {}
            new_metadata = {
                **existing_metadata,
                **{k.encode(): v.encode() for k, v in metadata.items()}
            }
            table = table.replace_schema_metadata(new_metadata)

            # Ensure output folder exists
            parquet_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to Parquet
            pq.write_table(
                table,
                parquet_path,
                compression=self.compression
            )

            # Get file sizes for compression ratio
            csv_size = csv_path.stat().st_size
            parquet_size = parquet_path.stat().st_size
            compression_ratio = csv_size / parquet_size if parquet_size > 0 else 0

            result = {
                "rows": len(df),
                "columns": len(df.columns),
                "csv_size_bytes": csv_size,
                "parquet_size_bytes": parquet_size,
                "compression_ratio": compression_ratio,
                "parquet_path": str(parquet_path)
            }

            logger.info(
                f"Parquet created: {len(df)} rows, "
                f"{parquet_size / 1024 / 1024:.2f} MB, "
                f"compression {compression_ratio:.2f}x"
            )

            return result

        except Exception as e:
            logger.error(f"Error converting CSV -> Parquet: {e}")
            raise

    def get_parquet_info(self, parquet_path: Path) -> Optional[Dict[str, Any]]:
        """
        Get information about existing Parquet file.

        Args:
            parquet_path: Path to Parquet file

        Returns:
            Dictionary with information:
            - rows: Number of rows
            - columns: Number of columns
            - file_size_bytes: File size
            - modified_at: Last modification timestamp
            - schema: PyArrow schema
            - metadata: Custom metadata
            Or None if file doesn't exist.
        """
        if not parquet_path.exists():
            return None

        try:
            # Load Parquet metadata (without loading data)
            parquet_file = pq.ParquetFile(parquet_path)

            # Basic info
            file_size = parquet_path.stat().st_size
            modified_at = datetime.fromtimestamp(parquet_path.stat().st_mtime)

            # Schema and metadata
            schema = parquet_file.schema_arrow
            custom_metadata = {}
            if schema.metadata:
                custom_metadata = {
                    k.decode(): v.decode()
                    for k, v in schema.metadata.items()
                    if k.decode() not in ["pandas"]  # Filter pandas internal metadata
                }

            info = {
                "rows": parquet_file.metadata.num_rows,
                "columns": len(schema),
                "file_size_bytes": file_size,
                "modified_at": modified_at.isoformat(),
                "schema": schema,
                "metadata": custom_metadata,
                "parquet_path": str(parquet_path)
            }

            return info

        except Exception as e:
            logger.error(f"Error reading Parquet info: {e}")
            return None

    def merge_parquet(
        self,
        existing_parquet: Path,
        new_csv: Path,
        output_parquet: Path,
        primary_key: List[str],
        dtypes: Optional[Dict[str, str]] = None,
        parse_dates: Optional[List[str]] = None,
        date_columns: Optional[List[str]] = None,
        pyarrow_schema: Optional[pa.Schema] = None
    ) -> Dict[str, Any]:
        """
        Merge new data from CSV into existing Parquet file.

        Performs upsert operation:
        - Rows with existing primary_key are updated
        - Rows with new primary_key are added

        Args:
            existing_parquet: Path to existing Parquet file
            new_csv: Path to CSV with new data
            output_parquet: Path where to save resulting Parquet
            primary_key: List of column names forming the primary key (supports composite PK)
            dtypes: Dictionary with data types
            parse_dates: List of datetime columns
            date_columns: List of DATE-only columns to convert to PyArrow DATE32
            pyarrow_schema: Optional PyArrow schema to enforce (prevents null-type columns)

        Returns:
            Dictionary with information:
            - total_rows: Total number of rows after merge
            - added_rows: Number of newly added rows
            - updated_rows: Number of updated rows
            - unchanged_rows: Number of unchanged rows

        Raises:
            Exception: If merge fails
        """
        pk_str = ", ".join(primary_key)
        logger.info(
            f"Merging Parquet: {existing_parquet.name} + {new_csv.name} -> {output_parquet.name} (PK: {pk_str})"
        )

        try:
            # Load existing Parquet
            existing_df = pd.read_parquet(existing_parquet)
            original_count = len(existing_df)

            logger.debug(f"Existing data: {original_count} rows")

            # Load new data from CSV
            # IMPORTANT: Use dtype=str to prevent pandas from guessing types
            # We apply our own types from Keboola metadata using _convert_column
            read_kwargs = {"dtype": str}

            if parse_dates:
                read_kwargs["parse_dates"] = parse_dates
            elif dtypes:
                datetime_cols = [
                    col for col, dtype in dtypes.items()
                    if "datetime" in dtype
                ]
                if datetime_cols:
                    read_kwargs["parse_dates"] = datetime_cols

            new_df = pd.read_csv(new_csv, **read_kwargs)

            # Apply dtypes using _convert_column to handle empty strings
            if dtypes:
                for col, dtype in dtypes.items():
                    if col in new_df.columns and "datetime" not in dtype:
                        try:
                            new_df[col] = _convert_column(new_df[col], dtype, col_name=col)
                        except Exception as e:
                            logger.warning(
                                f"Failed to apply dtype {dtype} to column {col}: {e}"
                            )

            new_count = len(new_df)

            logger.debug(f"New data: {new_count} rows")

            # Check that all primary_key columns exist in both dataframes
            for pk_col in primary_key:
                if pk_col not in existing_df.columns:
                    raise ValueError(
                        f"Primary key column '{pk_col}' not found in existing data"
                    )
                if pk_col not in new_df.columns:
                    raise ValueError(
                        f"Primary key column '{pk_col}' not found in new data"
                    )

            # Perform upsert: concat and then drop_duplicates with keep='last'
            # Keep='last' means that new data (which is second) will overwrite old
            merged_df = pd.concat([existing_df, new_df], ignore_index=True)
            merged_df = merged_df.drop_duplicates(subset=primary_key, keep='last')

            # Calculate statistics
            final_count = len(merged_df)
            added_rows = final_count - original_count
            # Updated rows = rows that were in both datasets
            updated_rows = new_count - added_rows if added_rows < new_count else 0
            unchanged_rows = original_count - updated_rows

            logger.info(
                f"Merge completed: {final_count} total rows "
                f"(+{added_rows} new, ~{updated_rows} updates)"
            )

            # Save as new Parquet
            # Prepare metadata
            metadata = {
                "created_at": datetime.now().isoformat(),
                "merged_from": new_csv.name
            }

            table = pa.Table.from_pandas(merged_df)

            # Convert DATE columns from timestamp to DATE32
            if date_columns:
                table = convert_date_columns_to_date32(table, date_columns)

            # Apply explicit schema (prevents null-type columns)
            if pyarrow_schema:
                table = apply_schema_to_table(table, pyarrow_schema)

            new_metadata = {
                k.encode(): v.encode() for k, v in metadata.items()
            }
            table = table.replace_schema_metadata(new_metadata)

            # Ensure output folder exists
            output_parquet.parent.mkdir(parents=True, exist_ok=True)

            # Write Parquet
            pq.write_table(table, output_parquet, compression=self.compression)

            result = {
                "total_rows": final_count,
                "total_columns": len(merged_df.columns),
                "added_rows": added_rows,
                "updated_rows": updated_rows,
                "unchanged_rows": unchanged_rows,
                "parquet_path": str(output_parquet)
            }

            return result

        except Exception as e:
            logger.error(f"Error merging Parquet: {e}")
            raise

    def validate_parquet(self, parquet_path: Path) -> bool:
        """
        Validate that Parquet file is readable and not corrupted.

        Args:
            parquet_path: Path to Parquet file

        Returns:
            True if file is valid, False otherwise
        """
        try:
            # Try to load schema (fast operation)
            parquet_file = pq.ParquetFile(parquet_path)
            _ = parquet_file.schema_arrow

            # Try to load first row (data validation)
            # Note: pyarrow 23.0 doesn't have nrows parameter, load full file then limit
            df = pd.read_parquet(parquet_path)
            _ = df.head(1)

            return True
        except Exception as e:
            logger.error(f"Parquet validation failed: {e}")
            return False


def create_parquet_manager() -> ParquetManager:
    """
    Factory function to create ParquetManager instance.

    Returns:
        ParquetManager instance
    """
    return ParquetManager()


if __name__ == "__main__":
    # Test Parquet manager
    print("📦 Testing Parquet manager...")

    import tempfile

    try:
        manager = create_parquet_manager()

        # Create test CSV
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Test data
            test_csv = tmpdir / "test.csv"
            test_csv.write_text(
                "id,name,value,created_at\n"
                "1,Alice,100,2026-01-01\n"
                "2,Bob,200,2026-01-02\n"
                "3,Charlie,300,2026-01-03\n"
            )

            test_parquet = tmpdir / "test.parquet"

            # Test 1: CSV -> Parquet
            print("\n1️⃣ Testing CSV -> Parquet conversion...")
            result = manager.csv_to_parquet(
                csv_path=test_csv,
                parquet_path=test_parquet,
                dtypes={"id": "Int64", "name": "object", "value": "Int64"},
                parse_dates=["created_at"],
                table_id="test.table"
            )
            print(f"   ✅ Conversion OK:")
            print(f"      Rows: {result['rows']}")
            print(f"      Compression: {result['compression_ratio']:.2f}x")

            # Test 2: Parquet info
            print("\n2️⃣ Testing Parquet info...")
            info = manager.get_parquet_info(test_parquet)
            if info:
                print(f"   ✅ Info loaded:")
                print(f"      Rows: {info['rows']}")
                print(f"      Metadata: {info['metadata']}")

            # Test 3: Validation
            print("\n3️⃣ Testing validation...")
            if manager.validate_parquet(test_parquet):
                print("   ✅ Parquet is valid!")

            # Test 4: Merge
            print("\n4️⃣ Testing merge...")
            # Create update CSV
            update_csv = tmpdir / "update.csv"
            update_csv.write_text(
                "id,name,value,created_at\n"
                "2,Bob Updated,250,2026-01-04\n"  # Update
                "4,David,400,2026-01-05\n"  # New
            )

            merged_parquet = tmpdir / "merged.parquet"
            merge_result = manager.merge_parquet(
                existing_parquet=test_parquet,
                new_csv=update_csv,
                output_parquet=merged_parquet,
                primary_key=["id"],  # Now uses list
                dtypes={"id": "Int64", "name": "object", "value": "Int64"},
                parse_dates=["created_at"]
            )
            print(f"   ✅ Merge OK:")
            print(f"      Total rows: {merge_result['total_rows']}")
            print(f"      Added: {merge_result['added_rows']}")
            print(f"      Updated: {merge_result['updated_rows']}")

            # Test 5: Empty string handling
            print("\n5️⃣ Testing empty string handling...")
            empty_csv = tmpdir / "empty_strings.csv"
            empty_csv.write_text(
                "id,is_active,revenue,note\n"
                '1,true,100.5,text\n'
                '2,,200.0,\n'           # Empty boolean and string
                '3,false,,note\n'       # Empty float
                '4,TRUE,N/A,\n'         # Invalid float value
            )

            empty_parquet = tmpdir / "empty_strings.parquet"
            result = manager.csv_to_parquet(
                csv_path=empty_csv,
                parquet_path=empty_parquet,
                dtypes={
                    "id": "Int64",
                    "is_active": "boolean",
                    "revenue": "float64",
                    "note": "object"
                },
                table_id="test.empty_strings"
            )
            print(f"   ✅ Conversion with empty strings OK:")
            print(f"      Rows: {result['rows']}")

            # Verify the data
            df = pd.read_parquet(empty_parquet)
            print(f"      Dtypes: {dict(df.dtypes)}")
            print(f"      is_active nulls: {df['is_active'].isna().sum()}")
            print(f"      revenue nulls: {df['revenue'].isna().sum()}")

        print("\n✅ All tests passed!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
