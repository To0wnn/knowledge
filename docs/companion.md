# Companion, machines and remote control

Moved from the README; [back to the README](../README.md). The companion is a small local service on each pc. It starts workers, reports the machine's state, runs the
fixed remote actions and relays chats. Installing it is part of the client setup ([clients.md](clients.md)).

## Companion (this pc)

- **Companion (this pc):** `knowledge-agent.py serve` (or `agent_hooks.py serve`, alias `companion`) runs a small
  local service on `127.0.0.1:47321` only. Every minute it asks `GET /tasks/ready`, starts workers as above (it takes
  each task with `POST /tasks/woken` for its worker session first and gives it back when the start fails), keeps
  their leases while they run and reports the machine's state (`POST /machine/report`). On request it trusts the
  knowledge hooks the agents' own way (Codex: `codex app-server` `hooks/list` + `config/batchWrite`, again after every
  plugin update; Claude Code: `hasTrustDialogAccepted` in `~/.claude.json`; agy: `trustedWorkspaces`), only for folders
  with a `.knowledge-project`. Its Folders section lists the linked folders and candidates (git checkouts up to three
  levels under your home folder and folders agents worked in, also elsewhere on the pc, not in the temp folder) and
  links one to a project the token may write by writing `.knowledge-project`, as the knowledge-init skill does; unlink
  removes only that file. "Choose a folder" browses the pc's drives and links any folder, on the companion page and in the web UI's Machines
  list (the machine's owner only, as every remote action, answered through the same channel as a diff and not kept). A
  drive root, the home folder or above it and system folders are never linked. A YOLO switch per
  agent writes its native "run without asking" settings and unchecking restores the previous values (agy's only while
  agy is closed); workers then get the agent's own flag too. These are the agent's global settings on that pc (Claude
  Code `permissions.defaultMode: bypassPermissions`, Codex `approval_policy = "never"` + `danger-full-access`, agy
  `always-proceed`), so YOLO covers every session of that agent there, your own interactive ones too, not only workers
  and chats. Turning it on, here or from the Machines list, needs the word YOLO typed in a dialog that says so. Every changed file is first copied to
  `~/backups/knowledge-companion/<date>/` (last five days kept). Hook failures Codex and agy log themselves are reported
  with `POST /status` as `agent-log` (Codex: only failures of the knowledge hooks). Its page is at
  `http://127.0.0.1:47321/`; the web UI shows "This pc" in its sidebar when the companion answers there. The local API answers
  only that page and the knowledge web UI's origin; every change except trusting the knowledge hooks, and the tasks
  and activity, also need the key in `~/.knowledge/companion/api-key` (readable by you only), which the menu entry
  passes to the page through the private file `~/.knowledge/companion/open.html` (`#key=`; never on a browser's
  command line, where other accounts on the pc could read it). YOLO and the worker settings change only on its own page (or remotely by the owner).

- **A folder that hangs:** a round never waits for a hard NFS mount or a share that stopped answering: looks at known folders wait 3 s together, git numbers 30 s, each on its own thread. The folder is then skipped until that call returns, said once in the activity list and the Tasks tab (the first three folders, so one dead NAS does not fill the list), and again when it answers. A remote action on such a repository still waits. `tests/companion_hang.py` proves it on a FUSE mount that really hangs.

## Machines

- **Models:** with a `model_advice.json` next to the development rules, Admin > Settings is the one place for models: per role and agent the advice with its test score and your own pick, models any pc offers that nobody measured, and per own pc whether its agents match, with what differs and an Apply button. Apply writes the lead and reader into that agent's own settings (a copy goes to `~/.knowledge/backups/`), only on a click; the result shows next to the button. Each agent reports what it is set to, so the page shows the real state. Without the file none of this appears.
- **The agents update too (and the tools, see Tools):** once a day, when no game, chat or worker runs (a pc that always has work: after two days anyway, a running session is not touched; never during a game), the companion runs `claude update`, `codex update` and `agy update`, each agent's own command; a failure stands in Activity as an error, a running session stays on its version until its next start. By hand: Update agents in a pc's ⋯ menu on the Machines page, or Update the agents in the companion. Off per pc: `KNOWLEDGE_AUTO_UPDATE=0`, which also turns off the plugin's own check.
- **Updates reach the running companion:** it watches its plugin version and restarts itself when an update lands (between rounds and actions, after two equal readings; on Windows through its Scheduled Task); live chats and workers are picked up again. The version in the list is the code that runs.
- **What a card shows:** per agent "x of y running", then Needs you (failed, commits not merged, running on an offline pc), Running now, and Earlier folded away; a worker shows its task text with its id behind it. Archive takes an ended worker off the list without touching its branch, worktree or restart hold, and refuses while commits are not merged. YOLO stays visible as a pill; its switch and the worker maximum sit under Settings. Above the cards: tasks no pc can start because no folder is linked to their project.

- **Machines:** every request of plugin 0.15+ names its pc (`X-Knowledge-Machine`, a random id made once in
  `~/.knowledge/machine.json` with the host name as label; `KNOWLEDGE_HOME` moves `~/.knowledge`). Leases and wakes key
  on token + machine, so one token on two pcs is two holders. The server keeps per machine and agent the sessions of
  the last two hours (project, held task) and what the machine's companion reports; the Tasks tab lists them under
  "Machines": online or "offline since", per agent its plugin, a red YOLO label, hooks trusted, usage limit (or
  "unknown"), last hook error, workers and recent sessions. A person sees the machines of their own tokens; an admin
  all. A machine id belongs to the person (or the admins) whose token first reported it; another
  person's token on the same pc gets a machine record of its own and never sees or steers the first one. Plugins before 0.15 show as one "old plugin" row per token.

## Tools (add-ons)

- **What it is:** an outside tool (an MCP server, an agent plugin, a skill repo) installed on a pc for every agent
  there (Claude Code, Codex, agy) with one click: a pc's card on the Machines page has a Tools list with Install,
  Update and Remove, the installed version and the result of the tool's own health check. Nobody has to open the
  tool's repository.
- **Where a tool is described:** one JSON manifest per tool in `plugin/tools/<id>.json`, shipped with the plugin. The
  page sends only the tool id; the companion runs the steps from its own plugin's manifest (so no free text reaches a
  pc, and a new manifest arrives with a plugin update). Format (checked by `plugin/hooks/tools.py`, which refuses an
  unknown key):

  ```json
  {"name": "...", "description": "...", "homepage": "https://...", "needs": ["npm"],
   "install": [{"run": ["npm", "install", "--global", "pkg@latest"]},
               {"run": ["pkg", "setup", "--client", "codex"], "agent": "codex"}],
   "check":   [{"run": ["pkg", "doctor", "--json"], "info": true}],
   "version": {"run": ["pkg", "--version"]},
   "remove":  [{"run": ["npm", "uninstall", "--global", "pkg"]}]}
  ```

  A step is one of `run` (a command looked up on PATH, no shell), `fetch` (the newest release file for this
  platform: `{"github": "owner/repo"}` with GitHub's sha256 of the asset (`"tag": "regex"`: the newest release of a
  line, when a tool supports only that line), or `{"shasums": "https://.../SHASUMS256.txt"}`
  like Node.js; `"asset": {"<os>-<x64|arm64>": "regex"}` or `{"match", "strip"}`; unpacked into `"into"` in
  `~/.knowledge/tools`, the old copy replaced only once the new one is complete and checked; skipped when that release
  is there already, or when no file fits this platform) or `delete` (a name in that folder). `{tools}` in an argument,
  `path` or `env` value is `~/.knowledge/tools`; the manifest's `path` goes in front of PATH and its `env` into every
  step, so a tool can bring its own runtime without admin rights (`{exe}`/`{cmd}`: `.exe`/`.cmd` on Windows; an env
  value starting with `?` is set only when that path exists). `"bin": {"name": [argv]}` writes a small launcher per
  program into `~/.local/bin` (where Claude Code, uv and pipx put theirs, so it is on PATH; on Windows it is added to
  the user's own PATH), so an agent finds the program without the tool's env. A file there that is not one of our
  launchers is never overwritten (the list says which name was taken); remove deletes ours. Downloads are https only, tried three times on a
  server error, and use a `GH_TOKEN` or `GITHUB_TOKEN` from the environment for the GitHub API when one is set. `agent`: only when that agent is installed; `os`: only on
  `linux`, `darwin` or `windows`; `ok_if`: output text that makes a non-zero exit fine ("already"); `expect`: a regex
  the output must match; `info` (check only): reported but never marks the tool unhealthy. Install stops at the first
  failed step; remove runs every step. Both run in the background (one tool at a time, other actions do not wait; a
  plugin restart does), a step stops after 15 minutes with its whole process tree, and the result or the error shows in
  the Tools list and in the companion's Activity. Update is install again, so steps use `@latest` or the agent's own marketplace
  update. A JSON report's failed `checks` or its `remediation` become the one line the page shows.
- **Shipped:** `rea` (its own Node.js LTS, Temurin JDK LTS, Ghidra, jadx-headless-mcp, Apktool, WABT and rizin, and on
  Linux binwalk and mitmproxy from conda-forge through micromamba, all in `~/.knowledge/tools` without root; tshark,
  sigrok and unblob have no build without root and stay with the system;
  npm `rea-agents` into a prefix there, registered with `rea setup` per agent, which passes Ghidra, the JDK and JADX to
  each agent; the other engines reach agents through launchers; checked with `rea doctor`), `ponytail` (each agent's own
  plugin command) and `no-ai-slop` (the skills CLI, telemetry off). The Node.js and JDK major versions are in the
  manifest (`latest-v26.x`, `temurin25-binaries`): move them when a newer LTS is out. Ghidra is the newest 12.1.x,
  the line REA 6.8.0 accepts (its doctor refuses 12.2): move the `tag` when REA takes a newer line. On Linux and macOS
  REA registers its `rea` launcher, which starts the first `node` on the agent's PATH; a step points that launcher at
  the Node in `~/.knowledge/tools`, as REA already does itself on Windows.
- **Updates:** with the agents' daily update (same rules: not during a game, chat or worker; Update agents in the ⋯
  menu runs it now; off with `KNOWLEDGE_AUTO_UPDATE=0`) every installed tool is installed again, so it takes the newest
  release; a fetch skips what is current. A failed install or update keeps the tool as it was, shows red in the list
  and goes once to the admins' Matrix/ntfy (again only after it was fixed and failed anew). No cooldown after a
  release and no automatic rollback: the newest version goes in, and a red check says when it broke something.
