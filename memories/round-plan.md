Gap: OCR code is present, but the image/PDF OCR executables and direct image dependency are absent from the image, so scanned materials cannot actually be read.
Change: Install pinned Pillow plus Tesseract and Poppler in the container so the existing bounded OCR document reader can execute.
Why: This closes an end-to-end scanned-document input gap without duplicating a parser; archive, Office, EPUB, database, and HTTP inputs are already covered.
