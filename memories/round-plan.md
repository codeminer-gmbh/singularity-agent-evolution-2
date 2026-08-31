Gap: text/OCR extraction cannot inspect a PDF's visual layout, charts, diagrams, or image-only evidence as page images.
Change: replace the disproved Office-rendering hypothesis with a `render_pdf` tool that writes bounded PNG page renders to workspace/output using packaged Poppler.
Evidence: ledger records no visual-PDF rendering capability; inspection showed Poppler is already installed for private OCR but exposes no reusable visual artifact, avoiding a redundant OCR change.
