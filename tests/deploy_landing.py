"""deploy.sh as the landing step, in throwaway git repos with a fake ssh, rsync and gh: two sessions that both changed
plugin/ land one after the other and get different versions, a hand-set minor bump stays, a conflict with main is
refused with the tree clean and the lock freed, a red main refuses unless --fix-main, a held lock is waited for, and a
worktree without local.env uses the main checkout's. Run: python3 tests/deploy_landing.py (part of ./check.sh)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="deploy-landing-"))
ROOT = T / "forge"  # stands for /root on forge
BIN = T / "bin"
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "KNOWLEDGE_"))},
       "PATH": f"{BIN}:{os.environ['PATH']}", "GIT_CONFIG_GLOBAL": str(T / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
       "HOME": str(T)}


def sh(cmd, cwd, ok=True, **env):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, env={**ENV, **env}, stdin=subprocess.DEVNULL)
    if ok and p.returncode:
        raise AssertionError(f"{cmd} failed in {cwd}:\n{p.stdout}{p.stderr}")
    return p


def version(tree):
    return json.loads((tree / "plugin/.claude-plugin/plugin.json").read_text())["version"]


def setup():
    BIN.mkdir()
    (T / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n")
    # ssh: build and ready wait succeed; everything else (lock, live commit) runs here with /root/ moved under T
    (BIN / "ssh").write_text(f'#!/bin/sh\nshift\ncase "$*" in *"compose -p"*|*health*) echo ok; exit 0;; esac\n'
                             f'exec sh -c "$(printf %s "$*" | sed "s#/root/#{ROOT}/#g")"\n')
    (BIN / "rsync").write_text("#!/bin/sh\nexit 0\n")
    (BIN / "chown").write_text("#!/bin/sh\nexit 0\n")  # forge's chown 1000:1000 runs as root there; a CI runner (uid 1001) may not
    # gh: GH_RED names a workflow whose last finished run on main failed
    (BIN / "gh").write_text('#!/bin/sh\ncase "$*" in *"$GH_RED"*) [ -n "$GH_RED" ] && echo "abc1234 (failure) broke it";; esac\nexit 0\n')
    for f in BIN.iterdir():
        f.chmod(0o755)
    (ROOT / "docker/knowledge").mkdir(parents=True)
    sh("git init -q --bare origin.git", T)
    src = T / "a"
    sh(f"git clone -q {T / 'origin.git'} a", T)
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    (src / "check.sh").write_text('#!/bin/sh\n[ -z "$CHECK_FAILS" ]\n')
    (src / ".gitignore").write_text("local.env\nserver/.env\n")
    for d in ("plugin/.claude-plugin", "plugin/.codex-plugin", "server"):
        (src / d).mkdir(parents=True)
    for f in ("plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
        (src / f).write_text('{\n  "name": "knowledge",\n  "version": "0.1.0"\n}\n')
    (src / "server.json").write_text('{\n  "version": "0.1.0",\n  "identifier": "ghcr.io/x/knowledge:0.1.0"\n}\n')
    (src / "plugin/hook.py").write_text("one\ntwo\nthree\n")
    (src / "server/app.py").write_text("x = 1\n")
    for f in ("deploy.sh", "check.sh"):
        (src / f).chmod(0o755)
    sh("git add -A && git commit -qm init && git push -q origin HEAD:main", src)
    (src / "local.env").write_text("KNOWLEDGE_FORGE_SSH=forge\n")
    b = T / "b"
    sh(f"git clone -q {T / 'origin.git'} b", T)
    (b / "local.env").write_text("KNOWLEDGE_FORGE_SSH=forge\n")
    return src, b


def edit(tree, path, old, new, msg):
    f = tree / path
    f.write_text(f.read_text().replace(old, new))
    sh(f"git commit -qam '{msg}'", tree)


def main():
    a, b = setup()
    lock = ROOT / "docker/knowledge/deploy.lock"
    # two sessions change plugin/ without a bump: each lands with its own number, the second brings the first in
    edit(a, "plugin/hook.py", "one", "ONE", "a")
    edit(b, "plugin/hook.py", "three", "THREE", "b")
    out = sh("./deploy.sh", a).stdout
    assert "plugin version 0.1.1" in out and version(a) == "0.1.1", out
    out = sh("./deploy.sh", b).stdout
    assert "brought in origin/main" in out and "plugin version 0.1.2" in out, out
    assert version(b) == "0.1.2" and '"version": "0.1.2"' in (b / "plugin/.codex-plugin/plugin.json").read_text()
    assert "knowledge:0.1.2" in (b / "server.json").read_text()
    sh("git fetch -q origin", b)
    hook = sh("git show origin/main:plugin/hook.py", b).stdout
    assert hook == "ONE\ntwo\nTHREE\n" and sh("git rev-parse origin/main", b).stdout == sh("git rev-parse HEAD", b).stdout
    assert not lock.exists(), "the lock is freed after a deploy"
    # a server-only change gets no version; a minor bump set by hand stays
    sh("git pull -q --ff-only origin main", a)
    edit(a, "server/app.py", "1", "2", "server")
    assert "plugin version" not in sh("./deploy.sh", a).stdout and version(a) == "0.1.2"
    for f in ("plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json", "server.json"):
        (a / f).write_text((a / f).read_text().replace("0.1.2", "0.2.0"))
    edit(a, "plugin/hook.py", "two", "TWO", "minor")
    assert "plugin version" not in sh("./deploy.sh", a).stdout and version(a) == "0.2.0"
    # a conflict with main: refused, the checkout is clean and back where it was, the lock freed
    edit(b, "plugin/hook.py", "two", "zwei", "conflict")
    head = sh("git rev-parse HEAD", b).stdout
    p = sh("./deploy.sh", b, ok=False)
    assert p.returncode and "does not merge cleanly" in p.stderr, p.stderr
    assert not sh("git status --porcelain", b).stdout and sh("git rev-parse HEAD", b).stdout == head and not lock.exists()
    sh("git reset -q --hard HEAD~1", b)
    # the check fails on the combined tree: the version files are not left changed
    (b / "plugin/new.py").write_text("n = 1\n")
    sh("git add -A && git commit -qm b2", b)
    # the check fails on the combined tree: its merge and version commits are taken back, or the version lines would
    # conflict with main at the next try
    head = sh("git rev-parse HEAD", b).stdout
    p = sh("./deploy.sh", b, ok=False, CHECK_FAILS="1")
    assert p.returncode and not sh("git status --porcelain", b).stdout and not lock.exists(), p.stderr
    assert sh("git rev-parse HEAD", b).stdout == head, "a failed deploy leaves no commits behind"
    # red main refuses, --fix-main goes on
    p = sh("./deploy.sh", b, ok=False, GH_RED="clients.yml")
    assert p.returncode and "CI clients.yml is red on main since abc1234 (failure)" in p.stderr, p.stderr
    sh("./deploy.sh --fix-main", b, GH_RED="clients.yml")
    assert sh("./deploy.sh --nope", b, ok=False).returncode
    # a lock held by another deploy is waited for; past the wait it refuses, and is not taken away from it
    lock.mkdir()
    (lock / "by").write_text("other-deploy\n")
    edit(b, "server/app.py", "x", "y", "c")
    p = sh("./deploy.sh", b, ok=False, DEPLOY_LOCK_WAIT="0")
    assert p.returncode and "another deploy is still running after 0s (other-deploy)" in p.stderr and lock.exists(), p.stderr
    # freed while this one waits: it takes the lock and lands
    p = sh(f"(sleep 3; rm -rf {lock}) & ./deploy.sh", b)
    assert "waiting for another deploy (other-deploy)" in p.stderr and "got the lock after" in p.stdout, p.stdout + p.stderr
    assert not lock.exists()
    # a worktree has no local.env: it uses the main checkout's
    sh(f"git worktree add -q {T / 'wt'} -b wt", b)
    edit(T / "wt", "server/app.py", "y", "z", "wt")
    assert not (T / "wt/local.env").exists()
    sh("./deploy.sh", T / "wt")
    print("ok deploy landing")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    sys.exit(0)
