Gap: existing Office readers already cover PowerPoint, but the agent cannot inspect EPUB ebooks, whose ordered chapters and embedded text may be the only task material.
Change: add a bounded, dependency-free EPUB reader that follows the package spine and converts XHTML chapter bodies to text through `read_document`.
Priority: this closes an unaddressed common document format without another large runtime dependency, unlike already-covered Office, archive, and SQLite inputs.
