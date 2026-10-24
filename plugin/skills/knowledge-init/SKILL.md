---
name: knowledge-init
description: Link a local project folder to a project on the knowledge server, so every session in that folder (Claude Code, Codex, agy) gets that project's memory and docs. Use when the user asks to link, connect, set up or init a folder for the knowledge server, or when the session context says the folder is not linked.
---

# Link a folder to a knowledge project

The link is one file, `.knowledge-project`, in the project's root folder, holding the project name. Every session
started in that folder or below gets that project (the folder name no longer matters), and for a project kept on the
server the folder's `.md` files are mirrored there as its docs.

1. **Which project:** call `knowledge_projects` (the projects this device may use). Take the one the user names. None
   fits, or the user is unsure: ask. A new project is made by the user on the web UI (`/ui/admin`, Projects), never
   guessed or invented.
2. **Which folder:** the project's root: the git root (`git rev-parse --show-toplevel`), else the folder the session runs
   in. Never the home folder or a drive root; ask when unsure.
3. **Already linked:** a `.knowledge-project` there, or in a folder above, with another name: show both and ask before
   changing it.
4. **Write the file** with your file tool: one line, the project name, UTF-8. (By hand: `echo a350 > .knowledge-project`;
   PowerShell `Set-Content .knowledge-project a350`. Both encodings are read.)
5. **Git:** the file holds only the name, so it may be committed; ask whether the user wants that.
6. **Tell the user** in two lines: the folder is linked to `<project>`, and it takes effect at the next session start
   (this session keeps the project it started with). For a server project: the folder's Markdown goes to its docs
   from then on.

Check: in the next session the context header reads `knowledge (project <name>)`, without a "not linked" notice.
