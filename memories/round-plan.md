Limitation: exact one-snippet editing still forces several risky tool calls for a normal multi-hunk code change; a later mismatch can leave an incomplete source edit.
Change: add a bounded, atomic batch exact-replacement tool that validates every hunk against one original UTF-8 file before writing it once.
Evidence: the existing `replace_in_file` shows incremental editing is the selected end-to-end gap, while the ledger's published tool inventories lack any multi-hunk transactional editor and mostly record already-explored attachment formats.
