# knowledge

A self-hosted knowledge server for AI coding agents: live docs and memory over MCP for Claude Code, Codex and
Antigravity, plus a web UI with a graph. Server in `server/` (Python, one Docker container), clients in `plugin/`.

## Commands
- check (must pass before "done"; the pre-commit hook runs it): `./check.sh`
- run: deployed as a Docker stack; there is no local dev server
- test one thing: `cd server && uv run test_server.py` (or `uv run lint.py`, `uv run graph.py` for their self-checks)
- UI test: `uv run -q --with playwright python tests/ui_smoke.py` (read-only against the live server)
- client test: `uv run --project server python tests/clients_e2e.py`; CI runs it on Windows, macOS and Linux
- deploy: `./deploy.sh` (runs the check first; routine)
- roll back: `git revert <commit>` and `./deploy.sh`

## Project rules
- Public repo: no names, user names, home paths or LAN addresses in code, tests or docs; installation values live in
  `local.env`. Commit author is the GitHub noreply address.
- Plugin parity: every plugin feature works the same in Claude Code, Codex and Antigravity, with a check per agent in
  `tests/clients_e2e.py`. Bump the version in both `plugin/.claude-plugin/plugin.json` and
  `plugin/.codex-plugin/plugin.json` on any plugin change.
- New logic in `graph.py`, `lint.py` or `vectors.py` gets an assert in its `__main__` self-check.
- Check a test's own exit code, never through a pipe (`| tail` hides a failure).
- Design defaults: all text in RAM; file watcher for instant updates plus a 60 s stat rescan; embeddings local (ONNX);
  hot reload: not needed (deploy rebuilds in seconds).

## Layout
- `server/`: MCP server, web UI (`static/`), lint, graph, embeddings, tests
- `plugin/`: the client plugin (hooks, skills, MCP relay) for all three agents
- `clients/`: device setup script
- `watcher/`: jobs that run on the machine holding the files (watcher, weekly reports, review loop)
- `tests/`: UI smoke test and client end-to-end test
