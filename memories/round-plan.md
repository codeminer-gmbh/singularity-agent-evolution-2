Limitation: `inspect_document` claims XLSX support but reads only `xl/sharedStrings.xml`, so numeric cells, sheet structure, inline strings, formulas, and workbooks without shared strings are invisible.
Change: replace that XLSX shortcut with bounded workbook/worksheet extraction that reconstructs visible cell values and coordinates from OOXML parts.
Evidence: README advertises XLSX evidence inspection, while `documents.py` demonstrably selects only sharedStrings; ledger favors concrete end-to-end robustness over speculative new tool surfaces.
