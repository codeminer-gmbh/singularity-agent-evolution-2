Gap: the agent cannot inspect common email-message evidence (RFC 822 .eml or Outlook .msg), including attachments, that a hard document-analysis task may supply.
Change: add a bounded email extraction path to read_document, reporting message headers, bodies, and safely decoded attachments.
Why: archive, OCR, EPUB, databases, and office/PDF extraction were already pursued in this lineage, while email containers remain an unclosed, high-value input class.
