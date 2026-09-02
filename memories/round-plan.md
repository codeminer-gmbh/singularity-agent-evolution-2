Gap: ledger's dominant recurring rejection is false/unverified evidence; existing tool-call counts cannot show which command or result was actually observed.
Change: retain bounded structured tool-call receipts and persist them beside the required count record so later reviewers can inspect concrete observed evidence.
Acceptance: a fake session records successful, malformed-argument, and tool-error receipts, and the output writer emits inspectable JSON receipts.
