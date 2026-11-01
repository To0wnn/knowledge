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

Requirements: Docker and about 2 GB RAM (the embedding model is 1.2 GB; it ships in the image, nothing is fetched at
runtime).

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
| `KNOWLEDGE_PROXY_SECRET`, `KNOWLEDGE_ADMINS`, `KNOWLEDGE_PROXY_IPS` | empty, empty, `127.0.0.1` | Sign-in through a reverse proxy (below) |
| `KNOWLEDGE_PROXY_USER_HEADER` | `X-Authentik-Username` | The header the proxy puts the user name in (below) |

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
- **Clients** (`plugin/`): one folder that installs as a plugin in Claude Code, Codex and agy. It adds the MCP server,
  the session-start hook, a session log and a docs mirror for guest projects. Codex and agy reach the server through a
  small stdio relay, so the token never sits in a config file. `clients/knowledge-agent.py` sets everything up.
- **Tokens:** one long random token per device, kept in the OS keyring (Linux Secret Service, macOS Keychain, Windows
  Credential Manager). Tokens can be read-only, masked, limited to one project, or allowed to sync shared skills.
- **Web sign-in:** a password for one admin (`KNOWLEDGE_UI_PASSWORD`, a signed cookie for 30 days; at most five wrong
  tries a minute), or, for several people, a reverse proxy with forward auth (for example Authentik) that sets the user
  header and a shared secret (`KNOWLEDGE_PROXY_SECRET`).

## Notifications

Notices (a Needs-you item, a stalled or idle task, a worker waiting for its usage limit, a risky YOLO action) go to
one Matrix room when `KNOWLEDGE_MATRIX_URL`, `KNOWLEDGE_MATRIX_ROOM` and `KNOWLEDGE_MATRIX_TOKEN` are set; Matrix is
the only channel. The one-machine `compose.yaml` does not pass these variables yet: add them under `environment:`
(see [ROADMAP.md](ROADMAP.md)). Details of the Needs-you notices: [chats.md](chats.md).

The trust model in one place: [security.md](security.md).
