# Run the server

Moved from the README; [back to the README](../README.md). The README has the short version; this page has the details.

## With Docker

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

Requirements: Docker and about 1.5 GB RAM (the embedding model takes about 400 MB, and a duplicate check briefly loads
a 1.1 GB reranker when one is set up; the model ships in the image, nothing is fetched at runtime).

## Settings in `.env`

The one-machine `compose.yaml` reads these from `.env` (checked against `compose.yaml` and `server/server.py`):

| Variable | Default | What it does |
|---|---|---|
| `KNOWLEDGE_UI_PASSWORD` | none, required | The password for the one admin web sign-in |
| `KNOWLEDGE_ALLOWED_HOSTS` | `localhost:8766,127.0.0.1:8766` | `host:port` names clients may use; add yours for other machines |
| `KNOWLEDGE_PUBLIC_URL` | empty | The address people use; named in the agents' instructions and in notice links |
| `KNOWLEDGE_LANGUAGE` | `en` | `en` or `nl`: memo section names and the words the server writes |
| `KNOWLEDGE_PORT` | `8766` | The port on the Docker host |
| `KNOWLEDGE_UID`, `KNOWLEDGE_GID` | `1000` | The owner of `./data` |
| `TZ` | `UTC` | Dates in source file names and the audit log |
| `KNOWLEDGE_UI_USER` | `admin` | The user name of the password sign-in |
| `KNOWLEDGE_PROXY_SECRET`, `KNOWLEDGE_ADMINS`, `KNOWLEDGE_PROXY_IPS` | empty, empty, `127.0.0.1` | Sign-in through a reverse proxy (below) |
| `KNOWLEDGE_PROXY_USER_HEADER` | `X-Authentik-Username` | The header the proxy puts the user name in (below) |
| `KNOWLEDGE_MATRIX_URL`, `KNOWLEDGE_MATRIX_ROOM`, `KNOWLEDGE_MATRIX_TOKEN` | empty | Notices to Matrix (below); the room can also be set per admin on Settings |
| `KNOWLEDGE_NTFY_URL`, `KNOWLEDGE_NTFY_TOKEN` | empty | Notices to an ntfy topic (below) |
| `KNOWLEDGE_TOOLS_MEMO` | empty | The memo a tool request's draft task goes into (Admin › Tools & skills › Draft it), e.g. `knowledge-memory/project_tools.md`; empty: requests can only be marked added or declined |
| `KNOWLEDGE_TOOLS_DRAFTER` | `Claude` | The agent that owns such a draft task |
| `KNOWLEDGE_GITHUB_TOKEN` | empty | A fine-grained GitHub token with read-only access to public repositories only: the companions of the admins' own devices use it for tool add-on downloads (GitHub times anonymous callers out at busy moments). Never a token that can write |
| `KNOWLEDGE_RERANK_MODEL` | empty | Duplicate check: a reranker folder under `./data` (`server/export_reranker.sh`), e.g. `/data/models/reranker` |

