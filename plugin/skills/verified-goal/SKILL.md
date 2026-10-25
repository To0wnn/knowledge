---
name: verified-goal
description: Turn a longer task into a goal the agent keeps working on until a check proves it is done - a condition a command can prove, no cut-off while it makes progress, a stop when it stalls. Use when the user wants work done without babysitting, says "keep going until ...", or a task needs many turns; also for a recurring unattended job.
---

# Work to a verified goal

Stop on a check that a command runs, never on your own claim.

1. **Condition** = the project's check command (AGENTS.md "## Commands") plus the one end-to-end test of this task,
   both exit 0, with their output printed in this session. The output alone must prove it ("`./deploy.sh` exits 0
   and `curl .../health` returns ok"). No subjective conditions ("cleaner code"): they never converge.
2. **Stop on lack of progress, not on a count.** Progress = something the check can see: one more passing test, item
   or file; fewer failures. Add to the condition: "..., or stop when 3 turns in a row made no progress". No turn or
   time limit while it progresses: work that is on track is never cut off. That is safe only when the work is a
   finite list (items, tests, files), so each step of progress brings the end closer; open-ended work gets a list
   first. `/goal` has no limit of its own in any of the agents.
3. **Start** with `/goal <condition>`: Claude Code, Codex and agy all have it. Claude's goal is judged by a small model
   that only reads the conversation; Codex and agy judge their own goal. So print the real check output every time.
4. **While looping:** one small step per turn and the check after each. Never edit, skip or delete a test or a check to
   make it pass (the usual way loops cheat). Commit after each passing step when the project uses git.
5. **Stuck** (no progress for 3 turns): stop, write the blocker as an open item in the
   project's memo (`open tot <date> [<agent>, <ID>]: ...`) and tell the user. Never report "almost done" as done.
6. Finish with skill verify-done.

## Unattended (no session open)

Agent schedules live only as long as their session (Claude's `/loop` tasks expire after 7 days, agy's schedule lives in
its conversation, Codex CLI has none). A recurring job is a systemd timer or cron entry on the host that fails loudly
(Uptime Kuma push or a chat message on a non-zero exit). The same loop works for every agent: fresh passes, each
a script measuring progress from the files (never from the agent's answer); it goes on as long as passes make
progress, without a total time limit, and stops when done or after 2 passes without progress. `timeout` per pass only
ends a hung pass; what that pass finished still counts.

```sh
idle=0
while ! ./check; do
  before=$(./progress)           # a number the files show: passing items, checked memos ...
  timeout 30m <agent run> "<task>. The check is ./check; run it and show its output."
  [ "$(./progress)" -gt "$before" ] && idle=0 || idle=$((idle + 1))
  [ "$idle" -ge 2 ] && { echo "no progress"; exit 1; }
done
```

In the systemd unit: `TimeoutStartSec=infinity` (the loop ends by itself), never a timeout that cuts a progressing run.

A worked example: `watcher/review-loop.py` in the knowledge repo (weekly memo check).

`<agent run>` per agent:
- Claude Code: `claude -p --max-turns 40 --allowedTools "<tools it needs>"` (without a permission mode `claude -p`
  refuses other tools silently and still exits 0: read `permission_denials` in `--output-format json`). `claude -p
  "/goal ..."` also loops by itself.
- agy: `agy -p` (`agy -p "/goal ..."` loops by itself; approvals it cannot ask for are refused silently).
- Codex: `codex exec` (it does not loop on `/goal`; the shell loop above does).
