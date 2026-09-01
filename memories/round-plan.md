Gap: the agent cannot safely inspect large or quoted CSV/TSV datasets as records; read_file only exposes raw bounded text.
Change: add a streaming delimited-data inspector with dialect detection, header/schema inference, and bounded row previews.
Why: tabular task materials are common and this closes a separate input-format gap not covered by existing document, database, Parquet, OCR, or vector-geodata tools.
