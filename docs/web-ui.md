# Web UI

Moved from the README; [back to the README](../README.md).

## Views

- **Web UI:** one app shell: a sidebar (`[` folds it to an icon rail; below 1000 px it is the rail) with **Agents**
  (Inbox, Chats, Tasks, Machines) and **Knowledge** (Notes, Map, Changes, Health = lint), This pc, Admin and Help;
  on a phone a bottom tab bar (Inbox, Chats, Tasks, Notes, More). Every view has its own URL (`#/inbox`, `#/notes/<path>`,
  `#/map`, `#/tasks?view=flow`, …), so refresh, back and forward work; an old `#<path>` link opens the note.
  Ctrl/Cmd+K (or "Search or jump", or the phone's search icon) opens a command palette: notes by title, text and
  meaning, chats, tasks, every view and a few actions (new chat, theme, sidebar, mission flow), with arrow keys, Enter,
  Esc and your recent picks; `/` opens it for notes only, and "Search all notes" opens the search page
  (`#/notes?q=…`). Notes: the topic list beside the note (on a phone two pages with a Back arrow), a home view per
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
  picker scopes only the knowledge views. An admin page for tokens, people and projects. Light or dark, your pick.

## Design system

One small set of controls in `server/static/app.css` ("Controls"), shared by the companion page (`.cmp-btn`,
`.cmp-iconbtn`, same sizes) through the token block both files carry (`--r-sm`, `--h-btn`, `--h-btn-sm`):

- **Buttons** are 8 px rectangles in two sizes, 36 px and `.small` 28 px (44 px on a phone), chosen by class, never by
  where they sit: `.btn` (secondary), `.btn.primary`, `.btn.quiet`, `.btn.danger`, `.btn.icon`. A pressed toggle or an
  open `<details>` button wears the amber tint. Inputs and selects are as high as the buttons beside them.
- **Clickable rows and cards** (`.row-link`, a chat in the list, an Inbox card) tint on hover, underline their title and
  end in a `›`. **Disclosures** always show `▸`/`▾`, also on a phone; a menu shows `▾`.
- **Links** have one look: the accent ink with a soft underline (never the browser's blue).
- **Status chips** (`.chip`, `.pill`, `.badge`, counts) are the only pills and never look clickable: no border, the
  default cursor. One focus ring everywhere: 2 px amber, outside the element (inside only where a box clips it).

## Graph

- **Graph:** notes as nodes, links as lines, one island per topic. A line's thickness and pull follow how related two
  notes are (direct link, shared source, shared neighbours, same type); weak links between topics show on hover.

## Install as an app (PWA)

The web UI installs as an app (Chrome or Edge menu, "Install app"; Safari, "Add to Home Screen"): a manifest at
`/ui/manifest.webmanifest` (start page `/ui#/inbox`, scope `/ui`, standalone) with 192/512 px and maskable icons.
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
