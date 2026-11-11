# knowledge

A self-hosted knowledge server for AI coding agents: live docs and memory over MCP for Claude Code, Codex and
Antigravity, plus a web UI with a graph. Server in `server/` (Python, one Docker container), clients in `plugin/`.

## Commands
- check (must pass before "done"; the pre-commit hook runs it; about 50 s): `./check.sh`; `./check.sh --unless-passed` skips
  it when this exact tree passed on this checkout already (deploy.sh uses that); in an agent session run it (and deploy.sh) under
  `env -u KNOWLEDGE_URL -u KNOWLEDGE_TOKEN -u KNOWLEDGE_CHAT`, or the session's own server settings break the docs sync test (401)
- run: deployed as a Docker stack; there is no local dev server
- test one thing: `cd server && uv run test_server.py` (or `uv run lint.py`, `uv run graph.py` for their self-checks)
- UI test: `uv run --project server --with playwright python tests/ui_ci.py` (a seeded throwaway server; CI runs it too),
  or `env -u KNOWLEDGE_URL uv run -q --with playwright python tests/ui_smoke.py` (read-only against the live server;
  an agent session has the public KNOWLEDGE_URL set, which would win over local.env and land on the sign-in page)
- load test of the web UI against a server on a copy of real data, before and after a change: `tests/ui_load.py`
- client test (about 2.5 min; `KNOWLEDGE_E2E_KEEP=1` keeps its temp folder): `uv run --project server python tests/clients_e2e.py`; CI runs it on Windows, macOS and Linux
  (`clients.yml`, not for a push of only docs or web UI files; `check.yml` runs the check and the UI test)
- a really hanging disk (Linux with FUSE; not in the check): `systemd-run --user --scope -q -p MemoryMax=3G uv run --project server --with fusepy python tests/storage_hang.py`
- a really hanging disk under the companion (Linux with FUSE; not in the check): `systemd-run --user --scope -q -p MemoryMax=3G uv run --project server --with fusepy python tests/companion_hang.py`
- a really blocked NFS mount (Linux with an NFS server and passwordless sudo; not in the check; about 2 min): `uv run --project server python tests/nfs_outage.py`
- deploy: `./deploy.sh` (routine; it is also how work lands). It rsyncs the working tree, so it refuses uncommitted changes
  (with someone else's changes in the checkout, deploy from your own worktree; a worktree uses the main checkout's
  `local.env`). It refuses while CI is red on main (`--fix-main` for the deploy that fixes it), unless a newer run on main
  is still going: then it waits for that one (up to 30 minutes). Under a lock on forge (a
  dead deploy's lock expires after 30 minutes; a held one is waited for up to 10 minutes, so start a deploy in the
  background or with a long timeout, never retry it in a loop yourself) it merges `origin/main` in (a conflict: merge it yourself, deploy again),
  hands out the next plugin version when `plugin/` changed, runs the check on the combined tree, refuses a checkout
  without the live commit (`--replace-live` takes it off the server on purpose) and pushes to `origin/main` before it
  goes live (live is always in main)
- roll back: `git revert <commit>` and `./deploy.sh`

## Project rules
- Public repo: no names, user names, home paths or LAN addresses in code, tests or docs; installation values live in
  `local.env`. Commit author is the GitHub noreply address.
- Plugin parity: every plugin feature works the same in Claude Code, Codex and Antigravity, with a check per agent in
  `tests/clients_e2e.py`. Do not bump the plugin version yourself: `deploy.sh` hands out
  the next patch number under its lock (two sessions once took the same one 8 times in a day); set a minor or major
  bump by hand in both `plugin.json` files and `server.json`.
- New logic in `graph.py`, `lint.py`, `tasks.py` or `vectors.py` gets an assert in its `__main__` self-check.
- Check a test's own exit code, never through a pipe (`| tail` hides a failure).
- Design defaults: all text in RAM; file watcher for instant updates plus a 60 s stat rescan; embeddings local (ONNX);
  hot reload: not needed (deploy rebuilds in seconds).

## Layout
- `server/`: MCP server, web UI (`static/`), lint, graph, embeddings, tests
- `plugin/`: the client plugin (hooks, skills, MCP relay) for all three agents
- `clients/`: device setup script
- `watcher/`: jobs that run on the machine holding the files (watcher, weekly reports, review loop)
- `tests/`: UI smoke test and client end-to-end test
