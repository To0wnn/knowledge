# Connect Claude Code, Codex and agy on this computer to a knowledge server, in one line (Windows PowerShell):
#   irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1 | iex
# The same line every time: the first time it installs; after that it updates, offers agents found without the
# plugin, and asks for a token only when the server refuses the stored one (clients/knowledge-agent.py setup).
# Gets or updates the scripts in ~\knowledge (git), then runs clients\knowledge-agent.py. Same steps as install.sh.
# No "exit": under iex it would close the PowerShell window; errors are thrown instead.
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
# A clone URL without case, trailing slash or .git.
function Norm($u) { "$u".ToLower().Replace("\", "/").TrimEnd("/") -replace "\.git$", "" }
if (Test-Path (Join-Path $Dir ".git")) {
    # Someone's own ~\knowledge (notes, another project) is never reset to our repository.
    $Have = git -C $Dir remote get-url origin 2>$null
    if ((Norm $Have) -ne (Norm $Repo)) { throw "$Dir is another repository (origin: $Have): move it away, or set KNOWLEDGE_DIR." }
    # The same steps as sync() in plugin/hooks/self_update.py; an old checkout may not have that function yet.
    if (Test-Path (Join-Path $Dir ".git\MERGE_HEAD")) { Git-Step merge --abort }
    $null = git -C $Dir pull --ff-only -q 2>&1
    if ($LASTEXITCODE) {
        if (git -C $Dir status --porcelain --untracked-files=no) {
            throw "$Dir has local changes: keep them elsewhere, then: git -C $Dir reset --hard origin/main"
        }
        Git-Step fetch -q
        Git-Step branch -f "knowledge-before-update-$(git -C $Dir rev-parse --short=8 HEAD)" HEAD
        Git-Step reset -q --keep "@{u}"  # --keep, not --hard: refuses to overwrite a file that git does not track
    }
} elseif (Test-Path $Dir) {
    throw "$Dir exists but is not a git checkout: move it away, or set KNOWLEDGE_DIR."
} else {
    git clone -q $Repo $Dir
    if ($LASTEXITCODE) { throw "git clone $Repo failed" }
}
Write-Host "${Dir}: up to date with GitHub"
# $args splatted as is: a one-element array assigned to a variable becomes a plain string.
$Agent = Join-Path $Dir "clients\knowledge-agent.py"
if ($args.Count) { & $Py.Source $Agent @args } else { & $Py.Source $Agent setup }
if ($LASTEXITCODE) { throw "knowledge-agent.py failed (exit $LASTEXITCODE)" }
