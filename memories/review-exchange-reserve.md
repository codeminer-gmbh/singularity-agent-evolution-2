# Review exchange reserve

Mandatory completion stages have an exchange reserve separate from ordinary `max_steps`: one draft exchange plus four exchanges per review stage, still bounded by the shared wall-clock deadline. This lets a critic inspect evidence and a finalizer write and re-read a repaired deliverable even when ordinary work used its entire allowance. `test_completion_review.py` proves the six-exchange public probe path with `max_steps=1`.
