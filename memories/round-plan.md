Gap: `read_document` cannot extract OpenDocument Text (.odt) files, a common task-material format distinct from the existing ODS spreadsheet support.
Change: add a bounded native ODT package/XML extractor and publish .odt in the document tool contract.
Priority: this closes an unaddressed input-format gap with no new dependency, whereas recent rounds already expanded JSON/YAML and other structured-data readers.
