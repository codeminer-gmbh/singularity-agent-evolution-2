Gap: Live web pages commonly arrive compressed, but the HTTP fetch tool exposes their raw bytes as unreadable replacement text.
Change: Teach http_fetch to request and safely decode gzip or deflate content within the existing output budget.
Priority: This completes the new live-research input path for ordinary modern web servers, rather than adding another local-file parser.
