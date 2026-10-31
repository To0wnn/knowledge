---
name: plan-change
description: Plan a change that touches several files, is unclear, or is risky, before writing code - files involved, what stays out of scope, how it is verified and how it is rolled back. Skip it when the whole diff fits in one sentence.
effort: high
---

# Plan a change

1. Read the code involved, the project's AGENTS.md and the "## Decisions" that concern it. Search the knowledge base for
   earlier decisions or pitfalls on the subject.
2. Write the plan short, in the conversation (not a new file unless the user wants one):
   - **Goal**: one sentence, from the user's side.
   - **Files**: what changes where, including tests, docs and config the change must reach.
   - **Out of scope**: what you deliberately leave alone.
   - **Verify**: the check command plus the one end-to-end test that proves the goal (on the OS it runs on).
   - **Measure** (when the change should improve something): the number before, and how you get it after.
   - **Roll back**: the backup or command that undoes it.
   - **Slices**: more than ~3 independent parts, or more than about an hour of work: cut it into slices that each
     ship on their own (tests, release), the one the user notices first going first. A wish that comes up while a
     slice is being built becomes the next slice, not extra work for the running one.
3. Ask only about a real choice (one recommendation with its reason); otherwise go.
4. Build in small steps; after each, run the check.