Sign-in through a reverse proxy: the proxy sends the shared secret in the header `X-Knowledge-Proxy` and the user name
in `KNOWLEDGE_PROXY_USER_HEADER`: `X-Authentik-Username` (Authentik's forward auth, the default), `Remote-User`
(Authelia, Caddy), `X-Auth-Request-User` or `X-Forwarded-User` (oauth2-proxy). The user header counts only together
with the secret, and only the one configured name: a proxy overwrites its own header, but passes others from the
browser through untouched. `KNOWLEDGE_ADMINS` names the users with full access, other users who sign in are listed on
the admin page to be given projects. `KNOWLEDGE_PROXY_IPS` are the addresses trusted for `X-Forwarded-*` headers.

## How it works

- **Server** (`server/`): Python, stateless streamable-HTTP MCP plus the web UI, in one Docker container. All text sits
  in memory, so search over a few thousand files takes tens of milliseconds. A watcher posts changed paths for instant
  updates; a stat-only rescan every 60 s is the safety net. Embeddings (for semantic search and related notes) run
  locally with ONNX; no data leaves the machine.
- **Tool arguments for weaker models:** agy shows its model only the tool names, and smaller models guess argument
  names (`file_path`, `new_text`, `start_line`). The server maps the common guesses onto the real names in one place
  (`ARG_GUESSES` in `server.py`, `end_line` becomes `limit`), and a failed call names the tool's arguments and the
  unknown ones sent. The start context lists the exact names of the most used tools (`tool_calls`). Measure the effect
  with `tests/tool_errors_by_model.py` (failed knowledge calls per model from the chat logs).
- **Keywords in the regex search:** weaker models send keyword lists to `knowledge_search` ("icing engine start
  pushback"), which as a regex matches no line; until 13 Nov 2026 that was half of all empty searches, and the agent
  then grepped local files instead. Plain words (spaces, no regex characters) that match nothing now return
  `knowledge_semantic_search`'s hits under the `(no matches)` line; only that search logs a knowledge gap then.
- **Tools loaded at once in Claude Code:** Claude Code defers MCP tools behind ToolSearch, and every session first
  loaded the same few (3 to 4 ToolSearch calls per chat; Haiku repeated one select up to 6 times). The five tools every
  session starts with (`knowledge_chat_title`, `_read`, `_search`, `_semantic_search`, `_edit`) carry
  `_meta: {"anthropic/alwaysLoad": true}` (`ALWAYS_LOAD` in `server.py`); the rest stay deferred. Other agents ignore it.
- **Clients** (`plugin/`): one folder that installs as a plugin in Claude Code, Codex and agy. It adds the MCP server,
  the session-start hook, a session log and a docs mirror for guest projects. Codex and agy reach the server through a
  small stdio relay, so the token never sits in a config file. `clients/knowledge-agent.py` sets everything up.
- **Tokens:** one long random token per device, kept in the OS keyring (Linux Secret Service, macOS Keychain, Windows
  Credential Manager). Tokens can be read-only, masked, limited to one project, or allowed to sync shared skills.
  Shared skills land in `~/.claude/skills`; after every sync each skill there (also one made on the device) gets a copy
  in `~/.agents/skills` (Codex) and `~/.gemini/config/skills` (agy's global skills), marked `.from-claude-skills`. Only marked copies are updated or
  removed; edit a skill in `~/.claude/skills`, an edit in a copy is overwritten.
- **Web sign-in:** a password for one admin (`KNOWLEDGE_UI_PASSWORD`, a signed cookie for 30 days; at most five wrong
  tries a minute), or, for several people, a reverse proxy with forward auth (for example Authentik) that sets the user
  header and a shared secret (`KNOWLEDGE_PROXY_SECRET`).

## Back up and restore

Everything lives in `./data` and `.env`. Copy both, with the container stopped or from a filesystem snapshot:

```sh
docker compose stop knowledge && tar czf knowledge-$(date +%F).tgz data .env && docker compose start knowledge
```

| In `./data` | What | Without it |
|---|---|---|
| `memory/`, `docs/`, `shared/`, `projects/` | the notes | lost |
| `admin/` | tokens, people, projects, chats, the sign-in key | every device and person starts over |
| `state/` | audit log, version history, trash | no undo |
| `state/vectors.npz`, `models/`, `voice/` | search index, optional models | rebuilt or fetched again; may be left out |

To restore, unpack both next to `compose.yaml` and `docker compose up -d`. The same tokens work and the search index
rebuilds itself. `tests/backup_restore.py`, part of the check, does this for real on every commit. An untested backup is
a hope, so restore yours somewhere once as well.

## Health checks

`/health` answers `ok` while the process lives. `/health?ready=1` is 503 until the indexes are loaded.
`/health?disk=1` is 503 while the note folders do not answer: point your monitor at that one. A network share that hangs
would otherwise make every read wait as long as the file server is away. After 10 s of silence a request that needs the
notes gets a 503 at once, with one notice each way, until the folders answer again. A session that starts meanwhile uses its
offline copy, if it has one; a tool call in a running session gets the error. Chats and machines keep working.
A write already under way waits on the hung mount and lands when the folders are back; only that request waits, because
async handlers leave file access to a thread (`test_server.py` checks this). A failing share never counts as a deletion:
the index keeps its text and no history versions appear.

## Notifications

Notices (a Needs-you item, a stalled or idle task, a worker waiting for its usage limit, a risky YOLO action, shared
folders that stop answering) go to:

- Matrix: to each admin's homeserver, room and bot token from the Settings page, a field left empty taken from `.env`
  (`KNOWLEDGE_MATRIX_URL`, `_ROOM`, `_TOKEN`). Another homeserver than `.env`'s needs the admin's own token (the `.env`
  bot's token is never sent elsewhere); that token is kept apart in `matrix_tokens.json` (0600) and never shown again.
  Admins on the same room get one message; Settings has a button for a test message;
- an [ntfy](https://ntfy.sh) topic, when `KNOWLEDGE_NTFY_URL` is set to the topic's full URL (`https://ntfy.example.com/knowledge`),
  plus `KNOWLEDGE_NTFY_TOKEN` when the topic needs one. Take a long topic name on a public server: the notices name
  your projects and tasks. A self-hosted server reaches an iPhone through a relay (`upstream-base-url`, see the
  [ntfy docs](https://docs.ntfy.sh/config/)).

Either or both; `compose.yaml` passes them on. A failed message shows on the admin page, System. In the web UI, Settings,
"Tell me in this browser" adds desktop notifications for new Needs-you items while the tab is open; a phone gets none of
those, so there Matrix or ntfy is the only push. Details of the Needs-you notices: [chats.md](chats.md).

The trust model in one place: [security.md](security.md).
