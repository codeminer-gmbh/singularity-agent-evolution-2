Cannot do: prevent a newly written verification receipt from being reused after its claimed candidate's source changes.
Change: bind verification.json to a deterministic digest of every candidate file excluding the receipt itself, and require that binding at publication.
Why this gap: the ledger's "freshness only" failure shows byte freshness admits false proof; provenance binding directly distinguishes a receipt for one tree from a receipt for another.
