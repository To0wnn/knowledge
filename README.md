# knowledge

Live, shared access to Anthony's homelab docs and Claude memory for Claude Code on every device.

- **Server** (`server/`): small MCP server (streamable HTTP, stateless) in a Docker container on forge
  (CT 101, Dockhand stack `knowledge`, port 8766, `https://knowledge.to0wnn.com`, LAN/WireGuard only).
- **Plugin** (`plugin/`): Claude Code plugin with the MCP connection and a SessionStart hook that loads the
  shared CLAUDE.md files and the memory index. This repo is also the plugin marketplace.
- **Source of truth (option A, current):** the files stay on the VM (<lan-ip>) and are NFS-exported to forge
  only (`/etc/exports.d/knowledge.exports`). Sessions on the VM keep using them natively; other devices use the plugin.
  Everyone sees the same files live.

## What is shared

| Path in the tools | On the VM | Access |
|---|---|---|
| `memory/…` | `~/.claude/projects/-home-to0wnn-homelab/memory` | read; write `*.md` and `archief/*.md` |
| `homelab/…` | `~/homelab` | read; write top-level `*.md` docs |
| `claude/CLAUDE.md`, `claude/skills/`, `claude/agents/` | `~/.claude/gedeeld/CLAUDE.md` (symlinked from `~/.claude/CLAUDE.md`), `~/.claude/skills`, `~/.claude/agents` | read |
| `anpitek/…` | `~/anpitek` (code repo) | **Markdown only**; write `anpitek/*/*.md` |
| `anpitek-memory/…` | `~/.claude/projects/-home-to0wnn-anpitek/memory` | read; write `*.md` and `archief/*.md` |
| `trade/…` | `~/trade_bot` (code repo) | **Markdown only**; write `trade/*.md`, `trade/docs/*.md` |
| `trade-memory/…` | `~/.claude/projects/-home-to0wnn-trade-bot/memory` | read; write `*.md` and `archief/*.md` |

**Projects.** homelab, anpitek and trade each have a docs root and a memory root (`PROJECTS` in `server/graph.py`).
The plugin's SessionStart hook sends `$CLAUDE_PROJECT_DIR`; the server picks the project from it (`…/trade_bot` →
trade, `…/anpitek/…` → anpitek, anything else → homelab) and returns that project's `CLAUDE.md` and memory index,
like a native session in that directory gets. Code, logs and state files of the code repos are never indexed or
served (`venv/`, `.next/` skipped too); code reaches other devices through git. The web UI has a project switch
(homelab / anpitek / trade, one graph each; skills show in every project).

**Web UI features (18 Oct):** one graph per project; colour by type or by topic (deterministic label-propagation
clusters, named after their best-connected note); local graph (1–3 steps around the selected note); optional 3D view
(vendored 3d-force-graph, loaded only when switched on); edit a note in the browser (same version check as
`knowledge_write`, Ctrl+S) and create a memo with its index line; Recent tab (newest changes per project, with the
device when the change came through the server, otherwise "edited on the VM"); deep links `/ui#<path>` with working
back/forward; quick open Ctrl+K across projects; keys `/`, `1`–`3`, `E`, `L`, `Esc`. Static files are served with
`Cache-Control: no-cache` (ETag revalidation), so a deploy shows up at the next reload.

**Semantic search, topics and insights (18 Oct):**
- `vectors.py`: Markdown chunks (~1200 chars) embedded locally with **embeddinggemma-300m** (fastembed/ONNX, model baked
  into the image, `HF_HUB_OFFLINE=1`). Chosen in a test on the real memos (8 paraphrased questions): gemma 8/8 in the
  top 3, mpnet 6/8, potion 5/8; Qwen3-Embedding-0.6B needed 12.8 GB RAM and was dropped. Background thread, `nice 10`,
  6 CPU threads, batches of 4: ~1.8 GB RAM, full run ~13 min (5.5 chunks/s), then only changed chunks; cache saved
  every ~640 chunks. Each chunk is embedded with its file title (`title: … | text: …`). The UHD 770 iGPU was tested
  via OpenVINO (1.2 chunks/s, OOM at batch 16) and rejected: slower than 2 CPU threads. Cache `/state/vectors.npz` (by chunk
  hash). Files > 300 KB stay regex-only. Container `mem_limit: 3g`. MCP tool `knowledge_semantic_search`; the UI shows
  "By meaning" results under the exact matches.
