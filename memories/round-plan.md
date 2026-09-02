Gap: supplied HTML/HTM evidence has no semantic inspection, so a hard task cannot reliably extract readable text, links, or table structure from markup without spending model turns on raw source.
Change: add bounded, dependency-free HTML inspection that returns title, visible text, links, and table rows through the existing `inspect_document` tool.
Priority: this closes a distinct web-evidence format gap; archives, databases, tabular data, PDFs/images, EPUB, and Office/OpenDocument files already have dedicated inspection.
