# knowledge

Live, shared access to Anthony's homelab docs and Claude memory for Claude Code on every device.

- **Server** (`server/`): small MCP server (streamable HTTP, stateless) in a Docker container on forge
  (CT 101, Dockhand stack `knowledge`, port 8766, `https://knowledge.to0wnn.com`; web UI public behind Authentik, MCP public with device tokens since 20 Oct).
- **Plugin** (`plugin/`): Claude Code plugin with the MCP connection and a SessionStart hook that loads the
  shared CLAUDE.md files and the memory index. This repo is also the plugin marketplace.
- **Source of truth (option A, current):** the files stay on the VM and are NFS-exported to forge
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
(homelab / anpitek / trade, one graph each; a skill shows in the projects whose notes mention it, an unmentioned one in homelab).

**Home view, admin and help (21 Oct):** with no note open, the middle shows one card per topic (its best-connected
notes with their index hook; "All N" unfolds the card) and the right column what changed lately and the lint count; on
a phone the cards replace the topic list. Lint findings show the note's title and a plain-words check name with what to
do (`LINT_INFO` in `app.js`: give a new lint check a line there). The admin page has tabs with counts
(`/ui/admin#tokens|people|projects|settings|system`); the help page uses `app.css` like the rest.

**All map and new memos (21 Oct):** the project switch ends with **All** (key `0`): every project on one map as its
own island, links between projects drawn in the accent, one home card per project. `knowledge_new_memo` writes a
memo the house-rules way (frontmatter, `## Stand`, `## Keuzes`, the index line under its heading) in one call; a new
memo through `knowledge_write` without that layout is refused with what is missing (existing memos only get hints).

**Web UI layout (20 Oct, round 2 of the UI review):** the note sits in the wide middle column; **Map** (or `M`) swaps
that column for the project graph (topic islands; a click gathers a note's neighbours in a ring and shows its card on
the right, a second click reads it). Left: tabs **Notes** (every topic of the project with its notes, or, while you
type, results: titles of all projects, full text of this project, then by meaning; Enter opens the first),
**Changes** (newest first: lines added/removed, who, and the question of the session that changed the file, taken from
the session log entry written after the change that names the file; a click opens that version's diff; changes since
your last look are marked and counted on the tab) and **Lint** (admins). Right: **Connected**, one list of related notes
(relevance model, with the reason) followed by linked notes the model did not rank. Header: project switch, the search
box (`/` or Ctrl+K), **+ Add** (new memo, add a source) and the user menu (Admin, Sign out). Edit a note in the browser
(same version check as `knowledge_write`, Ctrl+S), History with diff and restore; deep links `/ui#<path>` with working
back/forward; keys `/`, Ctrl+K, `1`–`9`, `E`, `M`, `Esc`. Removed in round 2: the 3D view, the Insights tab (its useful
parts are lint checks), the type/topic colour switch, the local graph and the separate quick-open dialog. Phone: lists
first, then the note, the map only on request. Static files are served with
`Cache-Control: no-cache` (ETag revalidation), so a deploy shows up at the next reload; `vendor/` files have their
version in the name and are cached as `immutable`; `/ui/api/graph` has an ETag on the index generation (304 when
unchanged). **Live:** `/ui/api/events` (Server-Sent Events) says `changed` within ~1 s of any change (watcher, MCP
write, UI save); the page then reloads the graph keeping its layout, refreshes Changes/Lint and the open note,
and only warns while you are editing. Green dot next to the title = live connection. **Always the latest release:** the page itself is `no-cache`; app.js/app.css are linked as
`?v=<release>` (hash of the UI files, fixed at startup) and cached as immutable, so a refresh always gets the new
version; the live stream sends the release in its `hello`, and an open tab that reconnects after a deploy reloads itself
(or shows a hint while you are editing). `timeout_graceful_shutdown=3`: open tabs no longer hold up a restart.

