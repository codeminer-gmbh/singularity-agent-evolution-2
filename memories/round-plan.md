Gap: The agent can extract XLSX text but cannot read OpenDocument spreadsheets (.ods), a common office-workbook format a hard task may supply.
Change: Extend bounded document extraction with safe ODS table parsing and expose .ods in the existing read_document capability.
Why: Recent lineage already closed archive, OCR, delimited, Parquet, and vector-data gaps; ODS is a distinct, widely used structured input format absent from the supported extractor.
