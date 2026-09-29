You are reviewing a change. `SPEC.md` says what the code must do, `before.py` is the code
before the change and `change.diff` is the change. Decide whether the code after the change
meets the spec. Don't modify the other files.

Write your verdict to `REVIEW.json` as `{"verdict": "passed" | "failed", "reason": "..."}`:
`failed` if the changed code violates the spec in any case, `passed` if it meets it. Give
the concrete input that fails, if any. Then reply with one line.