- `graph.py`: **Louvain** communities (networkx, seed 42) with cohesion; **insights**: isolated notes, sparse topics
  (cohesion < 0.15), bridge notes (3+ topics), surprising cross-topic links (UI tab Insights; isolated memos and sparse
  topics also in lint); **relevance model** for `knowledge_related` and the note panel: link ×3, shared source ×4,
  Adamic-Adar ×1.5, same type ×1, meaning ×3 (cosine > 0.3).
- **Source traceability:** memos list their raw sources in frontmatter `sources: [file.md]`; ingest stores a sha256 and
  refuses the same content twice.
Ideas taken from nashsu/llm_wiki (README only; no code copied).

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
- **Safe concurrent writes:** per-file lock + atomic replace for everything that goes through the server.
  `knowledge_edit` replaces one unique snippet, so edits elsewhere in the file survive; `knowledge_write` and
  `knowledge_delete` refuse when the file changed since it was read. Right before replacing, the server re-reads the
  file and retries the edit on fresh content if a native edit on the VM landed in between (up to 5 times). Measured
  18 Oct: 2 clients x 25 simultaneous edits = no loss; with 25 native VM edits interleaved within milliseconds,
  1-3 per round were lost (remaining window ~1-2 ms = one NFS round trip). In real use (edits minutes apart) that is
  negligible, and Claude Code's own Edit tool refuses to write a file that changed since it read it. Fully race-free
  needs all writers to go through the server (option B). Every write/delete is logged with the device name; deleted
  files are kept in `state/deleted/` on forge.
- **Monitoring:** Uptime Kuma monitor "Knowledge server" (`/health`).
- **Speed (measured 18 Oct, end to end over HTTPS incl. a fresh TLS handshake):** search over everything
  21–50 ms (was 15.4 s), list 11 ms, read 11 ms; server at ~0.3 % CPU and ~120 MB RAM.
  - **In-memory index:** all shared text (~1.3k files, 24 MB) lives in RAM, so list and search never touch NFS.
  - **Event-driven freshness, no polling:** `watcher/knowledge-watch.py` runs on the VM (systemd user unit
    `knowledge-watch`, inotify via `inotifywait`) and posts changed paths to `/invalidate` (token with `"watch": true`)
    within ~0.2 s; the server re-reads only those files with `open()` (NFS close-to-open, so no stale attribute
    cache). Native edits on the VM are searchable within ~0.4 s. A stat-only rescan every 60 s is the safety net.
  - **Case-insensitive search on a lowercase copy** without `re.I` (12x faster: 8 ms vs 98 ms over 24 MB), falling
    back to `re.I` for patterns with uppercase escapes (`\S`, `\W`, …) or files whose lowercase form changes length.
  - Directory walk prunes `.git`/`node_modules`/`.venv`, binaries are skipped, responses are gzip-compressed by the
    server, uvicorn runs on uvloop + httptools (C), NFS mounts use `actimeo=1`.

## LLM-wiki features: lint, ingest, web UI

Following Karpathy's "LLM Wiki" pattern (raw sources -> wiki pages -> index, with ingest / query / lint):

- **`knowledge_lint`** (tool + UI tab): deterministic checks on the in-memory index, ~80 ms: index lines pointing to
  missing files, memos missing from `MEMORY.md`, dead `[[wikilinks]]`, missing frontmatter, memos > 15 KB without an
  archive copy, long index lines, orphans, stale open items (newest date > 45 days), memos that probably contain a
  secret (counts only, values never shown) and raw sources not yet turned into a memo. Contradictions and superseded
  claims are judged by the Claude session reading the report, not by the server.
