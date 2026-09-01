Gap: the agent cannot recover information from scanned PDFs or image-only task materials, so hard tasks with receipts, forms, and screenshots are unreadable.
Change: add bounded local Tesseract OCR to read_document for common raster images and PDF pages with no native text.
Priority: this is an unclosed input-format gap; existing office parsing, archive extraction, web access, and SQLite querying already cover their respective formats.
