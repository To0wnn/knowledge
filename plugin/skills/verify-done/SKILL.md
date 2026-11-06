---
name: verify-done
description: Before saying a task is finished, fixed or working - run the project's check and the end-to-end test, show the output, and say plainly what was not tested. Use at the end of every code change.
---

# Verify before "done"

No "done", "fixed" or "works" without fresh evidence from this session.

1. In a **worktree or feature branch**: bring in main (`git fetch origin main && git merge origin/main`) before running the check, so you verify against the latest code, not a stale base.
2. Run the **check command** from the project's AGENTS.md ("## Commands"). Look at its exit code, not at a piped tail.
3. Run the **one test that proves the goal** end to end (the request, the page, the command the user will run).
4. Code that runs on **another OS or device**: test it there (CI runner, the real device), or say it was not.
   A green CI proves only what its tests exercise: check that a test actually runs the new code path; if none does,
   add that check to the CI test in the same change instead of listing the platform as "not tested".
5. Report in a few lines: what passed (with the relevant output), what was **not** tested and why, and any risk left.
6. Something failed: say so with the output; do not soften it, do not claim partial success as success.

A task that takes many turns: set the check as the goal up front (skill verified-goal), so the work stops only when it passes.
