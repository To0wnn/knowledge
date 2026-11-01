# Inbox, chats and approvals

Moved from the README; [back to the README](../README.md). The web UI's attention board and the chats with agents on your own pcs, including how a tool call is approved
from a phone. The pc side runs in the companion ([companion.md](companion.md)).

## Agents: Inbox and Chats

- **Agents: Inbox and Chats (chat with your agents):** the web UI's **Inbox** (key `A`, the start page) is the
  **attention board** (`GET /ui/api/board`); its sidebar badge counts what needs you, on every view and in every
  project. The header filters Inbox, Chats and Tasks to one project ("show all" says how many Needs-you items the
  filter hides) and shows when the fullest usage window resets. Its columns, each grouped per project with a count: **Needs you** (a chat whose agent
  answered and you have not opened since, a refused tool call or error in its last turn, a failed worker of the last
  week with the companion's reason (`why`), a worker whose pc went offline ("pc offline since <time>", no Stop), a
  task line waiting for your approval, a task woken that nobody took, a result or hand-over to a person (`result`:
  read it, then remove the line; it never starts anything), a task whose "start after" task was cancelled
  (`blocked`)), **Running** (chat turns, workers, task
  lines a session holds), **Open** (task lines for an agent, with where they will run and a "Start on <pc>" button
  per pc that may take it) and **Done (24 h)** (chats read, workers ended, results handed back). Opening a chat marks
  it read (chats from before 0.17 count as read; a turn you stopped yourself and replies older than a week do not
  need you). Dismiss takes a Needs-you item off your own board until it changes (stored per person on the server,
  `POST /ui/api/board/dismiss`). With Matrix configured (`KNOWLEDGE_MATRIX_URL/ROOM/TOKEN`), a new Needs-you item on
  an admin's board gives one Matrix message in that room, with a link to the item itself,
  `<KNOWLEDGE_PUBLIC_URL>/ui#/inbox/<key>` (every board item has a stable `key`): once per item and version (the key and signature Dismiss uses), at most
  `needs_per_hour` an hour (admin page, Settings; 0 = off, default 6; more at once give one summary, the rest wait
  for room), and none while an admin has the board open (polled in the last 2 minutes: they see it there). A woken
  task nobody took keeps its own notice, now with the same link. Non-admins get none: the room is the admin's. A card opens its chat (Esc or Back returns), a task its memo line, a worker how to look
  at it; Approve, Start and Stop act in place. A phone shows the columns one under the other, Done folded to one line. Chat cards and workers
  carry a **git button** (`task/KN-12 · ●3 · ↑2↓0`: branch, changed files, commits ahead/behind `main`/`master`, or
  the upstream on main itself), which the companion counts once a round per folder with `GIT_OPTIONAL_LOCKS=0` and
  sends in its report (the linked folder only, `-- .`, also inside a bigger repo); a tap asks that pc for the diff (`POST /ui/api/git/diff`, handed to the companion with the chat
  messages, answered on `POST /machine/chats/diff`): the stat, untracked files by name and `git diff <merge-base>`,
  capped at 200 kB, shown as text. A running worker's card also gets its `progress` from the companion's report:
  newest commit subject, commits, changed files and the last 3 log lines (secrets masked on the pc and again on the
  server). A
  chat is a conversation with Claude Code, Codex or agy on one of your own pcs, in a folder linked to a project there,
  as in a terminal: the answer streams in (text, plus one line per tool call or refused call), the next message
  continues the same conversation, Stop ends a running turn and drops the waiting line. A message sent while the agent answers waits in line (up to 5 per chat, button "Queue") and goes in when the turn ends; with messages waiting and an empty box the button turns red "Force": it stops the turn but keeps the line, so the first waiting message goes in at once. Enter is a new line, Ctrl/Cmd+Enter presses the button; two lists under the box pick the model and effort from the next turn on (default: the agent's own); they hold for the chat until changed. The companion reports per agent what it offers (Claude Code's aliases fable/opus/sonnet/haiku, `codex debug models` with visibility list, `agy models` until an ACP session ran, then the list ACP itself offers, which both ways accept; asked in the background, again after 6 h, after 10 min when empty; no probe session, which would leave an empty agy conversation for `agy --continue`), the server refuses anything else, and the turn gets it as flags (`--model`/`--effort`, Codex `-m` and `-c model_reasoning_effort`) or, over JSON-RPC, in Codex's `turn/start` and agy's `session/set_model`. agy has no effort list: Gemini's model names carry the level and its other models refuse `--effort`. A model the agent refuses fails the turn with the agent's own message; the page sends a choice only when it was changed in that box (a saved model the pc no longer lists stays saved, not turned into Default unasked); messages waiting in line run with the chat's choice when their turn starts; Codex keeps a `turn/start` override for the thread, so Default is named there (`config/read`, else `model/list` isDefault); **Chats** lists them beside the open one (`#/chats/<pc>/<chat>`). The server only relays, pull only like the actions: the UI posts a message
  (`POST /ui/api/chat/send`), the pc's companion fetches it (`GET /machine/chats`, every ~2 s while a chat is active,
  else ~10 s), runs one headless turn of the agent with the message on stdin (never on a command line), the first turn
  a new conversation and every later one the agent's own continuation, and posts the output back in small batches
  (`POST /machine/chats/events`). With YOLO on for that agent there, the turn runs with the agent's own YOLO flag
  (`claude -p --resume <session>`, `codex exec resume <thread>`, `agy --conversation <id>`). Without YOLO it starts
  with the same narrow set a worker has (edits in the folder, the knowledge tools, `git status/add/commit`, Claude also fixed `git diff`/`git log` forms)
  and **asks you for anything else**, each agent its own native way: Claude through its permission prompt tool
  (`--permission-prompt-tool`, a small stdio MCP server of the plugin, `approval_mcp.py`, that asks the companion on
  127.0.0.1 with its local key), Codex over `codex app-server` (workspace-write sandbox, approval policy on-request,
  `thread/resume` continues the thread; `codex exec` never asks), agy over `agy acp` (`session/load` continues the
  conversation; `agy -p` denies by itself). The turn pauses; the companion asks the server
  (`POST /machine/chats/ask`), and the chat card shows the request (tool, command or path, the agent's reason) with
  **Allow once**, **Allow for this chat** and **Deny**; each waiting approval is also an item under **Needs you** on
  the board, answered there or in the chat (`POST /ui/api/chat/approve`, only the machine's owner). The answer goes back
  with the companion's next poll (`answers` in `GET /machine/chats`) and the agent continues; denied, it is told so
  and goes on without. No answer within 30 minutes (admin page, Settings, `chat_approval_minutes`) counts as denied
  and the chat says so; a turn that ends or is stopped withdraws what still waits. The card shows the whole request (a long one folded,
  up to 4000 characters; longer can only be allowed once). "Allow for this chat" allows exactly the same call (the
  same full input, compared by hash) again for the rest of the chat without asking; `git push`, anything that names a
  path outside the chat's folder (relative, `~` and `$HOME` paths resolved), Codex's file changes outside the folder or
  without named paths, and Codex's requests for wider sandbox rights can only be allowed once. Codex is judged on the
  command it will run, never on its parsed summary of it; a Codex file change and fetching a URL are never allowed
  without asking. git's own files (`.git/`, `.gitattributes`, `.gitmodules`) are never written without asking, and
  only once (agy workers get a deny rule for them; Claude protects them itself); agy and Codex chats run no shell
  command, git included, without asking; and every git a chat turn or worker without YOLO starts runs with
  `core.fsmonitor=false` and `core.hooksPath=/dev/null` (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_NOSYSTEM=1`), so a
  config an agent wrote cannot run with an allowed git command. On the board an approval whose request is longer than
  its one-line title can only be denied there (Allow is in its chat, where it shows whole); bidi control characters in
  an approval show as `<U+202E>`. The machine's token is checked against today's token rules on every question and answer. Every question and answer, with who answered, is in the companion's
  activity log and the server's audit log (`chat-ask`, `chat-approve`). Codex without YOLO still cannot commit in a
  chat unless you allow it: the folder is a main checkout, and Codex keeps its `.git` read-only (opening it would let
  a sandboxed run change git hooks and config). A chat turn says it is one (`KNOWLEDGE_CHAT`, header `X-Knowledge-Chat`): like a worker it
  hears no task lists or "start your tasks", takes no lease, task or notice, and agy is not asked to register. Only the machine's owner sees or posts (admins
  too only on their own machines); a chat needs its pc online and says so. One file per chat under the state
  directory (`chats/<machine>/`), at most 50 chats per machine (the oldest go) and 1 MB per chat (the oldest output
  goes). Every turn, stop and failure is in the companion's activity log; a failure also shows in the chat.
- **Workers in the chat view:** Follow on a worker opens its transcript as a chat card
  (`#/chats/worker/<project>/<ID>`), with Steer and Take over for its owner; a worker taken over continues as an
  ordinary chat in its worktree. How it works: [tasks.md](tasks.md#follow-steer-and-take-over-a-worker).
