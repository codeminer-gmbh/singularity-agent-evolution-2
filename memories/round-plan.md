Gap: The agent cannot extract readable content from RFC 822 `.eml` email messages, a common task material containing headers, multipart bodies, and attached documents.
Change: Add bounded standard-library EML parsing to `read_document`, selecting decoded plain/HTML body text and reporting attachment metadata.
Why: ODS, EPUB, OCR, delimited, Parquet, geodata, and ZIP/TAR inputs are already covered; EML is a distinct ubiquitous office-record format that needs no fragile new runtime dependency.
