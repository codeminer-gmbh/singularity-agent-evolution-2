Bottleneck: the published tool surface can inspect PDF evidence but cannot create a requested PDF deliverable; it only creates DOCX and XLSX.
Change: add a bounded structured create_pdf tool that writes ordinary text, list, page-break, and table PDF documents to output/ and can be checked with inspect_pdf.
Evidence: objective current tool definitions expose create_docx/create_xlsx but no PDF writer, while PDF inspection is already present; this completes an unserved deliverable path rather than adding another evidence reader.
