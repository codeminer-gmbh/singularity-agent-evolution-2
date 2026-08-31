Gap: Existing image OCR cannot read a scanned PDF, because PDF extraction returns only embedded text.
Change: OCR PDF pages with no extractable text by rasterizing them locally through Poppler and passing the validated renders to Tesseract under a total budget.
Evidence: Image OCR was already implemented (so the prior plan was stale); scanned PDFs remain a common supplied-artifact workflow that this extractor cannot complete.