- **Adding a tool:** a new manifest plus a plugin version bump; `tools.py --self-test` validates every manifest.

## The admins' own devices

- **The admins' own devices:** a token an admin makes counts as the admins only when it is marked **"My own device"**
  (`"self": true`, a checkbox on the admin page, also on existing tokens): its machines are the admins' to see and
  steer, and its task lines are trusted. Mark the tokens of your own pcs, phone and the server's own jobs (watcher,
  reports); never a token you made for a friend, a guest or a personal assistant. Unmarked, an admin-made token is
  its own party: its machines get records of their own, its task lines wait for approval. Machine records bound to
  the admins before this flag existed stay theirs and match marked tokens only.

## Remote control

- **Remote control:** the owner of a machine steers it from the Machines list with fixed actions only: start a worker
  for a ready task, stop a worker, restart a worker that ended (`restart`: at once, past the companion's 6 h hold;
  queued as its own start), roll a YOLO worker back, trust hooks, update the plugin, install or remove a tool (see Tools above), YOLO on or off per agent
  (on needs the word YOLO typed in a dialog), workers allowed, max workers, link or unlink a folder (only folders the
  companion listed itself). The server queues them per machine (`POST /ui/api/machines/action`); the companion pulls
  them about every 10 seconds (`GET /machine/actions`), runs them with its own code and reports the result; an
  offline machine's action shows "waiting for machine". Every action is logged with who and when, and shows in the
  companion's activity log. There is no free-text command anywhere; free text reaches a pc only as a chat message
  to an agent (below), which runs with that agent's own permissions.

