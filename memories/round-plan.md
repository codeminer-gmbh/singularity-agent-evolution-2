Gap: the agent cannot faithfully expose DOCX revision metadata and marginal content (tracked insertions/deletions, comments, footnotes, headers), which a revision-reconciliation hard task may require.
Change: extend the DOCX extractor with bounded OOXML-part parsing that preserves revision and annotation context alongside visible text.
Choice: this follows the selected DOCX task class and closes a substantive document-semantic gap rather than duplicating the recent YAML/JSON numeric-fidelity work.
