# Iteration memories

Store concise, durable lessons for future iterations here. Record information
that is not obvious from the code, such as a gap you found, a failed approach
and why it failed, a design decision, and what the exam rewarded. Describe only
code that is actually in this tree: every note added or changed is audited by
a judge against the diff the version shipped, and a note found inaccurate is
named as such in every later round's ledger. A note may say that something was
tested or verified only when it names, in a code span, a test or fixture that
ships in the tree; the publication gate refuses a new note that claims a result
the tree cannot show. One note per change, under a short date-free name, at
most a few kilobytes. Do not store credentials or conversation transcripts.

What a rejected version writes here is not inherited by the tree: it reaches
later rounds through the ledger the orchestrator hands each improvement run
under `materials/ledger.md`.

`round-plan.md` is the evidence plan an improvement run writes before it
touches code, and is rewritten by every run. `verification.json` is the
round's proof record, bound to the tree by the digest that
`python -m evolving_agent.evidence` prints.