## Agents (remote control) per token

- **Agents (remote control):** a switch per device token (`"agents"` in `tokens.json`, a checkbox "agents" in the
  Access column of the admin page's Tokens table; a person switches it on their own tokens, an admin on every token
  with `POST /ui/api/admin/token {"name", "update": true, "agents": bool}`). It says whether the pc behind the token
  may be steered from the web UI. Default on: a token from before the switch (no key) and every new token, so nothing
  changes for an existing installation; a token that is not the person's own (an admin-made token not marked "My own
  device": a guest, a friend, a PA) could not be steered anyway. Off, the server refuses it, not only the page:
  `GET /machine/chats` hands out no messages, stops, diffs or answers (`"agents": false`), `GET /machine/actions`
  hands out no actions (they wait), `GET /tasks/ready` lists nothing and `POST /tasks/woken` refuses a worker
  session, so its companion starts no workers; `/machine/chats/ask`, `/events`, `/worker`, `/adopt`, `/diff` and
  `/file` answer 403 "remote control is off for this pc's token". In the web UI that machine counts as steerable only
  through a token with the switch on: remote actions, new chats, messages, approvals and diffs are refused with the
  same message, and its chats and workers drop out of the Agents view (forgetting the machine still works). Memory
  and the MCP tools are untouched, and so is "pak <ID>" in a session at the pc itself. A person none of whose tokens
  has it on gets `me.agents` false and sees only the knowledge part of the web UI. The companion goes quiet when it
  hears `"agents": false` (or such a 403): one line in its activity log ("Remote control is off for this pc's token
  on the server"), the chats it runs end (a running turn stops, live agents end cleanly, a message still fetching
  its files is dropped), and its polls go on without further errors; when the server says on again it logs that
  once and the next message resumes the chat. Running workers keep running; no new ones start.

## YOLO safety without blocking

- **YOLO safety without blocking:** task text by a writer the project does not trust reaches agents and workers marked
  as data, not instructions. A watch hook after every tool flags what a YOLO session does that is risky (force push,
  `rm -rf` outside the project, `curl | sh`, reading key or token files, reading the vault) in the companion's activity
  log and once per session and kind as a Matrix notice; it never refuses. A YOLO worker gets a git checkpoint of its
  whole worktree per turn on `refs/knowledge/checkpoints/<ID>`; "roll back" puts it back as before its last turn and
  keeps the state before as `<ID>-undo`.

## Hook failures

- **Hook failures:** when one of the plugin's own hooks crashes or exits with an error, the device keeps it in
  `~/.knowledge/hook-errors.jsonl` and the per-turn hook reports new ones with `POST /status` (at most every two
  minutes, never holding up the turn); the tasks tab shows the last failure per device and agent.
