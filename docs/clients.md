# Connect a client

Moved from the README; [back to the README](../README.md).

## Install

The server's setup page (`/setup`, readable without signing in, so a new person with only a token can follow it; the same steps as the help page `/ui/help` without the house rules) has the exact steps per client and OS. The admin page shows a new token together with the server address and these lines with `KNOWLEDGE_URL` filled in. In short:

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

## Plugin only, or with the companion

The install line sets up the plugin (memory, MCP, hooks) and, by default, the companion: an agent runner that starts
at logon, for chats, workers and approvals from the web UI. Where no process may start agents (a work laptop, an SSH
server), install the plugin only:

```sh
curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh -s -- --plugin-only
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1))) --plugin-only
```

- `--plugin-only` works on `install`, `setup` and `update`. Without it, `install` and `setup` in a terminal ask once:
  "Also install the companion ...? [Y/n]" (Enter means yes). Without a terminal and without a saved choice the
  companion is installed, as before.
- The choice is saved in `~/.knowledge/install.json` (`{"companion": false}`), so `update` (also the daily
  auto-update) and a later `setup` never put the companion back. The last line says what happened, e.g.
  `companion: not installed (plugin only); add it later with: knowledge-agent.py install-companion`.
- Switch later: `knowledge-agent.py install-companion` saves "companion" and installs it; `uninstall-companion`
  saves "plugin only" and removes it. Choosing plugin only on a pc that has the companion runs the same undo as
  `uninstall-companion` (YOLO off first) and says so.


## Import what your agents already know

A new server starts empty. The first `setup` (or `install`) in a terminal offers once to import what the agents on
this computer already know. It reads only `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` (agy),
Claude Code's local auto memory (`~/.claude/projects/*/memory/*.md`, not its index) and `CLAUDE.md`/`AGENTS.md` in
the root of folders linked to a project; a file that is a link to somewhere else is not read. It lists the files
(number, file, size, target memo) and what it skips with the reason, and you pick `all`, `none` (the default; also
without a terminal) or numbers such as `1,3,5`.

- Where it goes: a linked folder's files and the Claude memory of a linked folder go to that folder's project; the
  instruction files to the token's project. Claude memory of a folder that is not linked goes nowhere unless the token
  has exactly one project: link the folder first (skill `knowledge-init`).
- Secrets: known secrets are refused by the server; files that look like they contain other secrets (a private key,
  `sk-`/`ghp_`/`xoxb-`/`AKIA` keys, a random-looking `key=`, `token=` or `password=` value) are skipped and never
  uploaded. Neither check can find every secret: look at the list before you choose.
- Each file becomes one memo under the heading "Imported" through the server's `knowledge_new_memo` (frontmatter,
  both sections, index line, similar memos named); an existing memo is never replaced. The house-rules block the setup
  wrote into `AGENTS.md`/`GEMINI.md` is left out.
