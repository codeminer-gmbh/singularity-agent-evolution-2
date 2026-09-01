Limitation: The agent can fetch a known public URL but cannot discover relevant sources when a hard research task supplies only a question or topic.
Change: Add a bounded `search_web` tool that queries DuckDuckGo HTML and returns normalized title, URL, and snippet results, then expose it through the shared registry and prompts.
Evidence: The ledger lists public-web retrieval but no discovery/search mechanism among inherited or recent attempts; recent attachment-inspection additions mostly tied, so source discovery is a distinct, broad complementary gap.
