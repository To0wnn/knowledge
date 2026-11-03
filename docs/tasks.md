# Tasks: hand-overs, leases and workers

Moved from the README; [back to the README](../README.md). How one agent hands work to another, how a task is started on exactly one machine, and how workers run it.
The companion that starts workers is described in [companion.md](companion.md); chats in [chats.md](chats.md).

## Hand-overs during a session

- **Hand-overs during a session:** before a prompt (at most every two minutes) Claude Code, Codex and agy ask
  `/tasks` for the tasks handed to them; a task added after the session started is told to the agent once.
  The same per-turn call lets the server steer every session, also a resumed one that ran no start hook: notices
  (such as docs moved into an archive folder) go to the first session of the device that asks, offered again after an
  hour when that session had no further turn, and one session is asked to run a full docs mirror sync in the
  background when the device's last one is older than `docs_sync_minutes` (admin page, Settings).

## Task lines and leases

- **Task lines:** a hand-over is one line in a memo,
  `- open until <date> [<Owner>, <ID>] from <Requester>, start <now | after <ID> | <date> | after approval>: <what>`
  (`open tot` also works). A new line without `from`/`start` is refused at write time; older lines keep working as
  "start now" and show as legacy. The server records who added each line; a task starts by itself only when its start
  condition is met and its writer is trusted (per project on the admin page; by default the project's owner and their
  tokens, admins and the admins' own devices), otherwise the owner or an admin approves it in the Tasks tab. Only tasks
  that may start reach an agent as "start now" (`/tasks`, the wake, `GET /tasks/ready`); the others are listed with the
  reason. One task is started on one machine only: the first device to take it holds a 30-minute lease
  (`state/task_leases.json`). `POST /tasks/woken` takes the lease (200 `{"lease"}`, or 409 `{"held_by"}` when another
  device or another session holds it), `POST /tasks/release` gives it back (holder device only; a failed start or
  delivery), `GET /tasks/ready` leaves out tasks another device holds, and the per-turn `/tasks` takes the lease for
  the asking session when it first hears the task: every other session, on another device or on the same one, hears
  "<ID> is already being handled by <device | another session on this pc>; do not start it". Later turns (the plugin
  sends the ids it told the session before as `seen`) renew the session's own lease only within 30 minutes of the
  tell and never take a free one again, so a session that heard a task and did other work lets it go. Only a token
  that may write the project's memos takes leases. A worker the companion starts gets a nonce
  (`KNOWLEDGE_START_NONCE`) and only the session presenting it takes its task (after 2 minutes without it, any session
  of that machine may); the companion renews the lease while the worker runs. A lease ends when it runs out or the line gets its claim marker, which also makes the task not
  startable. `[<Owner>@<device>, <ID>]` pins a task to one device (the token's name, any case; token names are unique
  ignoring case, and a new line pinned to an unknown device is refused); others see it as reserved. A woken task nobody claims within 30 minutes
  shows as stalled and sends one Matrix message.

## Results handed back

- **Results handed back:** a line `- [<Requester>, <ID>-done]: <result>` is listed for its requester only, at
  session start ("Results handed back to you") and once per session at the next turn when it arrives later
  (`results` in the `/tasks` v2 answer): read it, act on it, then remove the line. The Tasks tab groups them as
  "Results waiting to be read by <requester>" with their age (a date in the line, else when the server saw it written,
  else the memo's history). Unread for 7 days: lint `stale_handback`, which session starts list too.

## Workers

- **Workers, not chats:** a task never goes into someone's chat. The companion (below) starts a **worker** per ready
  task: a fresh headless session of the task's agent (Claude Code `claude -p --output-format stream-json`, Codex
  `codex exec --json`, agy `agy -p`) in its own git worktree on branch `task/<ID>` (`~/.knowledge/worktrees/`), or in
  the project folder when that is no git repository. Without YOLO a worker may do what its task needs and no more: edit
  its own folder, use the knowledge tools, and run `git status/add/commit`, plus `git diff`/`git log` in fixed forms for Claude only
  (`--output=<file>` would write anywhere, .git/config included; agy rules are prefixes, so agy gets none) (no push, no other command; a
  headless run cannot ask, so the rest is refused). Each agent the native way: Claude `--permission-mode acceptEdits`
  plus `--allowedTools`; agy `--mode accept-edits` plus allow rules for that session only (`--settings`, never saved);
  Codex `-s workspace-write` with only the worktree's git folder and the repository's objects, refs and logs writable
  (not `.git/config` or hooks; the sandbox has no network) and the plugin's knowledge tools approved with
  `-c plugins.knowledge@knowledge.mcp_servers.knowledge.default_tools_approval_mode=approve`. A worker that ends with exit 0
  but left its task open (not claimed, not handed back) counts as failed: an error in the activity log and the
  stalled lane, plus the server's idle notice. Its prompt holds the task line and the rules: claim it, commit
  on the branch, never merge or push, hand back with `[<requester>, <ID>-done]: <result>; branch <name>; done: ...;
  left: ...` (the server refuses a worker's hand-back without branch, done and left). Every worker is a new session; a
  hand-over `<ID>-cont` goes on in the same worktree and branch, and the branch, the memo and the last hand-over line
  are all it knows. Workers are off on a new pc ("Workers allowed" in the companion's settings), at most one per
  agent unless its maximum says more, one while its usage limit is unknown, none while a Wolf game session runs (its
  API socket, when readable; Sunshine is not detected). On macOS the companion's python may not read ~/Documents, ~/Desktop or
  ~/Downloads (privacy consent): a task linked there waits with that reason until it gets Full Disk Access. A worker that ended without finishing its task is not started
  again for 6 hours unless asked. Its claim marker `(claimed <agent> worker, <date>)` holds only while its companion
  renews the task's lease, so such a task shows as startable again (and gets the idle notice) instead of staying stuck. The companion knows a worker by its pid and that process's start time,
  so after a reboot a recorded worker counts as ended and a stop never hits a reused pid. A worker records the branch its
  worktree was made from (`base_branch`). When it ended and its result line `[<requester>, <ID>-done]` names its branch,
  the Inbox shows a **Review** card under Needs you (`kind: review`): commits ahead of that branch, changed files, the
  newest commit, the worker's done/left and the clause of its done text about tests (the server runs nothing).
  **Merge** is the machine action `merge` (owner only): in the main checkout `git merge --no-ff -m "Merge task/<ID>"`
  without hooks, fsmonitor or signing, only when that checkout is on that branch, has no uncommitted changes and no
  merge, cherry-pick, revert or rebase of its own going on, and no file the branch adds is already on disk (an ignored
  `.env` would be replaced). Never when the branch changes `.gitattributes`, `.gitmodules` or `.lfsconfig` or
  `.git/config` changed since the worker started (hash recorded at start): the card says why, merge by hand. A conflict
  is aborted and reported as "conflict: merge by hand"; never pushed. **Reject** (`POST /ui/api/review/reject {key, reason,
  send_back}`) keeps the branch and takes the card off with its reason; with "send back" and a reason the result line
  becomes `- open until <date> [<agent>@<that pc's token>, <ID>-fix] from <user>, start now: ...`, which a worker on
  that pc continues on the same branch (`<ID>-fix` keeps `start after <ID>` waiting, like `-cont`). Once the requester merged the branch into main, master or that base branch, the companion removes the
  worktree; unmerged a week after the worker ended: lint `worker_unmerged`. While an online pc runs an agent's workers
  for the project (workers allowed, a maximum above 0, the agent installed, a folder linked), that agent's interactive
  sessions (`chat=1`) take no lease: they hear a new task once as "runs in workers" and where one runs; when the user
  says `pak <ID>` (or `take <ID>`) the plugin takes that task for the chat (agy, whose hooks never see the user's words,
  runs `agent_hooks.py take agy <ID>`), and no worker starts it. With no such pc, that agent's chats take and start
  its tasks themselves, as before 0.15. The Tasks tab says per task where it runs or why it waits there; a task that
  may start but that nobody took for 30 minutes (`KNOWLEDGE_TASK_IDLE`) gives one Matrix notice. A knowledge write
  asks the local companion to look at once.

## Follow, steer and take over a worker

A worker is a chat nobody types in. Its row in Tasks, its board card and its Flow panel have **Follow**, which opens
its transcript in the chat view (`#/chats/worker/<project>/<ID>`, also a link for Matrix or the Inbox) with a banner
"Worker for <ID> on <pc>" and its actions (Stop, Restart, Review, Take over, Open chat). Only the machine's owner sees
it (`GET /ui/api/worker?machine=&worker=<project>/<ID>&after=<n>`), as for chats.

- **Pull only, and only while someone watches.** Polling a running worker's transcript follows it for 30 s
  (`KNOWLEDGE_WORKER_FOLLOW_SECONDS`; `peek=1` reads without following). The companion's fast chat poll gets the
  followed workers (`follow` in `GET /machine/chats`), reads the new complete lines of the worker's own log and turns
  them into chat events with the chats' own parsers (Claude stream-json, Codex `--json`; agy `-p` prints plain text,
  its lines are its answer): text, one line per tool call or refused call, info. It posts them on
  `POST /machine/chats/worker` (masked on the pc, known secrets masked again on the server). A worker nobody follows
  sends nothing while it runs; when it ends, its companion sends its tail once (the last 2000 lines of its log, within
  4 MB, as at most 2000 events). Follow on an ended worker whose end the server does not have (it ended before the
  companion sent tails, or that send failed) asks its companion for it the same way, once. Only the token that
  machine's companion reports with may write. One transcript per worker under `chats/<machine>/workers/`, at most
  2000 events and 400 kB (the oldest go), 40 per machine, removed with the worker's record (30 days) or the machine.
  A new run of the same task starts a new transcript.
- **Steer** (running worker, owner only): `POST /ui/api/worker/steer {machine, worker, text}` adds a user line and
  hands the message to the companion with its next poll (`steer`). The companion leaves it as a note next to the
  worker's stop note; the plugin's PostToolUse hook (`watch.py`) gives it to the agent after its next tool call, the
  same way the 90% hand-over reaches a worker: Claude Code and Codex as `additionalContext`, agy at its next
  PostInvocation (`injectSteps`). So the worker carries on in its own session, worktree, lease, nonce, permissions and
  YOLO setting; nothing widens. The transcript says "Delivered" once the hook took it; a worker that ends without
  another tool call never reads it, and its transcript says so.
- **Take over** (machine action `takeover`, owner only): the companion stops the worker (its lease kept, as Stop),
  finds the agent's own session id (Claude's `init` event, Codex's `thread.started`, else the id the watch hook noted:
  agy), and asks `POST /machine/chats/adopt`. The server makes an ordinary chat on that pc in the worker's worktree,
  with the transcript so far, that resumes that session (`claude -p --resume`, Codex `thread/resume`, agy
  `session/load`), with the chats' approval rules. The task line gets `(claimed <owner> via chat, <date>)` instead of
  the worker's claim, so no worker starts it again; it stays the owner's until the chat hands it back (removes the
  line) or the owner cancels or assigns it (allowed for a line only a take-over chat holds).

## Usage limit at 90%

- **Usage limit at 90%:** the companion reads each agent's usage: Claude from its workers' own `rate_limit_event`
  (stream-json, until that window resets) or an interactive Claude's status line under 15 minutes old, whichever is higher; Codex from its newest rollout of the
  last 15 minutes; agy from a registered agy's language server; else "unknown". From 90% no worker of that agent
  starts and the task waits ("waiting for limit", one Matrix notice); a running worker is told after its next tool call
  to commit, write the hand-over line `[<agent with room>, <ID>-cont] from <original requester> ... done: ...; left: ...` (the result still goes back to whoever asked first) and end, and is stopped
  after two minutes. A result handed back never starts anything by itself.

## Mission map

- **Mission map:** Tasks > Flow (`#/tasks?view=flow`) shows the task lines between agents as a graph, always laid out by
  itself (no dragging boxes around): one lane per owner (Claude, Codex, Gemini/agy, people), tasks as boxes coloured by
  status (open, waiting on a task or date, needs approval, claimed, running in a worker, done in the last 24 h,
  stalled), left to right by "start after <ID>", with `-cont` continuations and `-done` results in their own line styles; hand-overs
  (requester to owner) show for the task you point at or pick. Tasks that wait on nothing stack in the first column. Pan and zoom with wheel, drag or pinch, `F` fits; a task opens a panel
  with its full line, where it runs, Approve and its memo. "Show all" adds older results; a project filter narrows it.
  A phone shows the chains per lane as a list of full-width cards. Data: `GET /ui/api/mission?project=&all=1` (same visibility as the
  Tasks tab); the layout is a small layered (Sugiyama-style) pass in `static/mission.js`.