**Semantic search, topics and insights (18 Oct):**
- `vectors.py`: Markdown chunks (~1200 chars) embedded locally with **embeddinggemma-300m** (fastembed/ONNX, model baked
  into the image, `HF_HUB_OFFLINE=1`). Chosen in a test on the real memos (8 paraphrased questions): gemma 8/8 in the
  top 3, mpnet 6/8, potion 5/8; Qwen3-Embedding-0.6B needed 12.8 GB RAM and was dropped. Background thread, `nice 10`,
  6 CPU threads, batches of 4: ~1.8 GB RAM, full run ~13 min (5.5 chunks/s), then only changed chunks; cache saved
  every ~640 chunks. Each chunk is embedded with its file title (`title: … | text: …`): a live test of 14 paraphrased questions went from top-3 8/14 to 10/14 (top-5 11 → 12/14; regex 0/14). Tried and rejected on 24 questions (14 paraphrases + 10 with one exact term;
  semantic alone: top-1 14, top-3 19, top-5 22): **hybrid** BM25 + embeddings with reciprocal rank fusion was never
  better (weight 1: 13/17/19; 0.15–0.5: 13–15/19/21); a **reranker** (bge-reranker-v2-m3-int8) gave 16/20/22 but
  costs ~6 s per query and ~2 GB extra RAM, and it pushed two right answers down. What did help: rewriting 29 vague or outdated memo descriptions in plain words
  (the description is part of every chunk's title): 20/21/24 on the same 24 questions. That number is optimistic,
  since the descriptions were written knowing the test questions; a fresh question set is needed for a fair figure. The UHD 770 iGPU was tested
  via OpenVINO (1.2 chunks/s, OOM at batch 16) and rejected: slower than 2 CPU threads. Cache `/state/vectors.npz` (by chunk
  hash). Files > 300 KB stay regex-only. Container `mem_limit: 3g`. MCP tool `knowledge_semantic_search`; the UI shows
  "By meaning" results under the exact matches.
- `graph.py`: **topics** with cohesion: a memo takes its MEMORY.md heading, a homelab doc (any `.md` at the top of `homelab/`) the heading named in its frontmatter `topic:` (title from `title:`), other projects' docs their subfolder or file-name family (3+ files, e.g. `FABLE_*`), other notes the topic of most of their links, the rest Louvain communities (networkx, seed 42); the MEMORY.md index itself is not drawn; **insights** for lint: isolated memos and sparse topics. Lint also checks the structure: every memo (except feedback/user) and every homelab doc has `## Stand` and `## Keuzes` (history in `archief/`), and every homelab doc a known `topic:`
  (cohesion < 0.15); **relevance model** for `knowledge_related` and the Connected list: link ×3, shared source ×4,
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

- **Overdue open items:** a memo line `open tot <date>: <what>` is listed by lint as `open_overdue` once the date has passed, appended to the session-start context of its project and listed every week in the Matrix report.
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
  wikilinks, markdown links, doc/skill references and cited sources (layout above, "Web UI layout"); full search
  (server-side, ~20 ms); note preview (marked + DOMPurify) with clickable links and connected notes; lint tab; ingest. See
  "Sign-in, people and admin page" below; UI writes also need an `X-Knowledge-UI` header (CSRF). All JS is vendored with its license in `server/static/vendor/`
  (force-graph 1.52.0 MIT, marked 18.1.1 MIT, DOMPurify 3.4.17 Apache-2.0/MPL-2.0); no CDN.

## Other agents: agy (Antigravity CLI) and Codex (20 Oct 2026)

The MCP server is a plain streamable-HTTP server with a Bearer token, so any MCP client works. Each client gets its own
token from the admin page; agy and Codex have read-only tokens with secrets masked (both talk to cloud models).
One command (`clients/knowledge-agent.py install`) connects agy or Codex: MCP server, the plugin's context, session log and docs mirror through their own hook files, and the house rules (the
memo check and secret guard stay Claude-only; the server refuses known secrets for every client).

