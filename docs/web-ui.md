# Web UI

Moved from the README; [back to the README](../README.md).

## Views

- **Web UI:** one app shell: a sidebar (`Alt+[` folds it to an icon rail; below 1000 px it is the rail) with one project
  picker on top (Alt+1-9, Alt+0 = all, except on the chat grid, where they pick a part of the grid: docs/chats.md; it scopes every view: Notes, Map, Agents, Tasks and a new chat's folder; the
  Needs-you badge still counts all), **Agents** (Agents, Tasks, Machines) and **Knowledge** (Notes, Map, Changes,
  Health = lint), This pc, Admin, Help and Settings (user menu: a new chat's default model, the start page, what ↗ on a grid card does, whether the grid groups per project or puts all chats together, and the send key, kept per person on the server; theme and notifications, per browser. Only what use cannot teach gets a setting: what remembers itself, like the list's width, or what the browser does, like zoom, does not);
  on a phone a bottom tab bar (Agents, Tasks, Machines, Notes, More). The Agents view (`#/chats`; `#/inbox` opens the same view) is one
  list grouped Needs you (each row says why: answered and not read, asks approval, failed), Review (a branch with
  commits ready to merge), Working and Done today (folded), with quick actions on a row (Allow/Deny an approval,
  Merge/Reject a review, a one-line Reply; Snooze 1 h / until tomorrow, Dismiss and Archive in the row's ⋯ menu) beside an open chat
  (`#/chats/<chat>`, or on its own at `#/chats?view=list`, where the `#/inbox/<key>` links land); on a desktop (1000 px and up) `#/chats` itself
  tiles the open chats in a grid that fills the screen, per project with the one last active on top (a project folds away, and then says what waits on you; a card you are in stays put on screen when others move; Settings can put all chats together) (a card's ↗ opens that chat alone in a tab of its own, `#/chats/<id>?solo=1`: no sidebar, list or header, its Knowledge logo goes to the home page, × or Esc closes the tab; Ctrl, Cmd or middle click on a row in the list does the same; a browser tab has the chat's name and `(n)` for what needs you; text typed but not sent stays per person and chat in this browser, also through a reload and in the chat's other tab, until it is sent or you sign out), a card moving to the top only when its chat finishes or asks an approval, with a List link in the header that counts what needs you but has no card there (an old `?view=grid` or `?view=board` link opens it too); on a
  phone the list is the start screen and a chat opens full screen. Details: [chats.md](chats.md). Every view has its own URL (`#/chats`, `#/notes/<path>`,
  `#/map`, `#/tasks?view=flow`, …), so refresh, back and forward work; an old `#<path>` link opens the note. The sidebar goes back to where you were in a view (its last route and scroll, per tab: sessionStorage `kn_routes`; scrolling or clicking yourself ends the restore); clicking the view you are on goes to its start. Usage chips (Agents) show each agent's fullest limit and reset time (Claude from its sessions and workers, Codex from its rollout or, without a session, `codex app-server` `account/rateLimits/read` every 15 min, agy from a running agy's language server, else the cloud quota call agy itself makes (`v1internal:fetchAvailableModels` with agy's OAuth token from `~/.gemini/antigravity-cli/antigravity-oauth-token`, `User-Agent: antigravity-cli` or Google answers 403; an expired token is renewed with `agy models`, no prompt; every 15 min; undocumented API, so a failure goes to the activity log)).
  Ctrl/Cmd+K (or "Search or jump", or the phone's search icon) opens a command palette: notes by title, text and
  meaning, chats, tasks, every view and a few actions (new chat, theme, sidebar, mission flow), with arrow keys, Enter,
  Esc and your recent picks; `/` opens it for notes only, and "Search all notes" opens the search page
  (`#/notes?q=…`). Alt plus a key jumps: `Alt+[` folds the sidebar, `Alt+A` the Agents view, `Alt+N` a new chat, `Alt+M` map or notes, `Alt+1`-`9`/`0` the project (outside a text box, and from a chat's text box). Notes: the topic list beside the note (on a phone two pages with a Back arrow), a home view per
  topic, a note preview with connected notes, an editor, change history. Changes (per day) and Health (lint, per
  check) are full pages. Errors show as a short message at the bottom instead of a browser dialog. Tasks: the task lines between agents with from whom and when they may start, waiting ones
  greyed with the reason, stalled wakes, an Approve button for the owner, results handed back (Done removes a line),
  as a list or as the mission map. Cancel (`POST /ui/api/task/cancel`, owner or admin) turns a line into the result
  `[<requester>, <ID>-done]: cancelled by <user>: <reason>` (a hand-over `<ID>-cont` cancels `<ID>`), so its requester
  hears it and a task that starts after it waits for a decision instead of starting, also after the result line is
  read and removed (the server keeps the cancellation; approving the waiting task lets it start, a new line with that ID
  reopens it). A task a worker or session holds is not cancelled: stop the worker first. A write refuses a new task line whose date has no year (it would be no
  task) or whose "start after <ID>" names an ID the project never had (it would start at once); a file written
  straight to disk gets the same message as a notice at that device's next turn. A line that waits for approval
  says so in the write's answer. Searches in the web UI that find nothing (exact and by meaning, where the person
  stopped typing) are logged as gaps like the agents' searches. Deleted memos and docs are listed with
  `GET /ui/api/trash` and go back with `POST /ui/api/trash/restore` (owner or admin, never over a file that exists). Machines: workers, sessions, hook errors and remote actions per pc. The project
  picker scopes only the knowledge views. Under Tokens, an admin can undo what one device changed in a time window (`POST /ui/api/admin/undo-device {device, hours, apply, paths}`): first the list, per file back to the version before or, for a file it made, to the trash; a file someone else changed since is left alone. An admin page for tokens, people and projects: one section at a time from a
  sticky tab bar (arrow keys move through it; on a phone it scrolls sideways), its hash in the URL (`/ui/admin#tokens`,
  `#settings`); System's parts (background jobs, index, config, audit log, searches without hits, lessons) fold, each
  with its count, and have a hash of their own (`#gaps`, `#audit`, …) that opens System there. Light or dark, your pick.
- **Empty, loading and error states:** every view says in one muted line what it is for and the next step (with a
  button where there is one), "Loading…" while it loads and, when its API call fails, the message with Retry. Health
  with no findings says "All clear". A person with no pc online with a companion gets a Get started card in the
  Agents list instead (the setup line for their system from the help page's Install section, link a folder, start a
  chat); with a pc but no chats, a line with New chat.
- **Without remote control:** a person none of whose tokens has "Agents (remote control)" on (`me.agents` false,
  [companion.md](companion.md#agents-remote-control-per-token)) gets only the knowledge part: no Agents or Machines
  entries (sidebar, tab bar, palette), no usage chips or New chat; the start page, `#/chats`, `#/inbox` and
  `#/machines` open Notes. Tasks stays, without "Start on <pc>" or worker buttons; the phone tab bar is Tasks,
  Notes, Map, Changes, More. The server enforces it; the page only follows.
- Live updates: the page keeps a Server-Sent Events stream open. `changed` means a note or doc changed; the page then
  does one refresh at a time (changes during it make one more after it) and a background tab waits until it is shown.
  `sessions` means a session log changed, nearly all writes on a busy day: only a page showing Changes or an open session
  log acts on it, and graph, lint and context caches stay valid. `tests/ui_load.py` measures what the refreshes cost the
  rest of the server.

## Design system

One idea: **one lamp**. Amber means "this needs you" and nothing else: the lamp before the name (dark until something
waits, then lit with one ring), the Needs-you count, a chat or row that waits, an approval, a token shown once. What you
act on or have selected is spruce (`--act`, `--act-soft`, `--act-text`); red (`--bad`) is only for what is broken or
dangerous, such as YOLO. Surfaces are flat, without blur: the sidebar on `--rail`, pages on `--canvas`, raised things on
`--panel`, rows divided by a hairline instead of boxed in cards. The dotted grid is only on the map, where graph paper
means something. `server/test_server.py` checks the contrast of the tokens in both themes (text 4.5:1, map colours 3:1).

Type is Atkinson Hyperlegible Next and its Mono, self-hosted in `static/vendor` (SIL OFL, latin subset, 34 and 18 kB;
other scripts fall back to the system font): chosen because it keeps l, I, 1 and 0, O apart in the ids and paths this
tool is full of. Sizes are 11, 12, 13, 14, 16, 20 and 26 px; headings are in sentence case.

One small set of controls in `server/static/app.css` ("Controls"), shared by the companion page (`.cmp-btn`,
`.cmp-iconbtn`, same sizes) through the token block both files carry (`--r-sm`, `--h-btn`, `--h-btn-sm`):

- **Buttons** are 6 px rectangles in two sizes, 36 px and `.small` 28 px (44 px on a phone), chosen by class, never by
  where they sit: `.btn` (secondary), `.btn.primary`, `.btn.quiet`, `.btn.danger`, `.btn.icon`. A pressed toggle or an
  open `<details>` button wears the spruce tint. Inputs and selects are as high as the buttons beside them. Send is
  quiet until there is something to send, so a grid of open chats is not a wall of filled buttons.
- **Clickable rows** (`.row-link`, a row in the Agents list) tint on hover and underline their title. **Disclosures**
  always show a drawn chevron that turns, also on a phone; a menu has one after its label.
- **Links** have one look: the accent ink with a soft underline (never the browser's blue).
- **Status chips** (`.chip`, `.pill`, `.badge`, counts) are small labels with 4 px corners and never look clickable: no
  border, the default cursor. One focus ring everywhere: 2 px spruce, outside the element (inside only where a box clips it).

**Layout that follows the content:** the map takes the whole page with the topic list floating over it, folded to its colour dots until you point at it, and a chosen note's card opens as a column beside it; the Notes
overview has no right column, what changed lately comes first and then the notes per topic; a project without tasks
shows the open tasks of your other projects instead of an empty page (the sidebar count follows); on Machines offline
pcs come last, as one line with their last report folded (open when something in it needs you or an action waits), and Update plugin is a button only on an online pc with an
older plugin than another of your pcs reports (otherwise in the ⋯ menu); the Deleted section of Changes shows only with
something in it; an agent's prose stops at 78 characters while its code and tool lines keep the whole width.

## Graph

- **Graph:** notes as nodes, links as lines, one island per topic. A line's thickness and pull follow how related two
  notes are (direct link, shared source, shared neighbours, same type); weak links between topics show on hover.

## Install as an app (PWA)

The web UI installs as an app (Chrome or Edge menu, "Install app"; Safari, "Add to Home Screen"): a manifest at
`/ui/manifest.webmanifest` (start page `/ui#/inbox`, the Agents view, scope `/ui`, standalone) with 192/512 px and maskable icons.
There is no service worker: Chrome needs none for "Install app" since version 108 on Android and 112 on desktop,
only its automatic install banner still wants a fetch handler
([Chrome blog](https://developer.chrome.com/blog/update-install-criteria)). The manifest and icons carry no data and
are served without sign-in; the page links the manifest with `crossorigin="use-credentials"`, because browsers fetch
a manifest without cookies otherwise, and a sign-in proxy in front (forward auth) would answer with a redirect to its
login page instead of the manifest. Behind a sign-in proxy on another origin (`auth.<domain>`): in the installed app
on Android, a redirect to the login page leaves the app's scope, so Chrome shows it in the app window with a
custom-tab style bar (the login page's address and a close button) and, as reported, returns to the app view once
the redirect lands inside `/ui` again
([w3c/manifest discussion](https://lists.w3.org/Archives/Public/public-webapps-github/2018Jun/0728.html); not yet
tried on a phone with this server). The installed app shares Chrome's cookies, so someone signed in in Chrome is
usually never sent there. A manifest cannot put another origin in scope (`scope` must be on the app's own origin), so
the fix is a long proxy session, not the manifest. iOS home-screen apps keep their own cookies: the first start signs
in once.
