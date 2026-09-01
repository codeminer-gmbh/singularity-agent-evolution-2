Missing capability: the agent can read an XLSX as flat TSV but cannot reliably expose worksheet structure, formulas, table ranges, merged headers, or typed cells for spreadsheet-analysis tasks.
Change: add a bounded `inspect_workbook` normal tool that returns structured workbook metadata and a typed cell/formula sample.
Why this gap: the ledger records repeated document-format additions, but none closes structured spreadsheet inspection; this materially improves tasks that ask the model to analyze or repair an Excel workbook rather than merely quote its cells.
