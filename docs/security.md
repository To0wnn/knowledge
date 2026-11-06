# Security model

The trust model in one place, with links to the pages that have the details. Nothing here is new: each point is
described in full on the linked page.

- **Tokens.** One long random token per device, kept in the OS keyring; the server stores only hashes. A token can be
  read-only, masked, limited to one project (a guest), or allowed to sync shared skills. Only a token that may write
  a project's memos takes task leases. [server.md](server.md#how-it-works), [tasks.md](tasks.md#task-lines-and-leases)
- **Web sign-in.** One admin with a password (signed cookie, at most five wrong tries a minute), or several people
  through a reverse proxy with forward auth and a shared secret. Use HTTPS for anything beyond one machine: over plain
  HTTP the password and tokens cross the network readable. [server.md](server.md)
- **Who may start a task.** A task line starts by itself only when its writer is trusted for that project; otherwise
  the owner or an admin approves it. Text by an untrusted writer reaches agents marked as data, not instructions.
  [tasks.md](tasks.md#task-lines-and-leases), [companion.md](companion.md#yolo-safety-without-blocking)
- **Whose words.** A note someone else wrote reaches your agents as data, and semantic search shows your own notes first.
  A token that went wrong: revoke it, then undo what it changed, per file, on the admin page. Nothing later is
  overwritten. [memory.md](memory.md#whose-words-a-note-holds)
- **Whose machine.** A machine belongs to the person whose token first reported it; only its owner sees and steers it.
  Admin-made tokens count as the admins' only when marked "My own device".
  [companion.md](companion.md#machines), [companion.md](companion.md#the-admins-own-devices)
- **Remote control per token.** A switch per device token ("agents", default on) says whether its pc may be steered
  from the web UI at all; off, the server refuses chats, workers, remote actions and approvals for it (the page only
  follows). [companion.md](companion.md#agents-remote-control-per-token)
- **No remote shell.** Remote control is a fixed list of actions the companion runs with its own code; free text
  reaches a pc only as a chat message to an agent, which runs with that agent's own permissions.
  [companion.md](companion.md#remote-control)
- **Narrow by default.** Without YOLO, workers and chats may edit their folder, use the knowledge tools and run
  `git status/add/commit` and (Claude) `git diff`/`git log` in fixed forms only, never with free arguments
  (`--output=<file>` writes anywhere); a chat asks for anything else (Allow once, Allow for this chat, Deny), git runs
  without hooks or fsmonitor. YOLO is per agent and pc, needs the word YOLO typed, and is watched, never blocked.
  [tasks.md](tasks.md#workers), [chats.md](chats.md), [companion.md](companion.md#yolo-safety-without-blocking)
- **The local companion.** Listens on `127.0.0.1:47321` only, answers only its own page and the web UI's origin, and
  needs a key file readable by you only for changes. [companion.md](companion.md#companion-this-pc)
- **Secrets.** Known secret values are masked in every answer and refused in every write and file edit; text in
  `<private>...</private>` is dropped before anything is stored. [memory.md](memory.md#offline-copy-secrets-duplicates-private-spans),
  [clients.md](clients.md#what-the-plugin-does-in-every-session)
- **Private by design.** Embeddings and reranking run locally; the model ships in the image, nothing is fetched at
  runtime. [memory.md](memory.md#search-quality), [server.md](server.md)
- **Audit.** Every remote action, approval question and answer is logged with who and when, in the companion's
  activity log and the server's audit log. [chats.md](chats.md), [companion.md](companion.md#remote-control)
