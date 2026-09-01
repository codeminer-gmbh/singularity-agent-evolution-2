Gap: Apple Mail EMLX evidence can include a plist trailer that a generic RFC email parser wrongly treats as message content.
Change: honor EMLX's leading byte-count record and parse only the declared RFC 5322 payload.
Priority: this closes a correctness gap for a common mail-export format, beyond existing generic documents and archives.
