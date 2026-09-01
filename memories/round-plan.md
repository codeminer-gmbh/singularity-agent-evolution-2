Gap: `inspect_document` can OCR an image but cannot reliably report image dimensions, orientation, capture time, or GPS provenance that a hard evidence task may need.
Change: extend bounded image inspection to emit normalized image properties and readable EXIF/GPS fields alongside OCR text.
Priority: this closes the unaddressed visual-evidence provenance gap; existing attachment inspectors already cover documents, mail, archives, databases, tabular data, media, and web pages.
