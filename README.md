# kennis

Live, shared access to Anthony's homelab docs and Claude memory for Claude Code on every device.

- **Server** (`server/`): small MCP server (streamable HTTP, stateless) in a Docker container on forge
  (CT 101, Dockhand stack `kennis`, port 8766, `https://kennis.to0wnn.com`, LAN/WireGuard only).
- **Plugin** (`plugin/`): Claude Code plugin with the MCP connection and a SessionStart hook that loads the
  shared CLAUDE.md files and the memory index. This repo is also the plugin marketplace.
- **Source of truth (option A, current):** the files stay on the VM (<lan-ip>) and are NFS-exported to forge
  only (`/etc/exports.d/kennis.exports`). Sessions on the VM keep using them natively; other devices use the plugin.
  Everyone sees the same files live.

## What is shared

| Path in the tools | On the VM | Access |
|---|---|---|
| `memory/…` | `~/.claude/projects/-home-to0wnn-homelab/memory` | read; write `*.md` and `archief/*.md` |
| `homelab/…` | `~/homelab` | read; write top-level `*.md` docs |
| `claude/CLAUDE.md`, `claude/skills/`, `claude/agents/` | `~/.claude/gedeeld/CLAUDE.md` (symlinked from `~/.claude/CLAUDE.md`), `~/.claude/skills`, `~/.claude/agents` | read |

Never shared: `.git`, `.env`, `*.bak*`, keys, `credentials*`, the rest of `~/.claude` (logins, sessions, settings).

## Why it does not break easily

- **No logins that expire:** one long random token per device (`tokens.json`), checked by the server itself.
  Revoke a device by deleting its entry; tokens are re-read per request, no restart needed.
- **Stateless HTTP + JSON responses:** no MCP sessions that get lost when the server restarts or Claude Code updates.
- **Pinned versions:** `pyproject.toml` + `uv.lock` + exact image tags; an update is a deliberate bump.
  The container has its own Python, so OS updates on forge or the VM do not touch it. `restart: always`.
- **Only documented Claude Code features:** an `http` MCP server with a header, `userConfig` (token in the OS
  keychain), and a SessionStart hook. Hook output is capped at 10,000 characters by Claude Code, so the server
  splits the startup context into parts (`/context/1..4`).
- **Safe concurrent writes:** per-file lock + atomic replace. `kennis_edit` replaces one unique snippet, so
  edits by another session elsewhere in the file survive; `kennis_write` refuses if the file changed since it
  was read. Every write is logged with the device name (`/root/docker/kennis/state/audit.log` on forge).
- **Monitoring:** Uptime Kuma monitor "Kennis-server" (`/health`).

## Install on a device (once)

1. Create a token on the VM: add `"<device>": {"token": "<long random>", "write": true}` to
   `~/.config/kennis-server/tokens.json` (e.g. `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`),
   copy the file to forge `/root/docker/kennis/config/tokens.json` (owner 1000, mode 600). Read-only device:
   `"write": false`. Also add the token to `~/.config/kennis-sync/geheimen.tsv`.
2. The device needs LAN or WireGuard (the route only admits 10.0.0.0/8).
3. In Claude Code on that device:
   ```
   claude plugin marketplace add <git URL of this repo>
   claude plugin install kennis@kennis
   ```
   Enter the token when asked (stored in the keychain). URL default: `https://kennis.to0wnn.com`.
4. Turn off local auto memory on that device so it does not build a second memory:
   `"autoMemoryEnabled": false` in `~/.claude/settings.json`.
5. Start a session: the first lines show `[kennis context part 1/2]`; `/mcp` lists `kennis`.

Do **not** install the plugin on the VM itself: sessions there read the files natively.

## Deploy / update the server

`./deploy.sh` runs the self-test, rsyncs `server/` to the forge stack and rebuilds. Bump versions in
`pyproject.toml` (then `uv lock`) and the `FROM`/uv tags in the Dockerfile deliberately.

## Option B (later): files on forge

1. Stop the stack, copy the three trees to forge (e.g. `/root/docker/kennis/data/{memory,homelab,claude}`).
2. In `compose.yaml` replace the NFS volumes with those bind mounts (owner 1000).
3. On the VM: remove `/etc/exports.d/kennis.exports`, install the plugin there too and set
   `autoMemoryEnabled: false` (the VM then also works through the server).
The server itself needs no change (paths come from `KENNIS_*` environment variables).
