---
name: review-diff
description: Review a larger change in a fresh context before calling it finished - only the diff since a given commit, focused on security, data loss and real bugs, every finding verified. Use after a feature, a refactor or a risky fix.
effort: high
---

# Review a diff

1. Pick the range: `git diff <base>..HEAD` (everything already reviewed stays out).
2. Hand it to a fresh reviewer (a subagent or a new session) with: what the change is for, the order of concern
   (security and access, data loss, correctness with a concrete failing input, other OS), and "verify every finding;
   drop what you cannot show".
3. Check each finding yourself before fixing: read the code path, reproduce when cheap. A finding that does not hold:
   say why.
4. Fix the real ones, each with a test that fails without the fix; run the check.
5. Report: findings, what was fixed, what was judged not a bug, what was not covered.
