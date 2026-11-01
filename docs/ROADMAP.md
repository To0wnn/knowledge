# Roadmap

Open first-run conveniences found while rewriting the README (1 Nov 2026). Not promises, just the known gaps.

- [ ] **First token without the web UI.** Today the first device token can only be made on the admin page
  (`/ui/admin`); `token.py add` says so and exits. A one-liner such as
  `docker compose exec knowledge python server.py new-token <name>`, or a token printed once on first start, would
  let the quickstart run without a browser.
- [ ] **Notifications in the one-machine `compose.yaml`.** `KNOWLEDGE_MATRIX_URL`, `KNOWLEDGE_MATRIX_ROOM` and
  `KNOWLEDGE_MATRIX_TOKEN` (and `KNOWLEDGE_RERANK_MODEL` for the duplicate check) are not passed through from `.env`,
  so they need a hand edit of `compose.yaml`.
- [ ] **Notification channels besides Matrix.** Matrix is the only one; ntfy or Web Push would suit phone users who
  run no homeserver.
- [ ] **Generic proxy user header.** Proxy sign-in reads `X-Authentik-Username`; other proxies (oauth2-proxy,
  Pomerium, Caddy) send `Remote-User` or `X-Forwarded-User` and need a header rewrite.
- [ ] **`compose.yaml` comment.** It points to "README, Web sign-in"; that text now lives in
  [server.md](server.md#settings-in-env).
- [ ] **MCP registry entry.** `server.json` still names version 0.12.5.
- [ ] **Screenshots.** The README lists the images still to add.
