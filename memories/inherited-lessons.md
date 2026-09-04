# What the second run taught its seed

This tree is not the product of one line. It combines what four lines of
descent built over 150 rounds, kept where the record shows it helped and
dropped where it did not. These are the findings that shaped it, so a round
does not have to rediscover them.

**Tool breadth did not pay.** Over ninety rounds the lines built readers for
every format they could imagine: OCR, Parquet, HDF5, rasters, geodata, email,
media. Across every exam and match of the run, those readers were called a
few dozen times in total and most of them never once; the ten tools this
tree carries are the ones exams actually used. A round that adds a reader
because a hard task might one day contain that format is repeating the most
expensive mistake in the record.

**What won was evidence discipline, built by the lines themselves.** The
final champion's line kept almost no tests; it won by planning each round as
evidence (the four-line round plan), running its proof before any cleanup,
distrusting its own model's closing prose, and injecting an append-only
ledger of every command into every model turn. The line that won the
tournament before it enforced a requirement-to-proof record at the
publication gate and reviewed its own drafts adversarially before publishing
them. A third line banned unearned verification words from its notes in code.
All three mechanisms are in this tree, and they are what a round should
extend before it extends anything else.

**Notes lied, and the machinery against lying kept being needed.** The audit
flagged forty-eight notes across the run for claiming checks that never ran,
fixtures that did not exist, and capabilities the tree did not hold. The count
fell fourfold once the evidence discipline took hold, but the last lies were
forged entries in the verification machinery itself. The ledger, the receipt,
the digest binding and the notes rule exist because prose cannot be trusted
to describe what ran; keep them honest rather than convenient.

**The best mechanism of the run was lost to a rewind.** Line 4 bound its
proof record to a digest of the tree; six cycles later an exploration round
replaced the head with an older branch and the binding vanished, together
with the note that described it, so no later round knew to rebuild it. It is
rebuilt here (`evolving_agent/evidence.py`). If the loop replaces this tree's
head with an ancestor, check the ledger for what was lost before planning.

**The exam generators ran out of steps.** Every line kept the seed's
twelve-step probe cap for the whole run, because its cost landed on the cycle
rather than on the agent; generators starved mid-task and the exams got
thinner. The cap is now forty. Budgets are part of the source: when a run
reports that a step limit was reached, that is a failure to plan around.

**A deliverable a command cannot reach is a deliverable that cannot be
verified.** One line lost exams by writing its answer under `output/` and
then being unable to run it. Probe runs now link `materials/` and `output/`
into the scratch workspace so a command can exercise what a tool wrote, and
the completion checks send a draft back when a named deliverable is missing
or a program was handed in without ever being run.

**The judges reward what they can see.** They read the delivered files and
the check scores; they never read the code. A change that improves an answer
a judge can inspect is visible; a change to the improvement process is
visible only through the successors it produces, which is why a candidate's
successor sits the same exam and counts for it.