| | agy | Codex |
|---|---|---|
| MCP | `agy mcp add --header "Authorization: Bearer $(secret KNOWLEDGE_TOKEN_AGY)" knowledge https://knowledge.to0wnn.com/mcp` → `~/.gemini/config/mcp_config.json` (`serverUrl` + `headers`; the token is stored in plain text there, so `chmod 600`) | `~/.codex/config.toml` `[mcp_servers.knowledge]` with `url` and `http_headers_helper = "~/.local/bin/knowledge-mcp-header KNOWLEDGE_TOKEN_CODEX"`: the token comes from the vault, never from a file; `default_tools_approval_mode = "writes"` |
| Working rules | `~/.gemini/GEMINI.md` (always on; on the VM generated by knowledge-watch, elsewhere a block written by install) | `~/.codex/AGENTS.md` (the same) plus the context hook |
| Setup | `knowledge-agent.py install agy [<VAULT_NAME>]`: checks the token against `/house-rules`, adds the MCP server to `~/.gemini/config/mcp_config.json` (header with the token, chmod 600; left alone when present), writes `~/.gemini/config/hooks.json` (SessionStart → context as `injectSteps` plus the docs mirror; PostToolUse on the CLI's `write_file\|edit_file` (arg `Path`) → docs-sync; Stop → session-log, one entry per turn) and the house rules as a marked block in `~/.gemini/GEMINI.md` | `install codex [<VAULT_NAME>]`: adds `[mcp_servers.knowledge]` with `bearer_token_env_var = "KNOWLEDGE_TOKEN"` to `~/.codex/config.toml` (when absent), writes `~/.codex/hooks.json` (one SessionStart handler, openai/codex#45999; PostToolUse `apply_patch` → docs-sync; PreCompact and SessionEnd (max 3 s) → session-log) and the rules block in `~/.codex/AGENTS.md`. Trust the hooks once with `/hooks` |
| Tokens in hooks | A vault name on the VM, `-` elsewhere (`KNOWLEDGE_TOKEN`; Linux, macOS, Windows). The session log always gets `-`: without `KNOWLEDGE_TOKEN` it posts with `~/.config/knowledge-server/session.env` (`vm-sessions`), so the agy/Codex tokens on the VM stay read-only | same; full paths in the commands because Codex runs hooks with a bare PATH |

Server side for every client: tools carry MCP annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`), and the
first ~500 characters of the instructions stand alone (Codex only shows that part when choosing tools).

## Development rules and skills (21 Oct 2026)

How code is changed, the same for every project and agent: `claude/dev_rules.md` (`~/.claude/gedeeld/dev_rules.md`
on the VM), twelve numbered rules (read first, plan only larger work, root cause with a failing test, measure
before/after, one change with a way back, done = check passed with output, fail loudly, Conventional Commits, English,
docs and Keuzes in the same change, ask before visible actions, review larger changes). It travels like the house
rules: CLAUDE.md imports it, the watcher writes it into GEMINI.md/AGENTS.md, a guest's context and `/house-rules`
carry it. Each project keeps only its own commands and exceptions in AGENTS.md (`## Commands`, `## Project rules`).

The plugin ships the procedures as skills in `plugin/skills/`: `project-setup` (AGENTS.md template, one check
command, git pre-commit hook, README), `plan-change`, `debug-root-cause`, `verify-done`, `review-diff`.
`knowledge-agent.py install` copies them into `~/.agents/skills` for Codex and agy; on the VM they are symlinked into
`~/.claude/skills`. Background and sources: memo `reference_ai_ontwikkelwerkwijze`.

## House rules: one file (20 Oct 2026)

`claude/house_rules.md` (on the VM `~/.claude/gedeeld/house_rules.md`, English, a numbered list) is the only copy.
The shared `CLAUDE.md` imports it with the Claude Code import line `@~/.claude/gedeeld/house_rules.md`; the server
expands that line in the context it sends to other devices (`expand_imports`) and reads the list for the MCP
instructions, the guest context and the help page (`house_rule_items`; `<memory root>` becomes the project's memory
root). The instructions are rebuilt when the watcher reports the file, so no restart is needed; a missing file shows
"THE HOUSE RULES ARE MISSING" everywhere. `knowledge-watch` writes `~/.gemini/GEMINI.md` and `~/.codex/AGENTS.md`
(CLAUDE.md with the import filled in) at start and on every change: agy and Codex do not read imports. In the graph
the file is its own node on its frontmatter `topic:`, linked to the memos it names.

## Sign-in, people and admin page (20 Oct)

- **Sign-in = Authentik**, no tokens in the browser. Traefik router `knowledge-ui` (`/`, `/ui*`,
  `/outpost.goauthentik.io/*`) runs `authentik-forwardauth` and then `knowledge-proxy-secret`, which adds
  `X-Knowledge-Proxy: <secret>`. The server trusts `X-authentik-username` only together with that secret
  (`KNOWLEDGE_PROXY_SECRET`, from `/root/docker/knowledge/config/proxy.env`, Vaultwarden `KNOWLEDGE_PROXY_SECRET`),
  because port 8766 is reachable on the LAN without Traefik. Authentik: provider/application "Knowledge" (pk 43,
  forward_single, session 30 days), access = group "Knowledge".
- **People:** admins = `KNOWLEDGE_ADMINS` (compose). Everyone else needs projects in `users.json` (read or write per
  project); without any they get "no access yet" and appear on the admin page. Graph, search, semantic, related and
  recent only show their projects; lint and ingest are admin-only.
- **Admin page** `/ui/admin`: create a device token (shown once, only the SHA-256 is stored in `/admin/tokens.json`;
  scope = roots, write, redact) or revoke one; grant project access; create a project (roots `<name>` docs and
  `<name>-memory`, stored in `/projects/<name>/` on forge, listed in `/admin/projects.json`). Device tokens also
  count as secrets (redaction, write guard).
- **Self-service (23 Oct 2026):** a person with access opens the same page ("My tokens & projects" in the user menu)
  and sees only two tabs. Tokens: named `<user>.<name>`, `owner` stored, only their own projects, never "All
  projects" or watcher; at every request `token_device` cuts the token to what the owner may read and write *now*
  (access lowered or removed = the token follows). Projects: the maker gets write access (`users.json` `created`).
  Limits on the admin page (Settings): `user_max_tokens` 5, `user_max_projects` 3, `project_max_mb` 20 (every UI
  project; the write that crosses it lands, later writes are refused, deleting still works). Every new token or
  project made by a person posts to Matrix (`KNOWLEDGE_MATRIX_URL/ROOM` from local.env, bot token
  `KNOWLEDGE_MATRIX_TOKEN` in forge `config/proxy.env` = Vaultwarden `MATRIX_BOT_TOKEN`); a failed post shows on the
  System tab. Tokens do not expire (choice Anthony). A person needs access to one project first (admin, People).
- **Searched, not found** (admin page, System tab): every `knowledge_search` without matches and every
  `knowledge_semantic_search` that answered nothing useful is logged: best score below 0.45, or below 0.55 while the
  best note lacks the question's subject words (`lint.query_coverage` < 0.7). Calibrated 21 Oct 2026 on 20 answerable
  and 20 plausible unanswerable questions: score alone caught 8 of 20 gaps, with coverage 15 of 20, no false alarms. Re-measure with `server/calibrate_gaps.py`
  (exit 1 on a false alarm) after changing the model or the thresholds. Logged to
  `gaps.log` next to the audit log, grouped per query with count and devices: the memos that are missing. The UI search and the plugin hook are not logged
  (typing and automatic prompts would be noise).
- `/admin` = forge `/root/docker/knowledge/admin` (rw, 0700, uid 1000); `/config` stays read-only (secrets.json).
- **MCP and plugin endpoints** keep device tokens: from outside router `knowledge` (priority 100) with `rate-limit`, from the LAN router `knowledge-lan` (priority 150, `ClientIP(10.0.0.0/8)`) without a limit; public since 20 Oct. Traefik config lives in the homelab Ansible role `traefik_config`.

## Agents, PA, history and session log (18 Oct)

- **Scoped tokens** (admin page: tick roots, untick write, tick "mask secrets"): limited to some roots, and every known
  secret is masked on the way out (`‹secret›`), also inside `user/pass`, `KEY=value`, tables and "password: …".
  The server only holds SHA-256 hashes of the secrets (`token.py push-secrets` → `/config/secrets.json`), and scoped
  tokens get no web UI. Tokens: `pa-anthony` (memory + homelab, redacted; `mcp_servers.knowledge` with a
  `headers:` Authorization in `/home/assistent/.hermes/config.yaml`, tools limited to search/read/list/related).
  The second PA has no access (family scope).
- **History:** every version of every Markdown file is kept in `/state/history/<rel with __>/` (who: device name, or
  VM for edits on the VM; first change keeps the baseline; 200 versions per file). MCP `knowledge_history`, UI button
  "History" with diff and restore.
- **Session log:** `plugin/hooks/session-log.py` on SessionEnd and PreCompact (long tmux sessions rarely end) posts the
  latest prompts, changed files and Claude's last answer to `POST /sessions`; the server redacts and appends to
  `<project memory>/sessions/<YYYY-MM>.md`. It remembers per session how far it logged. On the VM via
  `~/.claude/settings.json` + `~/.config/knowledge-server/session.env` (token `vm-sessions`, memory roots only).

## Secrets (Vaultwarden) — `secrets/`

Notes never hold secret values; they say `‹secret:NAME›`. The values live in a Vaultwarden/Bitwarden **organisation**
("Homelab", collection "Homelab") shared between the owner's account and a dedicated automation account, so the owner
sees and edits them in the normal Bitwarden apps and the automation cannot see the owner's personal vault.

- `secrets/secret` (installed as `~/.local/bin/secret`): `secret NAME` (read, ~50 ms), `secret ls`, `secret set NAME`
  (stdin), `secret rm NAME`, `secret tsv`, `secret scrub [--apply] FILE...` (replace vault values in notes by
  placeholders; values that are dictionary words such as `sunflower` only on lines about logging in),
  `secret import-tsv FILE`. Reads use **rbw** (agent keeps the keys in memory); writes use the official **bw** CLI,
  because rbw cannot write to an organisation.
- **Always unlocked:** the automation account's master password is encrypted with `systemd-creds --user`
  (`~/.config/knowledge-server/vw-master.cred`); `secrets/rbw-pinentry-creds` hands it to rbw, so the vault unlocks
  itself after a lock or reboot (tested: fresh Claude session with a locked vault logged in over SSH using
  `sshpass -p "$(secret NAME)"` without printing the value).
- **Never back in:** the server refuses MCP/UI writes that contain a known secret; on the VM the PreToolUse hook
  `plugin/hooks/secret-guard.py` (Edit|Write) does the same for native edits. Both compare SHA-256 hashes
  (`token.py push-secrets` writes `/config/secrets.json` on forge and `~/.config/knowledge-server/secret-hashes.json`).
  Run `push-secrets` after every `secret set`.
- Why not Bitwarden Secrets Manager or `bw serve`: Vaultwarden does not implement Secrets Manager (Bitwarden-licensed),
  and `bw serve` exposes an unauthenticated local API.

## A guest project (someone else's machine, e.g. Windows)

The guest's own folder stays the source of the code and docs; the knowledge server holds the project's memory and a
read-only mirror of its Markdown, and the guest's sessions follow the same house rules.

On the server (admin page): create the project (`<name>` + `<name>-memory`), a **write token limited to those two
roots** with secrets masked, and, for the web UI, add the guest's Authentik username under People with write on that
project only. A token limited to one project always works in that project, whatever the folder on the guest's machine
is called, and its session context holds this project and the house rules only.

On the guest's machine:
1. Windows: install Git for Windows (Claude Code runs hooks in Git Bash) and Python 3 (python.org, with the `py`
   launcher). Hooks run Python through `hooks/py.sh` (`python3`, `py` or `python`).
2. In the project folder, a file `.knowledge-project` with the project name (one line). Only below such a folder are
   `.md` files mirrored: every one at session start (files deleted there are removed on the server, a copy is kept),
   and each one Claude edits right after the edit. Code is never sent; dot-folders and venvs are skipped.
3. Claude Code: `claude plugin marketplace add To0wnn/knowledge`, `claude plugin install knowledge@knowledge --scope user`,
   `claude plugin configure knowledge@knowledge` (URL and the token), and `"autoMemoryEnabled": false` in
   `~/.claude/settings.json` (memory lives on the server).
4. Check: a new session in the folder starts with "knowledge (project <name>)"; ask it to search the knowledge.

## Offline copy of the memory (21 Oct 2026)

Every device with a write token (Claude plugin, agy/Codex via `knowledge-agent.py`) keeps
`~/.knowledge/mirror/<memory root>/`: the memos and their `archief/`, not the session logs. `memory_mirror.py` syncs
in the background at session start (and end, Claude), so a session never waits for it:

- up first: only files this device wrote or changed since the last sync (`POST /mirror/push`, with the version it had);
- then down: the device sends the versions it has, the server returns only what differs (`POST /mirror`);
- a memo changed on both sides is never overwritten: the offline text lands next to it as
  `<name>.offline-<time>.md`, and `knowledge_lint` (`offline_conflict`) asks to merge it;
- server down: the startup context is the cached one of the last online session, plus where the copy is and that new
  memos go there by the house rules; they go up at the next start. `~/.knowledge/mirror/last-sync.log` only exists
  when something needs a look (conflict, refusal such as a secret, failed sync).

A read-only token gets no copy. Tested on the Framework (188 memos, a memo written there reached the server, a
deletion on the server reached the copy) and in `test_server.py` (offline edit, conflict, lost file); not on Windows.

## Client test on Windows, macOS and Linux (CI, 21 Oct 2026)

`.github/workflows/clients.yml` runs `tests/clients_e2e.py` on every push, on real Windows, macOS and Linux runners.
It starts the real server on the runner (fake embeddings) and drives the client side the way the tools do, under
awkward conditions on purpose: a home folder with a space and an "é", a UTF-16 `.knowledge-project` (PowerShell 5),
text with → and ✅, the plugin and `knowledge-agent.py` under a path with a space.

- Claude Code plugin: every command in `hooks.json` through Git Bash (Windows) or sh, with `${CLAUDE_PLUGIN_ROOT}`
  both as a variable and substituted; context, docs mirror, secret guard, session log, offline copy.
- agy and Codex: `knowledge-agent.py install` twice, then every generated hook through PowerShell and
  `cmd /d /s /c "…"` (Windows) or sh; context JSON, a wrong token stops with 401.
- Offline: server down → offline context within 10,000 characters; back online → the offline memo goes up.
- The real `claude plugin install` from this repo and `codex mcp list` (CI fails if the CLIs are missing).

Run it locally: `uv run --project server python tests/clients_e2e.py`. Windows notes: `uvloop` is skipped there,
`extract.py` refuses to extract without the Unix `resource` limits, Codex finds its folder via the OS (the test
and `knowledge-agent.py` honour `CODEX_HOME`).

## Local addresses

The addresses of an installation (docker host, NFS host, LAN URL, Matrix, proxy) live in `local.env`, which is not in
git: copy `local.env.example`. `deploy.sh` reads it and copies it to `server/.env` for docker compose; the systemd units
read it with `EnvironmentFile`; `token.py` and `tests/ui_smoke.py` load it themselves.

## Install on a device (once)

1. Create a token on the admin page (`/ui/admin` → New token) and store it in the vault
   (`printf %s '<token>' | secret set KNOWLEDGE_TOKEN_<DEVICE>`).
2. Works from anywhere since 20 Oct 2026 (router `knowledge` is public, Traefik `rate-limit` 100/s per IP from outside; a token is
   256 bits of randomness, so guessing is not a risk).
3. In Claude Code on that device (no GitHub login needed when the device has an SSH key for the VM; the
   marketplace is then cloned straight from the VM's repo):
   ```
   claude plugin marketplace add To0wnn/knowledge
   claude plugin install knowledge@knowledge --scope user
   printf '{"url":"https://knowledge.to0wnn.com","token":"%s"}' "$TOKEN" | claude plugin configure knowledge@knowledge --values-stdin
   ```
   The token goes in over stdin, so it stays out of shell history. Set `"autoUpdate": true` on the marketplace in
   `extraKnownMarketplaces`; installed copies only update when `plugin/.claude-plugin/plugin.json` gets a new `version`.
4. Turn off local auto memory on that device so it does not build a second memory:
   `"autoMemoryEnabled": false` in `~/.claude/settings.json`.
5. Start a session: the first lines show `[knowledge context part 1/2]`; `/mcp` lists `knowledge`.

Do **not** install the plugin on the VM itself: sessions there read the files natively. They do get the MCP server (user scope, token
`vm-sessions`) for search across all projects: `claude mcp add --scope user --transport http knowledge
https://knowledge.to0wnn.com/mcp --header "Authorization: Bearer <token>"`. A small side folder that is no project
of its own shares the memory of the project it belongs to via `autoMemoryDirectory` in its
`.claude/settings.local.json`; the weekly report lists memory dirs with memos that the server does not serve.

## Watcher on the VM (option A)

`ln -s ~/knowledge/watcher/knowledge-watch.service ~/.config/systemd/user/ && systemctl --user enable --now knowledge-watch`
(needs `inotify-tools`, linger on, and `~/.config/knowledge-server/watcher.token` = the `vm-watcher` token).
With option B the watcher runs wherever the files live (forge) instead.

## Weekly lint report (VM)

`watcher/lint-report.py` sends the lint findings that are **new since the last run** to Matrix (room Homelab Alerts),
silent when nothing is new. Timer: `ln -s ~/knowledge/watcher/lint-report.{service,timer} ~/.config/systemd/user/ &&
systemctl --user enable --now lint-report.timer` (Monday 08:00). Needs `~/.config/knowledge-server/report.token`
(token `vm-lint-report`, read-only) and `secret MATRIX_BOT_TOKEN`. State in
`~/.local/state/knowledge-lint-report.json`; `--init` stores a baseline without sending, `--dry-run` prints.
Review cycle (like Guru's verification): lint check `review_due` lists memos with IP addresses or x.y.z versions whose
newest `(gecontroleerd <date>)` mark (without one: the newest date in the memo; future dates do not count) is older
than 45 days (admin setting `review_after_days`; measured 21 Oct 2026: 8 of 68 such memos). The weekly report always
lists the 5 oldest of them, also when nothing else is new, so a few get checked each week.

When `knowledge_write` creates a memo, the answer lists existing memos in the same memory root with a similar
meaning (score ≥ 0.72, calibrated on embeddinggemma), so the writer can merge instead of duplicating.

## Checks outside the server

- **UI smoke test** (`tests/ui_smoke.py`, headless system Chrome via Playwright, read-only against the live server):
  click zoom, ring, exact release, deselect, phone tap and Read chip. Run after UI changes:
  `systemd-run --user --scope -q -p MemoryMax=3G ~/.local/bin/uv run -q --with playwright python tests/ui_smoke.py`.
- **New-memo guard on the VM** (`memory-check.py --pre`, PreToolUse `Write` in `~/.claude/settings.json`, since 21 Oct 2026):
  a session that writes a NEW memo without frontmatter or `## Stand` / `## Keuzes` is refused before the file exists,
  with the same rule the server applies (`lint.new_memo_lacks`). Existing memos and other files pass.
- **Memo hook on the VM** (`plugin/hooks/memory-check.py`, PostToolUse `Edit|Write|MultiEdit` in `~/.claude/settings.json`,
  timeout 15 s): after a session edits a memo directly, it runs lint on that memory root and tells Claude what concerns
  that file, plus the similar-memo hint for a new memo. Advisory only (`additionalContext`), never blocks.

## Deploy / update the server

`./deploy.sh` runs the self-test, rsyncs `server/` to the forge stack and rebuilds. Bump versions in
`pyproject.toml` (then `uv lock`) and the `FROM`/uv tags in the Dockerfile deliberately.

## Option B (later): files on forge

1. Stop the stack, copy the three trees to forge (e.g. `/root/docker/knowledge/data/{memory,homelab,claude}`).
2. In `compose.yaml` replace the NFS volumes with those bind mounts (owner 1000).
3. On the VM: remove `/etc/exports.d/knowledge.exports`, install the plugin there too and set
   `autoMemoryEnabled: false` (the VM then also works through the server).
The server itself needs no change (paths come from `KNOWLEDGE_*` environment variables).
