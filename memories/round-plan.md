Gap: the agent cannot read the text, tables, and formulas in PDF, DOCX, or XLSX task materials, so it must infer from opaque binary files.
Change: add a bounded read_document tool backed by pypdf, python-docx, and openpyxl, available for materials/ and output/ paths.
Priority: this is an unclosed, high-frequency task-input gap noted in the lineage; command execution and archive extraction cannot reliably interpret these formats.
