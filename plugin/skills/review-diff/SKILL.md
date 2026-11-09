---
name: review-diff
description: Review a larger change in a fresh context before calling it finished - only the diff since a given commit, focused on security, data loss and real bugs, every finding verified. Use after a feature, a refactor or a risky fix.
effort: high
---

# Review a diff

1. Pick the range: `git diff <base>..HEAD` (everything already reviewed stays out).
2. Hand it to a fresh reviewer (a subagent or a new session) with: what the change is for (the request, not your own
   verdict on the diff), the order of concern (security and access, data loss, correctness with a concrete failing
   input, other OS), and "verify every finding; drop what you cannot show". Also ask:
   - Does it do what was asked: anything missing, anything done that was not asked, anything that looks done but is
     wrong?
   - Does it loosen a check to get green: a lowered threshold or budget, a skipped or deleted test, removed
     assertions, a silenced checker (`# noqa`, `eslint-disable`, `@ts-ignore`), an empty `catch`, a stub or TODO
     where the code should be? Tightening a bar may be silent; loosening it must be stated and justified.
3. Check each finding yourself before fixing: read the code path, reproduce when cheap. Dismiss a finding only with
   evidence the reviewer could check (file:line, a test, a Decisions bullet); "it was intended" is not evidence.
   Without such evidence, fix it or put it to the user.
4. Fix the real ones, each with a test that fails without the fix; run the check.
5. Report: findings, what was fixed, what was judged not a bug, what was not covered.
