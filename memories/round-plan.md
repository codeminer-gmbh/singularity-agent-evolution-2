Gap: Archive extraction capped expanded bytes but accepted highly compressed ZIP or compressed TAR payloads that can waste disproportionate CPU and disk work.
Change: Enforce a 1,000:1 expansion-ratio limit from ZIP member metadata and from bytes consumed by the outer TAR stream during extraction.
Priority: This closes the archive-bomb resource-exhaustion gap noted by the lineage while preserving normal ZIP/TAR extraction and its existing size/member limits.
