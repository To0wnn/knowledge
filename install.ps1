# Connect Claude Code, Codex and agy on this computer to a knowledge server, in one line (Windows PowerShell):
#   irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1 | iex
# Update every agent that has the plugin, no questions:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1))) update
# One agent: the same with install codex instead of update.
# Gets or updates the scripts in ~\knowledge (git), then runs clients\knowledge-agent.py. Same steps as install.sh.
# No "exit": under iex it would close the PowerShell window; errors are thrown instead.
$Command = if ($args.Count) { @($args) } else { @("install") }
$Repo = if ($env:KNOWLEDGE_REPO) { $env:KNOWLEDGE_REPO } else { "https://github.com/To0wnn/knowledge" }
$Dir = if ($env:KNOWLEDGE_DIR) { $env:KNOWLEDGE_DIR } else { Join-Path $HOME "knowledge" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is missing: install Git for Windows (https://git-scm.com/download/win), then run this again."
}
# py (the launcher) first; a "python" from WindowsApps is the Store stub that only opens the Store.
$Py = Get-Command py -ErrorAction SilentlyContinue
if (-not $Py) { $Py = Get-Command python -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } | Select-Object -First 1 }
if (-not $Py) { throw "Python 3 is missing: install it from https://www.python.org/downloads/ (keep 'py launcher' ticked), then run this again." }

function Git-Step { git -C $Dir @args; if ($LASTEXITCODE) { throw "git $($args[0]) failed in $Dir" } }
if (Test-Path (Join-Path $Dir ".git")) {
    # The same steps as sync() in plugin/hooks/self_update.py; an old checkout may not have that function yet.
    if (Test-Path (Join-Path $Dir ".git\MERGE_HEAD")) { Git-Step merge --abort }
    $null = git -C $Dir pull --ff-only -q 2>&1
    if ($LASTEXITCODE) {
        if (git -C $Dir status --porcelain --untracked-files=no) {
            throw "$Dir has local changes: keep them elsewhere, then: git -C $Dir reset --hard origin/main"
        }
        Git-Step fetch -q
        Git-Step branch -f knowledge-before-update HEAD
        Git-Step reset -q --hard "@{u}"
    }
} elseif (Test-Path $Dir) {
    throw "$Dir exists but is not a git checkout: move it away, or set KNOWLEDGE_DIR."
} else {
    git clone -q $Repo $Dir
    if ($LASTEXITCODE) { throw "git clone $Repo failed" }
}
Write-Host "${Dir}: up to date with GitHub"
& $Py.Source (Join-Path $Dir "clients\knowledge-agent.py") @Command
if ($LASTEXITCODE) { throw "knowledge-agent.py $($Command -join ' ') failed (exit $LASTEXITCODE)" }
