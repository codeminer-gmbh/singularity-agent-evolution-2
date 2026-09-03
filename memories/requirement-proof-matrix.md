# Requirement-to-proof matrix

| Requirement | Input | Expected | Public interaction |
|---|---|---|---|
| Preserve exact parsing language | Probe task allows a string integer only when stripped text is ASCII decimal; counterexample `+7` | Guidance requires an explicit accepted/rejected language decision and forbids silently accepting `+7` | `probe_instructions()` text inspection, then a simulated public probe session records a boundary test |
| Keep proof verdict-directed | The same task has an exact signed-string boundary but no concurrency or performance contract | Guidance asks for that boundary and does not demand unrelated lifecycle/performance cases | `probe_instructions()` text inspection |
| Exercise the delivered path | A coding task names `output/solution.py` and supplies executable checks | Guidance requires running the exact counterexample against the deliverable/public entry point, not only syntax or a generic suite | `probe_instructions()` text inspection and prompt-level session test |

Implementation decision: probe guidance will require translating only explicit task semantics into decisions; for parsing it will name the exact accepted language and rejected boundary forms. In the ledger case, stripped `[0-9]+` accepts `7` and rejects `+7`, `-7`, and non-ASCII digit forms. No precedence, ordering, lifecycle, or complexity rule is added unless the probe task states one.
