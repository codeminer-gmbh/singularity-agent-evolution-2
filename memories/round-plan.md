Gap: The agent cannot inspect common CSV, TSV, JSONL, or JSON data attachments with schema and SQL aggregation; hard evidence tasks often use these rather than SQLite or Parquet.
Change: Add a bounded read-only `inspect_tabular` tool built on the installed DuckDB relation APIs and expose it through the MCP registry.
Choice: This extends the already proven attachment-inspection pattern without a new dependency, while archives, documents, SQLite, Parquet, and OCR are already covered.
