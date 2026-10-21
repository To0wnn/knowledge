---
name: project-setup
description: Start a new project, or bring an existing one onto the shared development rules - AGENTS.md with Commands and Project rules, a CLAUDE.md that imports it, one check command, a git pre-commit hook and a README. Use when starting a project, or when a project has no AGENTS.md with a "## Commands" section.
---

# Project setup

For a new folder and for an existing project. Existing project: read it first and keep what is there; move project
facts into the AGENTS.md sections below instead of deleting instructions.

1. **Look**: stack, how it runs, how it is tested, where it deploys; existing CLAUDE.md / AGENTS.md / README.
2. **git**: `git init` when needed; a `.gitignore` for the stack plus `.env`, virtualenvs, `node_modules/`, data and
   build output. A public repo gets the GitHub noreply address as commit e-mail.
3. **One check command** that proves the project works (lint + tests + build), exits non-zero on failure and runs on
   the OS of whoever works on it: `uv run pytest` / `py -m pytest` (Windows), `npm run check`, `make check`, or a
   `check.py` that calls the rest. No tests yet: start with one smoke test of the main path.
4. **AGENTS.md** from the template below. **CLAUDE.md** holds only the line `@AGENTS.md` (Claude Code then reads it;
   Codex and agy read AGENTS.md themselves).
5. **pre-commit hook** `.git/hooks/pre-commit` that runs the check (git runs it for every agent):
   `#!/bin/sh` + the check command. `git commit --no-verify` only in an emergency, and say so.
6. **README**: what it is, how to run it, how to check it, where decisions are kept.
7. **Knowledge server** (ask first): a file `.knowledge-project` with the project name mirrors the folder's Markdown
   (`Set-Content .knowledge-project <name>` in PowerShell, `echo <name> > .knowledge-project` elsewhere).
8. Run the check once, show the output, commit `chore: project setup`.

## AGENTS.md template

```markdown
# <Project>

<One paragraph: what it is, for whom, where it runs.>

## Commands
- check (must pass before "done"): `<command>`
- run: `<command>`
- test one thing: `<command>`
- deploy: `<command, or "none">` (routine | ask first)
- roll back: `<command>`

## Project rules
- <Only what differs from or adds to the shared development rules, e.g. "measure before and after with <command>",
  "never write to <folder>", "work in branch <x>, merge with --ff-only".>

## Layout
- `<dir>/`: <what lives there>
```
