# knowledge

A small self-hosted knowledge server for AI coding agents. It gives Claude Code, Codex and Antigravity (agy) on every
device the same live docs and memory, over MCP, plus a web UI with a graph of how the notes connect.

Notes are plain Markdown files on disk. The server indexes them, serves them to agents with a per-device token, and
checks them against a few house rules, so a growing pile of agent memory stays findable and tidy.

## What it does

- **MCP tools for agents:** search (regex and semantic), read, list, related notes, write and edit (with conflict
  checks), new memo, delete (kept as a copy), history, lint, and ingest of a web page, PDF or text as a raw source.
- **Session start context:** each agent gets the shared instructions, the house rules and the memory index of the
  project it works in, plus open items whose date has passed and hand-over tasks between agents.
- **Hand-overs during a session:** before a prompt (at most every two minutes) Claude Code, Codex and agy ask
  `/tasks` for the tasks handed to them; a task added after the session started is told to the agent once.
- **Projects:** each project has a docs root and a memory root. A session picks its project from its working folder;
  a token can also be limited to one project (a guest).
- **Lint:** deterministic checks in ~80 ms: dead links, index lines without a file and the reverse, missing frontmatter
  or sections, oversized memos, overdue open items, probable secrets (never shown), raw sources not yet processed.
- **Web UI:** a home view per topic, full search, a note preview with connected notes, an editor, change history, a
  lint tab and an admin page for tokens, people and projects. Light or dark, your pick.
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
- **Web sign-in:** the UI expects a reverse proxy with forward auth (for example Authentik) that sets the user header.

## Run the server

1. Copy `local.env.example` to `local.env` and fill in your hosts.
2. Point the volumes in `server/compose.yaml` at your docs and memory folders (local paths or NFS).
3. `./deploy.sh` runs the self-checks and the server tests, copies `server/` to the Docker host, builds and starts it,
   and waits for `/health`.
4. Create device tokens on the admin page (`/ui/admin`).

Requirements: Docker, about 2 GB RAM (the embedding model is 1.2 GB), and a reverse proxy for HTTPS and sign-in.

## Connect a client

The server's help page (`/ui/help`) has the exact steps per client and OS. In short:

```sh
git clone https://github.com/To0wnn/knowledge ~/knowledge
python3 ~/knowledge/clients/knowledge-agent.py install claude   # or codex, agy (Windows: py instead of python3)
```

It asks for the token once, checks it with the server, stores it in the keyring and installs the plugin. The plugin
keeps itself up to date in every agent, and says so once at session start: in Claude Code it turns on the marketplace's
auto-update (off under `/plugin` > Marketplaces); Codex and agy have no plugin auto-update, so there the plugin checks
GitHub at most once a day and updates itself with the agent's own CLI (the setup without a plugin: `git pull` and the
setup again). Off: `KNOWLEDGE_AUTO_UPDATE=0`. A new version loads at the next start. Each session start
reports the plugin version: the admin page shows it per device and agent, and a device still behind the newest
version after two days hears how to update by hand (also listed in lint as `plugin_behind`).

Link a project folder to its project once: ask your agent (skill `knowledge-init`), which writes a file
`.knowledge-project` with the project name in the folder's root. Every session started there gets that project. A linked git worktree (Codex and agy make one per chat) counts as its main checkout: same project, docs mirrored from the main checkout only, and the session is told it runs in a separate checkout. On Windows, agy needs a `python3` command on PATH (Python from the python.org install manager or the
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
