Gap: The agent cannot inspect word-processing or presentation evidence (DOCX/PPTX/ODT) without executing an untrusted office parser or manually unpacking it.
Change: Add a bounded, metadata-and-text-only office-document inspection tool for DOCX, PPTX, and ODT, registered in the shared tool registry.
Why: Office documents are a common hard-task evidence format, while the inherited line already covers spreadsheets, PDFs, images, emails, and archives.
