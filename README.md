# knowledge

[![LongMemEval-S R@5](https://img.shields.io/badge/LongMemEval--S_R%405-98.8%25-brightgreen)](#search-quality)
[![Real questions, top 5](https://img.shields.io/badge/real_questions_top_5-88.9%25-brightgreen)](#search-quality)
[![Clients CI](https://github.com/To0wnn/knowledge/actions/workflows/clients.yml/badge.svg)](https://github.com/To0wnn/knowledge/actions/workflows/clients.yml)
[![Release](https://img.shields.io/github/v/release/To0wnn/knowledge)](https://github.com/To0wnn/knowledge/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**One shared memory for your AI coding agents, on your own server.** Claude Code, Codex and Antigravity (agy) on every
device get the same live docs and notes over MCP, plus a web UI with a graph of how the notes connect.

- **Finds the right note.** 98.8% R@5 on LongMemEval-S (500 questions), with no LLM in the loop and nothing tuned on
  the test; 88.9% top-5 on real questions over Dutch notes. Multilingual, and made for prose as much as code.
- **Stays tidy as it grows.** House rules, lint, and a local reranker that tells the agent right after it writes a
  near-duplicate, so agent memory does not pile up into contradictions.
- **Shared, not per machine.** One server for all devices and agents, per-device tokens, per-project guests.
- **Private.** Plain Markdown on your disk; embeddings and reranking run locally, nothing leaves the machine.

Notes are plain Markdown files on disk. The server indexes them, serves them to agents with a per-device token, and
checks them against a few house rules, so a growing pile of agent memory stays findable and tidy.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/map-dark.png">
  <img alt="The web UI map: notes grouped into topics, links between them" src="demo/map-light.png">
</picture>

<sub>Made-up demo notes from <code>demo/</code>; regenerate with <code>uv run --project server --with playwright python demo/screenshot.py</code>.</sub>

## What it does

- **MCP tools for agents:** search (regex and semantic), read, list, related notes, write and edit (with conflict
  checks), new memo, delete (kept as a copy), history, lint, and ingest of a web page, PDF or text as a raw source.
  Semantic hits show the file size and the newest "(checked <date>)" mark, so an agent can tell which hit to read.
- **Session start context:** each agent gets the shared instructions, the house rules and the memory index of the
  project it works in, plus open items whose date has passed, hand-over tasks between agents and the project's
  fixable lint findings (at most 12), which the agent clears once the user's request is done.
- **Hand-overs during a session:** before a prompt (at most every two minutes) Claude Code, Codex and agy ask
  `/tasks` for the tasks handed to them; a task added after the session started is told to the agent once.
  The same per-turn call lets the server steer every session, also a resumed one that ran no start hook: notices
  (such as docs moved into an archive folder) go to the first session of the device that asks, offered again after an
  hour when that session had no further turn, and one session is asked to run a full docs mirror sync in the
  background when the device's last one is older than `docs_sync_minutes` (admin page, Settings).
- **Task lines:** a hand-over is one line in a memo,
  `- open until <date> [<Owner>, <ID>] from <Requester>, start <now | after <ID> | <date> | after approval>: <what>`
  (`open tot` also works). A new line without `from`/`start` is refused at write time; older lines keep working as
  "start now" and show as legacy. The server records who added each line; a task starts by itself only when its start
  condition is met and its writer is trusted (per project on the admin page; by default the project's owner and their
  tokens, admins and tokens an admin made), otherwise the owner or an admin approves it in the Tasks tab. Only tasks
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
- **Results handed back:** a line `- [<Requester>, <ID>-done]: <result>` is listed for its requester only, at
  session start ("Results handed back to you") and once per session at the next turn when it arrives later
  (`results` in the `/tasks` v2 answer): read it, act on it, then remove the line. The Tasks tab groups them as
  "Results waiting to be read by <requester>" with their age (a date in the line, else when the server saw it written,
  else the memo's history). Unread for 7 days: lint `stale_handback`, which session starts list too.
- **Hook failures:** when one of the plugin's own hooks crashes or exits with an error, the device keeps it in
  `~/.knowledge/hook-errors.jsonl` and the per-turn hook reports new ones with `POST /status` (at most every two
  minutes, never holding up the turn); the tasks tab shows the last failure per device and agent.
- **Machines:** every request of plugin 0.15+ names its pc (`X-Knowledge-Machine`, a random id made once in
  `~/.knowledge/machine.json` with the host name as label; `KNOWLEDGE_HOME` moves `~/.knowledge`). Leases and wakes key
  on token + machine, so one token on two pcs is two holders. The server keeps per machine and agent the sessions of
  the last two hours (project, held task) and what the machine's companion reports; the Tasks tab lists them under
  "Machines": online or "offline since", per agent its plugin, a red YOLO pill, hooks trusted, usage limit (or
  "unknown"), last hook error, workers and recent sessions. A person sees the machines of their own tokens; an admin
  all. Plugins before 0.15 show as one "old plugin" row per token.
- **Workers, not chats:** a task never goes into someone's chat. The companion (below) starts a **worker** per ready
  task: a fresh headless session of the task's agent (Claude Code `claude -p --output-format stream-json`, Codex
  `codex exec --json`, agy `agy -p`; without YOLO they may still edit their own folder: `acceptEdits`,
  `workspace-write`, `accept-edits`) in its own git worktree on branch `task/<ID>` (`~/.knowledge/worktrees/`), or in
  the project folder when that is no git repository. Its prompt holds the task line and the rules: claim it, commit
  on the branch, never merge or push, hand back with `[<requester>, <ID>-done]: <result>; branch <name>; done: ...;
  left: ...` (the server refuses a worker's hand-back without branch, done and left). Every worker is a new session; a
  hand-over `<ID>-cont` goes on in the same worktree and branch, and the branch, the memo and the last hand-over line
  are all it knows. Workers are off on a new pc ("Workers allowed" in the companion's settings), at most one per
  agent unless its maximum says more, one while its usage limit is unknown, none while a Wolf game session runs (its
  API socket, when readable; Sunshine is not detected). On macOS the companion's python may not read ~/Documents, ~/Desktop or
  ~/Downloads (privacy consent): a task linked there waits with that reason until it gets Full Disk Access. A worker that ended without finishing its task is not started
  again for 6 hours unless asked. Its claim marker `(claimed <agent> worker, <date>)` holds only while its companion
  renews the task's lease, so such a task shows as startable again (and gets the idle notice) instead of staying stuck. The companion knows a worker by its pid and that process's start time,
  so after a reboot a recorded worker counts as ended and a stop never hits a reused pid. Once the requester merged the branch into main or master, the companion removes the
  worktree; unmerged a week after the worker ended: lint `worker_unmerged`. While an online pc runs an agent's workers
  for the project (workers allowed, a maximum above 0, the agent installed, a folder linked), that agent's interactive
  sessions (`chat=1`) take no lease: they hear a new task once as "runs in workers" and where one runs; when the user
  says `pak <ID>` (or `take <ID>`) the plugin takes that task for the chat (agy, whose hooks never see the user's words,
  runs `agent_hooks.py take agy <ID>`), and no worker starts it. With no such pc, that agent's chats take and start
  its tasks themselves, as before 0.15. The Tasks tab says per task where it runs or why it waits there; a task that
  may start but that nobody took for 30 minutes (`KNOWLEDGE_TASK_IDLE`) gives one Matrix notice. A knowledge write
  asks the local companion to look at once.
- **Usage limit at 90%:** the companion reads each agent's usage: Claude from its workers' own `rate_limit_event`
  (stream-json), else an interactive Claude's status line under 15 minutes old; Codex from its newest rollout of the
  last 15 minutes; agy from a registered agy's language server; else "unknown". From 90% no worker of that agent
  starts and the task waits ("waiting for limit", one Matrix notice); a running worker is told after its next tool call
  to commit, write the hand-over line `[<agent with room>, <ID>-cont] from <original requester> ... done: ...; left: ...` (the result still goes back to whoever asked first) and end, and is stopped
  after two minutes. A result handed back never starts anything by itself.
- **Remote control:** the owner of a machine steers it from the Machines list with fixed actions only: start a worker
  for a ready task, stop a worker, roll a YOLO worker back, trust hooks, update the plugin, YOLO on or off per agent
  (on needs the word YOLO typed in a dialog), workers allowed, max workers, link or unlink a folder (only folders the
  companion listed itself). The server queues them per machine (`POST /ui/api/machines/action`); the companion pulls
  them about every 10 seconds (`GET /machine/actions`), runs them with its own code and reports the result; an
  offline machine's action shows "waiting for machine". Every action is logged with who and when, and shows in the
  companion's activity log. There is no free-text command anywhere.
- **YOLO safety without blocking:** task text by a writer the project does not trust reaches agents and workers marked
  as data, not instructions. A watch hook after every tool flags what a YOLO session does that is risky (force push,
  `rm -rf` outside the project, `curl | sh`, reading key or token files, reading the vault) in the companion's activity
  log and once per session and kind as a Matrix notice; it never refuses. A YOLO worker gets a git checkpoint of its
  whole worktree per turn on `refs/knowledge/checkpoints/<ID>`; "roll back" puts it back as before its last turn and
  keeps the state before as `<ID>-undo`.
- **Companion (this pc):** `knowledge-agent.py serve` (or `agent_hooks.py serve`, alias `companion`) runs a small
  local service on `127.0.0.1:47321` only. Every minute it asks `GET /tasks/ready`, starts workers as above (it takes
  each task with `POST /tasks/woken` for its worker session first and gives it back when the start fails), keeps
  their leases while they run and reports the machine's state (`POST /machine/report`). On request it trusts the
  knowledge hooks the agents' own way (Codex: `codex app-server` `hooks/list` + `config/batchWrite`, again after every
  plugin update; Claude Code: `hasTrustDialogAccepted` in `~/.claude.json`; agy: `trustedWorkspaces`), only for folders
  with a `.knowledge-project`. Its Folders section lists the linked folders and candidates (git checkouts up to three
  levels under your home folder and folders agents worked in) and links one to a project the token may write by
  writing `.knowledge-project`, as the knowledge-init skill does; unlink removes only that file. A YOLO switch per
  agent writes its native "run without asking" settings and unchecking restores the previous values (agy's only while
  agy is closed); workers then get the agent's own flag too. Every changed file is first copied to
  `~/backups/knowledge-companion/<date>/` (last five days kept). Hook failures Codex and agy log themselves are reported
  with `POST /status` as `agent-log` (Codex: only failures of the knowledge hooks). Its page is at
  `http://127.0.0.1:47321/`; the web UI shows a "This pc" tab when the companion answers there. The local API answers
  only that page and the knowledge web UI's origin; every change except trusting the knowledge hooks, and the tasks
  and activity, also need the key in `~/.knowledge/companion/api-key` (readable by you only), which the menu entry
  passes to the page (`#key=`). YOLO and the worker settings change only on its own page (or remotely by the owner).
- **Usage limits:** each prompt checks how much of the agent's own usage limit is used and says it once per session
  and limit window at 50, 75 and 90%; from 95% a new subagent is refused (Claude Code `Agent`, Codex `spawn_agent`, agy
  `invoke_subagent`). The numbers are the agent's own: Codex's rollout, agy's language server (after its one-time
  `register`), and for Claude Code, which hands them only to its status line, a small tap that the plugin puts in front
  of your status line at session start (your line still shows; the old command is kept in
  `~/.knowledge/statusline-original.json`). Without numbers nothing is said or refused.
- **Secret guard:** an edit that writes a known secret value into a file is refused (Claude Code Edit/Write, Codex
  apply_patch, agy's file tools), where `token.py push-secrets` left the hash file (`secret set` and `secret rm` run it themselves; a value
  added in the Vaultwarden web UI needs a manual push). Only new text counts, so removing a secret is allowed.
- **Token use:** the session log also sends the tokens of each logged part (uncached input, cache writes, cache reads,
  output; Claude's per-block copies of one call counted once, Codex's running totals differenced). Lint reports
  `usage_outlier`: a session of the last week far above that agent's usual (modified z-score above 3.5 against the last
  month, from 10 sessions on).
- **Projects:** each project has a docs root and a memory root. A session picks its project from its working folder;
  a token can also be limited to one project (a guest). A new server has one project (`homelab`, docs in
  `data/docs`, memory in `data/memory`); the admin page makes more and gives a project the name the web UI shows.
- **Lint:** deterministic checks in ~80 ms: dead links, index lines without a file and the reverse, missing frontmatter
  or sections, oversized memos, overdue open items, probable secrets (never shown), raw sources not yet processed.
- **Web UI:** a home view per topic, full search, a note preview with connected notes, an editor, change history, a
  lint tab, a tasks tab (the task lines between agents with from whom and when they may start, waiting ones greyed
  with the reason, stalled wakes, an Approve button for the owner, results handed back, and the Machines list with
  workers, sessions, hook errors and remote actions; Done removes a line) and an admin page for tokens, people and projects. Light or dark, your pick.
- **Graph:** notes as nodes, links as lines, one island per topic. A line's thickness and pull follow how related two
  notes are (direct link, shared source, shared neighbours, same type); weak links between topics show on hover.
- **External skills:** an admin adds a skill folder from GitHub by its link; it is pinned to a commit, taken in only as
  plain text (no tools, hooks or load-time commands, a licence that allows copying) and shared like your own skills.
  A weekly check takes small updates in by itself and holds big or unsafe ones for the admin; local additions below
  `<!-- local -->` survive updates.
- **Archive:** folders such as `archief/`, `archive/`, `old/` or `history/` are skipped by search unless asked for.
- **Offline copy:** a device with a write token keeps a copy of the memory and syncs only differences; when the server
  is down, the agent still gets a cached start context.
- **Secrets:** known secret values are masked in every answer and refused in every write.
- **Duplicate check (optional):** a local reranker (Qwen3-Reranker-0.6B, int8 ONNX, CPU) judges memo pairs whose
  embeddings are close, in the background, and lint lists the pairs it calls the same subject (`possible_duplicate`).
  A written memo is judged within a minute, and the device that wrote it hears at its next turn which memo it
  duplicates (once per pair, with the other memo's description). Memos that link each other count as deliberately
  separate. Memo files written outside the MCP tools and reported by the watcher notify the device named in
  `KNOWLEDGE_WATCH_DEVICE`.
  Off unless `KNOWLEDGE_RERANK_MODEL` points to a model made with `server/export_reranker.sh`.
- **Private spans:** text between `<private>` and `</private>` is dropped from writes, edits and the session log
  before anything is stored.

## How it works

- **Server** (`server/`): Python, stateless streamable-HTTP MCP plus the web UI, in one Docker container. All text sits
  in memory, so search over a few thousand files takes tens of milliseconds. A watcher posts changed paths for instant
  updates; a stat-only rescan every 60 s is the safety net. Embeddings (for semantic search and related notes) run
  locally with ONNX; no data leaves the machine.
- **Clients** (`plugin/`): one folder that installs as a plugin in Claude Code, Codex and agy. It adds the MCP server,
  the session-start hook, a session log and a docs mirror for guest projects. Codex and agy reach the server through a
  small stdio relay, so the token never sits in a config file. `clients/knowledge-agent.py` sets everything up.
- **Tokens:** one long random token per device, kept in the OS keyring (Linux Secret Service, macOS Keychain, Windows
  Credential Manager). Tokens can be read-only, masked, limited to one project, or allowed to sync shared skills.
- **Web sign-in:** a password for one admin (`KNOWLEDGE_UI_PASSWORD`, a signed cookie for 30 days; at most five wrong
  tries a minute), or, for several people, a reverse proxy with forward auth (for example Authentik) that sets the user
  header and a shared secret (`KNOWLEDGE_PROXY_SECRET`).

## Search quality

Semantic search uses [EmbeddingGemma](https://huggingface.co/google/embeddinggemma-300m) (300M, runs on a CPU):
multilingual (100+ languages) and made for prose as much as code, so notes in Dutch, German or English, docs, decisions
and runbooks are found as well as code snippets. Measured with no LLM in the loop and nothing tuned on the test:

| Benchmark | Result |
|---|---|
| [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval), 500 questions, session R@5 | **98.8%** (R@10 99.4%, NDCG@10 0.945) |
| The maintainer's 45 real questions over Dutch notes, right note in the top 5 | 88.9% (top 1: 73%) |

LongMemEval per question type (R@5): knowledge update 100%, temporal reasoning 100%, multi-session 99.2%, user facts
98.6%, preferences 96.7%, assistant facts 94.6% (documents hold the user turns only).
These are retrieval numbers (is the right session among the first five), not answer accuracy. Re-run with
`tests/longmemeval.py` (data from the LongMemEval Hugging Face dataset). EmbeddingGemma 2 scored lower on both sets
(97.8% R@5; 64% top 1 on the real questions), so the server stays on the first version.

## Run the server

With Docker, all notes in `./data` next to `compose.yaml`:

```sh
git clone https://github.com/To0wnn/knowledge && cd knowledge
mkdir data && echo "KNOWLEDGE_UI_PASSWORD=<a long password>" > .env
docker compose up -d
```

Open `http://localhost:8766/ui`, sign in with the password and create a device token on the admin page. Notes live
in `data/memory` (memos with their `MEMORY.md` index), `data/docs` and `data/shared` (`CLAUDE.md`, house rules and
skills shared with every agent); more projects can be made on the admin page. Reached from other machines: add their
`host:port` to `KNOWLEDGE_ALLOWED_HOSTS` in `.env` (also when you change `KNOWLEDGE_PORT`), and put a reverse proxy
with HTTPS in front: over plain HTTP the password and tokens cross the network readable. The container runs as uid
1000; if `./data` belongs to another user, set `KNOWLEDGE_UID` and `KNOWLEDGE_GID` in `.env`. The image (`ghcr.io/to0wnn/knowledge`, about 1.5 GB with the embedding model) is built for every release;
it is also listed in the MCP registry as `io.github.To0wnn/knowledge`.

Settings in `.env` (all optional): `KNOWLEDGE_PUBLIC_URL` (the address people use, named in the agents' instructions),
`KNOWLEDGE_LANGUAGE` (`en` or `nl`: a memo's sections are `## Status` and `## Decisions`, or `## Stand` and
`## Keuzes`; both are always accepted), `TZ`. The house rules every agent gets are `default_house_rules.md` until you
put your own `house_rules.md` in `data/shared`.

Requirements: Docker and about 2 GB RAM (the embedding model is 1.2 GB; it ships in the image, nothing is fetched at
runtime).

The original installation runs differently (`server/compose.yaml`: notes shared over NFS from the machine the agents
run on, a zero-downtime switch on deploy, `KNOWLEDGE_BUILTIN_PROJECTS=homelab,anpitek,trade`): copy `local.env.example` to `local.env`, point the volumes at your folders
and run `./deploy.sh`, which runs the tests, copies `server/` to the Docker host, builds, switches and waits for
`/health`.

## Connect a client

The server's help page (`/ui/help`) has the exact steps per client and OS. In short:

```sh
curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh   # Linux, macOS
```

```powershell
irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1 | iex        # Windows
```

Both need git and Python 3.9 or newer (macOS's own python3 is 3.9). They keep the scripts in `~/knowledge` (a git checkout, also brought back after a rewritten
history) and run `clients/knowledge-agent.py setup`. It is the same line every time: the first time it finds Claude
Code, Codex and agy and asks y/n for each; after that it updates the agents that have the plugin, asks y/n only for an
agent found without it, and asks for a token only when the server refuses the stored one.

`setup`, `install` and `update` end by setting up the companion (again only updates; `install-companion` alone):
it starts at logon (Linux: systemd `--user` unit `knowledge-companion.service`; macOS: LaunchAgent
`io.github.knowledge.companion`; Windows: a per-user Scheduled Task "Knowledge companion" that runs `pythonw.exe`
directly, no PowerShell), a menu entry "Knowledge companion" opens its page in an app window (Edge or Chrome
`--app=`, else the default browser), and a first run trusts the knowledge hooks. On Windows the Start menu `.lnk` is
made with one visible `powershell -NoProfile -Command` line (the shell's COM object). `--dry-run` (or
`KNOWLEDGE_COMPANION_DRY_RUN=1`) lists the steps, also for another OS with `--os linux|darwin|windows`. Without a
user session to start it in (no systemd user bus over SSH, a container, WSL; macOS without a GUI login) the autostart
is skipped and the setup still succeeds; start it by hand with `knowledge-agent.py serve`. On Linux the unit has
`KillMode=process`, so a restart never ends the tmux sessions and `codex exec` runs the companion started.
`knowledge-agent.py uninstall-companion` first turns every YOLO switch off (the agents' own settings come back), then
removes the autostart and the menu entry (its settings in `~/.knowledge/companion` stay).

`install claude` (or `codex`, `agy`) sets up one agent without asking which ones. `install` asks for your server's
address (or set `KNOWLEDGE_URL`, e.g. `KNOWLEDGE_URL=http://localhost:8766`, for one run); the installer has no default, so a
token never goes to a server you did not name.
The address that answered is stored in `~/.config/knowledge/url` for the hooks of every agent and set as the Claude
plugin's "Knowledge server URL" option, so later sessions and `update` need nothing else.

It asks for the token once, checks it with the server, stores it in the keyring and installs the plugin. The plugin
keeps itself up to date in every agent, and says so once at session start: in Claude Code it turns on the marketplace's
auto-update (off under `/plugin` > Marketplaces); Codex and agy have no plugin auto-update, so there the plugin checks
GitHub at most once a day and updates itself with the agent's own CLI (the setup without a plugin: its git checkout is brought to
GitHub's main, also after a rewritten history when nothing in it is changed, then the setup runs again;
`knowledge-agent.py update` does the same by hand). Off: `KNOWLEDGE_AUTO_UPDATE=0`. A new version loads at the next start. Each session start
reports the plugin version: the admin page shows it per device and agent, and a device still behind the newest
version after two days hears how to update by hand (also listed in lint as `plugin_behind`).

Link a project folder to its project once: ask your agent (skill `knowledge-init`), which writes a file
`.knowledge-project` with the project name in the folder's root. Every session started there gets that project. A linked git worktree (Codex and agy make one per chat) counts as its main checkout: same project, docs mirrored from the main checkout only (the folder is the source: knowledge_write/edit/delete refuse the mirror, as the UI does, since the next sync would undo them), and the session is told it runs in a separate checkout. On Windows, agy needs a `python3` command on PATH (Python from the python.org install manager or the
Microsoft Store has one); without it the setup configures agy without the plugin. Codex and the agy hooks on Windows start Python through
`plugin/python3.cmd`: the py launcher, then Python on PATH, then any install in the registry (PEP 514), each one
test-run first, so the Microsoft Store stub is skipped and a python.org install without py or PATH still works.

The plugin also ships development skills, the same in every agent: `plan-change`, `debug-root-cause`, `verify-done`,
`review-diff`, `project-setup`, `efficiency-pass`, `knowledge-init`, `verified-goal` (keep working until a check command
proves the task is done, with a hard cap and an unattended loop recipe for each agent) and `fan-out-verify` (split a
broad job over subagents, have every finding refuted by a fresh verifier, report only what survives).

## Development

- `server/graph.py`, `server/lint.py` and `server/vectors.py` have self-checks under `__main__`; `server/test_server.py`
  tests the server. `deploy.sh` runs all of them first.
- `tests/clients_e2e.py` runs the client side against a real server: plugin hooks, the installers, Codex's own start
  of the MCP server, the offline copy and the skills sync. CI runs it on Windows, macOS and Linux for every push
  (`.github/workflows/clients.yml`).
- `tests/ui_smoke.py` checks the web UI with Playwright.
- A change to the plugin needs a version bump in `plugin/.claude-plugin/plugin.json` and
  `plugin/.codex-plugin/plugin.json` (the test checks they match), or installed copies never update.

Browser libraries are vendored with their licences in `server/static/vendor/` (force-graph, marked, DOMPurify); no CDN.

## License

MIT, see [LICENSE](LICENSE).

The server image includes the EmbeddingGemma model weights. Gemma is provided under and subject to the Gemma Terms of
Use found at [ai.google.dev/gemma/terms](https://ai.google.dev/gemma/terms); its use is also subject to the
[Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy). The weights are not modified.
