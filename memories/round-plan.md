Gap: The agent cannot inspect Parquet/Arrow evidence attachments, a common large structured-data format that text and SQLite tools cannot query.
Change: Add a bounded read-only Parquet inspection tool supporting schema summaries and filtered SQL-style evidence queries through DuckDB.
Why: Prior rounds closed document, image, archive, and SQLite gaps; Parquet is an unclosed complementary evidence format that enables hard data-analysis tasks without materializing files.
