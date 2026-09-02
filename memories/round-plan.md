Limitation: a local HTML/HTM attachment under materials cannot be inspected as readable evidence; `fetch_web_page` only helps when the page has an HTTP(S) URL.
Change: extend the existing bounded document inspector with a standard-library HTML visible-text and link extractor, using the same evidence shape as fetched pages.
Evidence: the ledger records one prior HTML-inspection candidate whose direct executable capability test passed, while the current source still lacks the branch; this closes a common attachment workflow gap without a dependency or separate tool surface.
