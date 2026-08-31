Gap: The agent cannot inspect Parquet/Arrow evidence attachments, a common large structured-data format that its text and SQLite tools cannot query.
Change: Add bounded read-only Parquet inspection with schema summaries and filtered SQL-style evidence queries via DuckDB.
Why: Prior rounds closed document, image, archive, and SQLite gaps; Parquet is the unclosed complementary evidence format for hard data-analysis tasks.
