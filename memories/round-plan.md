Gap: The agent cannot directly retrieve, inspect, or cite a public web page/API, so research tasks waste steps trying unavailable shell/network tooling.
Change: Add a bounded fetch_url tool that downloads HTTP(S) resources and returns decoded text or readable HTML text with source metadata.
Priority: Document extraction is already closed; first-class web retrieval is a distinct high-value input channel for research and current-information tasks.
