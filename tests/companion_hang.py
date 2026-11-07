#!/usr/bin/env python3
"""A really hanging disk under the companion: a linked folder is a FUSE mount that stops answering, as a hard NFS mount or
a NAS share does. A round must go on without it, say so once, and pick the folder up again by itself. Linux with FUSE
only, so not in the check; run it after changing what a companion round touches on disk:

  systemd-run --user --scope -q -p MemoryMax=3G uv run --project server --with fusepy python tests/companion_hang.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "plugin" / "hooks")]
os.environ["KNOWLEDGE_COMPANION_SCAN_EVERY"] = "0"


def main():
    from storage_hang import wait_for
    with tempfile.TemporaryDirectory() as t:
        real, mount, flag, home = Path(t) / "real", Path(t) / "mnt", Path(t) / "hang", Path(t) / "home"
        for d in (real, mount, home):
            d.mkdir()
        os.environ.update(HOME=str(home), KNOWLEDGE_HOME=str(home / ".knowledge"))
        fuse = subprocess.Popen([sys.executable, str(ROOT / "storage_hang.py"), "--fuse", str(real), str(mount), str(flag)])
        try:
            wait_for("mount", lambda: os.path.ismount(mount), 10)
            subprocess.run(["git", "-C", str(mount), "init", "-q", "-b", "main"], check=True)
            (mount / ".knowledge-project").write_text("garden\n")
            import companion
            said = []
            companion.log = lambda kind, text, detail="": said.append((kind, text))
            companion.STATE.mkdir(parents=True, exist_ok=True)
            (mount / "b").mkdir()
            (mount / "b" / ".knowledge-project").write_text("beds\n")
            (companion.STATE / "linked.json").write_text(json.dumps([str(mount), str(mount / "b")]))
            links = {"linked": [{"path": str(mount), "project": "garden"}], "candidates": []}

            def round_(budget, what):
                t0 = time.time()
                found = companion.project_folders()
                folders = companion.folder_links()
                git, _ = companion.with_git(links, {}, time.time())
                took = time.time() - t0
                assert took < budget, f"{what}: a round took {took:.1f} s"
                print(f"ok {what}: {took:.2f} s")
                return found, folders, git["linked"][0]["git"]

            found, folders, git = round_(25, "before")
            assert found.get("garden") == str(mount) and git and git["branch"] == "main", (found, git)

            flag.touch()
            print("-- the disk stops answering")
            found, folders, git = round_(8, "first round on the hung disk, two folders")  # the looks wait together, then skipped
            assert "garden" not in found and git is None, (found, git)
            found, folders, git = round_(2, "next round")
            assert "garden" not in found and git is None, (found, git)
            assert len([s for s in said if "does not answer" in s[1]]) == 2 and all(s[0] == "error" for s in said if "does not answer" in s[1]), said

            flag.unlink()
            print("-- it answers again")
            wait_for("the waiting calls to end", lambda: not any(th.is_alive() for th in companion.HUNG.values()), 30)
            found, folders, git = round_(25, "after")
            assert found.get("garden") == str(mount) and git and git["branch"] == "main", (found, git)
            assert any("answers again" in s[1] for s in said), said
            print("ok")
        finally:
            flag.unlink(missing_ok=True)
            subprocess.run(["fusermount", "-uz", str(mount)], capture_output=True)
            fuse.terminate()


if __name__ == "__main__":
    main()
