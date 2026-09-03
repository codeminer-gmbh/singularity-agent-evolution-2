# Final evidence checkpoint

A successful `write_file`, `delete_path`, or `extract_archive` now arms a session checkpoint: the first attempted final answer is retained in context and challenged to compare the original contract with observed, behavior-specific proof. A later mutation rearms it, while a failed mutation does not. The checkpoint permits an honest unverified final on the next turn, avoiding loops while preventing immediate evidence-free completion.
