Gap: the ledger's repeated audit finding is unsupported verification claims, while existing session observations are discarded before a successor can inspect them.
Change: persist a compact, machine-captured receipt of every improvement-run `run_command` outcome and teach the completion protocol to distinguish that receipt from relevance proof.
Why: this closes the documented evidence-loss failure directly, rather than adding an unrelated input-format capability.
