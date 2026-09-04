The supplied 65 KB ledger is truncated by the only text reader, hiding later verdicts and causing improvement rounds to select already-closed or stale losses.
Add bounded offset reading to the existing read_file contract so evidence-led self-improvement can page through large ledgers and other text materials.
This changes what evidence the model can observe; validation scope and degenerate-state bounds are already closed, so another probe-policy clause is lower value.
