---
name: debug-root-cause
description: Find and fix the real cause of a bug, failure or wrong output instead of the symptom - reproduce, prove, fix once, check the pattern elsewhere, record the pitfall. Use for any bug report, failing test or unexpected behaviour.
effort: high
---

# Debug to the root cause

1. **Reproduce** it, with the exact input and the exact error. Can't reproduce it: gather evidence (logs, versions,
   state) before guessing.
2. **Make it a failing test** when possible (or a script that shows it). It must fail now.
3. **Find the cause**: follow the data from input to wrong output; change one thing at a time; read the code, not
   only the error. A cause is proven when you can say why it fails and the test confirms it.
4. **Fix the cause once**, in the shared code, not at every caller. No silencing (broad try/except, retries, skipping
   the test).
5. **Verify**: the failing test passes, the project's check passes; show the output.
6. **Look wider**: one cheap check whether the same pattern exists elsewhere in the project.
7. **Record**: the pitfall in the project's memo or doc when it can come back; explain the cause in plain words to the
   user.
