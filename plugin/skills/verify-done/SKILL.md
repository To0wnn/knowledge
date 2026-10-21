---
name: verify-done
description: Before saying a task is finished, fixed or working - run the project's check and the end-to-end test, show the output, and say plainly what was not tested. Use at the end of every code change.
---

# Verify before "done"

No "done", "fixed" or "works" without fresh evidence from this session.

1. Run the **check command** from the project's AGENTS.md ("## Commands"). Look at its exit code, not at a piped tail.
2. Run the **one test that proves the goal** end to end (the request, the page, the command the user will run).
3. Code that runs on **another OS or device**: test it there (CI runner, the real device), or say it was not.
4. Report in a few lines: what passed (with the relevant output), what was **not** tested and why, and any risk left.
5. Something failed: say so with the output; do not soften it, do not claim partial success as success.
