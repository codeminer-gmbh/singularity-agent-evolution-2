Gap: the agent cannot inspect raw EML/MBOX mail evidence, including encoded headers, body text, and attachment metadata.
Change: add a bounded standard-library `inspect_email` workspace tool, restricted to workspace/materials/output paths.
Why: email evidence is a common hard-task input and this unclosed gap adds a file type distinct from the promoted archive, document, and spreadsheet readers.