- **`knowledge_ingest`** (tool: URL or pasted text; UI: also PDF/DOCX/MD/TXT/HTML upload): stores the source unchanged
  in `homelab/sources/<date>-<slug>.md` (never overwritten), appends `homelab/sources/LOG.md`, and returns the text
  plus possibly related memos (IDF-weighted, nothing rather than noise). The session then writes/updates memos.
  Extraction: trafilatura (web), pypdf (PDF), python-docx, run in a child process with CPU/memory limits.
  URL fetching is SSRF-safe: http(s) on 80/443 only, every resolved address must be public (no private, loopback,
  link-local, CGNAT, reserved), the connection goes to the validated IP (no DNS rebinding), redirects re-checked,
  identity encoding, 10 MB / 20 s caps. Source text is treated as data, never as instructions.
- **Web UI** at `https://knowledge.to0wnn.com/ui`: an Obsidian-style "brain" graph (force-graph, canvas; the layout
  settles and stops, so no CPU while idle) of memos (coloured by type), docs, skills and sources with edges for
  wikilinks, markdown links, doc/skill references and cited sources; type filters; full search (server-side, ~20 ms);
  note preview (marked + DOMPurify) with clickable links and connected notes; lint tab; ingest form. Sign in once
  with a device token: an HttpOnly, SameSite=Strict cookie keeps the browser signed in for a year; UI writes also
  need an `X-Knowledge-UI` header (CSRF). All JS is vendored with its license in `server/static/vendor/`
  (force-graph 1.52.0 MIT, marked 18.1.1 MIT, DOMPurify 3.4.17 Apache-2.0/MPL-2.0); no CDN, works on the LAN only.

## Install on a device (once)

1. Create a token on the VM: add `"<device>": {"token": "<long random>", "write": true}` to
   `~/.config/knowledge-server/tokens.json` (e.g. `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`),
   copy the file to forge `/root/docker/knowledge/config/tokens.json` (owner 1000, mode 600). Read-only device:
   `"write": false`. Also add the token to `~/.config/kennis-sync/geheimen.tsv`.
2. The device needs LAN or WireGuard (the route only admits 10.0.0.0/8).
3. In Claude Code on that device:
   ```
   claude plugin marketplace add <git URL of this repo>
   claude plugin install knowledge@knowledge
   ```
   Enter the token when asked (stored in the keychain). URL default: `https://knowledge.to0wnn.com`.
4. Turn off local auto memory on that device so it does not build a second memory:
   `"autoMemoryEnabled": false` in `~/.claude/settings.json`.
5. Start a session: the first lines show `[knowledge context part 1/2]`; `/mcp` lists `knowledge`.

Do **not** install the plugin on the VM itself: sessions there read the files natively.

## Watcher on the VM (option A)

`ln -s ~/knowledge/watcher/knowledge-watch.service ~/.config/systemd/user/ && systemctl --user enable --now knowledge-watch`
(needs `inotify-tools`, linger on, and `~/.config/knowledge-server/watcher.token` = the `vm-watcher` token).
With option B the watcher runs wherever the files live (forge) instead.

## Deploy / update the server

`./deploy.sh` runs the self-test, rsyncs `server/` to the forge stack and rebuilds. Bump versions in
`pyproject.toml` (then `uv lock`) and the `FROM`/uv tags in the Dockerfile deliberately.

## Option B (later): files on forge

1. Stop the stack, copy the three trees to forge (e.g. `/root/docker/knowledge/data/{memory,homelab,claude}`).
2. In `compose.yaml` replace the NFS volumes with those bind mounts (owner 1000).
3. On the VM: remove `/etc/exports.d/knowledge.exports`, install the plugin there too and set
   `autoMemoryEnabled: false` (the VM then also works through the server).
The server itself needs no change (paths come from `KNOWLEDGE_*` environment variables).
