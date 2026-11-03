# Development

Moved from the README; [back to the README](../README.md).

- `server/graph.py`, `server/lint.py` and `server/vectors.py` have self-checks under `__main__`; `server/test_server.py`
  tests the server. `deploy.sh` runs all of them first.
- `tests/clients_e2e.py` runs the client side against a real server: plugin hooks, the installers, Codex's own start
  of the MCP server, the offline copy and the skills sync. CI runs it on Windows, macOS and Linux for every push
  (`.github/workflows/clients.yml`).
- `tests/ui_smoke.py` checks the web UI with Playwright. `tests/ui_ci.py` runs it against a throwaway server on the
  demo notes (seeded with projects, a used token, a pc, a chat and a task line); CI does the same on every push, and
  locally it is one command:
  `uv run --project server --with playwright python tests/ui_ci.py`
  (once: `uv run --with playwright python -m playwright install chromium`). Checks that need data the demo lacks
  print a `skip:` line; `KNOWLEDGE_SMOKE_SHOTS=<dir>` keeps screenshots. Against a real server, set `KNOWLEDGE_URL`,
  `KNOWLEDGE_TEST_USER` and `KNOWLEDGE_PROXY_SECRET` and run `tests/ui_smoke.py` itself
  (`KNOWLEDGE_SMOKE_BROWSER=chromium` when there is no system Chrome).
- A change to the plugin needs a version bump in `plugin/.claude-plugin/plugin.json` and
  `plugin/.codex-plugin/plugin.json` (the test checks they match), or installed copies never update.

Browser libraries are vendored with their licences in `server/static/vendor/` (force-graph, marked, DOMPurify); no CDN.

The screenshot in the README uses made-up demo notes from `demo/`; regenerate with
`uv run --project server --with playwright python demo/screenshot.py`.

## The maintainer's installation

The original installation runs differently (`server/compose.yaml`: notes shared over NFS from the machine the agents
run on, a zero-downtime switch on deploy, `KNOWLEDGE_BUILTIN_PROJECTS` turns on its two extra built-in projects): copy `local.env.example` to `local.env`, point the volumes at your folders
and run `./deploy.sh`, which runs the tests, copies `server/` to the Docker host, builds, switches and waits for
`/health`.
