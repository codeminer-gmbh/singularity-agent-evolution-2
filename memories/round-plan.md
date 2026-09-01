Gap: The agent cannot inspect scientific/geospatial evidence such as GeoJSON, KML, GPX, or shapefile archives with a bounded structural and queryable view.
Change: Add a read-only `inspect_geospatial` tool for common vector geodata, including schema, feature bounds, and bounded SQL queries.
Priority: Existing tools cover office, media, archives, email, databases, Parquet, and tabular evidence; geospatial vector attachments remain an unclosed, hard-task-relevant input class.