- On the machine the server reads its files from (its memory or `CLAUDE.md` is byte for byte the server's copy)
  nothing is imported.
- `~/.knowledge/imported.json` records what was imported, so a rerun imports nothing twice. Later:
  `knowledge-agent.py import` (`--dry-run` only lists; without a terminal it imports all listed files).

## The companion's autostart

`setup`, `install` and `update` end by setting up the companion unless plugin only was chosen (see above; again
only updates; `install-companion` alone):
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

## Server address, token and updates

`install claude` (or `codex`, `agy`) sets up one agent without asking which ones. `install` asks for your server's
address (or set `KNOWLEDGE_URL`, e.g. `KNOWLEDGE_URL=http://localhost:8766`, for one run); the installer has no default, so a
token never goes to a server you did not name.
The address that answered is stored in `~/.config/knowledge/url` for the hooks of every agent and set as the Claude
plugin's "Knowledge server URL" option, so later sessions and `update` need nothing else.
A device with no address uses no server and sends its token nowhere: its session start and the MCP relay say so and give
the one-line installer. Plugins before 0.14.2 had a built-in address; while such a device still reaches its server, the
server tells it to run the installer with that server's address before the update removes the built-in one.

It asks for the token once, checks it with the server, stores it in the keyring and installs the plugin. The plugin
keeps itself up to date in every agent, and says so once at session start: in Claude Code it turns on the marketplace's
auto-update (off under `/plugin` > Marketplaces); Codex and agy have no plugin auto-update, so there the plugin checks
GitHub at most once a day and updates itself with the agent's own CLI (the setup without a plugin: its git checkout is brought to
GitHub's main, also after a rewritten history when nothing in it is changed, then the setup runs again;
`knowledge-agent.py update` does the same by hand). agy's install deletes the old folder first, which Windows stops halfway when a process stands in that folder, so the update keeps a copy and puts it back when the install fails; a folder an older version left empty is installed again from a hook of another agent or the companion. Off: `KNOWLEDGE_AUTO_UPDATE=0`. The companion's and the web UI's "Update plugin" does it at once for all three agents, each compared with its own version, and says when it failed. A new version loads at the next start. Each session start
reports the plugin version (Codex and agy also their last update check and why it failed): the admin page shows it per device and agent, and a device still behind the newest
version after two days hears how to update by hand (also listed in lint as `plugin_behind`).

## Linking a folder, and Windows

Link a project folder to its project once: ask your agent (skill `knowledge-init`), which writes a file
`.knowledge-project` with the project name in the folder's root. Every session started there gets that project. A linked git worktree (Codex and agy make one per chat) counts as its main checkout: same project, docs mirrored from the main checkout only (the folder is the source: knowledge_write/edit/delete refuse the mirror, as the UI does, since the next sync would undo them), and the session is told it runs in a separate checkout. On Windows, agy needs a `python3` command on PATH (Python from the python.org install manager or the
Microsoft Store has one); without it the setup configures agy without the plugin. Codex and the agy hooks on Windows start Python through
`plugin/python3.cmd`: the py launcher, then Python on PATH, then any install in the registry (PEP 514), each one
test-run first, so the Microsoft Store stub is skipped and a python.org install without py or PATH still works.

## Skills

The plugin also ships development skills, the same in every agent: `plan-change`, `debug-root-cause`, `verify-done`,
`review-diff`, `project-setup`, `efficiency-pass`, `knowledge-init`, `verified-goal` (keep working until a check command
proves the task is done, with a hard cap and an unattended loop recipe for each agent) and `fan-out-verify` (split a
broad job over subagents, have every finding refuted by a fresh verifier, report only what survives).

## What the plugin does in every session

- **Usage limits:** each prompt checks how much of the agent's own usage limit is used and says it once per session
  and limit window at 50, 75 and 90%; from 95% a new subagent is refused (Claude Code `Agent`, Codex `spawn_agent`, agy
  `invoke_subagent`). The numbers are the agent's own: Codex's rollout, agy's language server (after its one-time
  `register`), and for Claude Code, which hands them only to its status line, a small tap that the plugin puts in front
  of your status line at session start (your line still shows; the old command is kept in
  `~/.knowledge/statusline-original.json`). Without numbers nothing is said or refused.
- **Secret guard:** an edit that writes a known secret value into a file is refused (Claude Code Edit/Write, Codex
  apply_patch, agy's file tools), where `token.py push-secrets` left the hash file (`secret set` and `secret rm` run it themselves; a value
  added in the Vaultwarden web UI needs a manual push). Only new text counts, so removing a secret is allowed.
- **Scripted runs:** set `KNOWLEDGE_NO_SESSION_LOG=1` for a measurement or a one-off reader: every agent's hook then skips
  the session log, which is for sessions someone looks back on.
- **Token use:** the session log also sends the tokens of each logged part (uncached input, cache writes, cache reads,
  output; Claude's per-block copies of one call counted once, Codex's running totals differenced). Lint reports
  `usage_outlier`: a session of the last week far above that agent's usual (modified z-score above 3.5 against the last
  month, from 10 sessions on).
