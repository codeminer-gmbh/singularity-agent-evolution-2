Gap: The agent cannot retrieve remote HTML, text, or JSON evidence for web-research tasks despite network access.
Change: Add a bounded `fetch_url` tool that follows only limited HTTP(S) redirects and returns decoded text or JSON metadata.
Priority: Remote evidence is a broad missing input class; it complements local inspection and is more generally useful than another format-specific creator.
