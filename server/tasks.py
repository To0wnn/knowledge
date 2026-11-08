"""Task lines in memos: "open until <date> [<owner>, <ID>] from <requester>, start <when>: <what>" read as fields.
Pure functions over one line of text; the server (server.py) keeps who may write, start and lease them."""
import hashlib
import re

import lint as kb_lint

TASK_OWNER = re.compile(r"^\[([^,\]]+),\s*([^\]]+)\]")
TASK_ITEM = re.compile(r"^\s*(?:[-*+]\s+)?")  # a task line is a list item, or a bare line, that starts with "open until"
# "from <requester>, start <condition>:" right after the owner bracket; a "(claimed ...)" marker may sit around it.
TASK_START = re.compile(r"\s*(?:\([^)]*\)\s*)*from\s+([^,:]+?)\s*,\s*start\s+(now|after approval|after\s+[^\s:,()]+"
                        r"|20\d\d-\d\d-\d\d|\d{1,2} [a-z]+ 20\d\d)\s*(?:\([^)]*\)\s*)*:", re.I)
DONE_LINE = re.compile(r"^\s*-\s*\[([^,\]]+),\s*([^\]]+-done)\]:?\s*(.*)$", re.I)  # a result handed back, no date
TASK_FORMAT = ("'- open until <date> [<Owner>, <ID>] from <Requester>, start <now | after <ID> | <date> | after approval>: "
               "<what>' (<Owner>@<device> lets only that device start it)")
HAND_BACK = ("When another agent handed it to you and waits on the result, hand it back with a line for that agent: "
             "'[<requester>, <ID>-done]: <result>; <commit or path>'. A new task for another agent: " + TASK_FORMAT + ".")
WORKER_HANDBACK = re.compile(r"\b(?:branch|folder)\s+\S+.*\bdone:.*\bleft:", re.I | re.S)  # folder: a project without git
# what is meant as a task line: the owner bracket before any ':' (a plain open item may link "[a, b](...)" after its colon)
TASK_LIKE = re.compile(r"^\s*(?:[-*+]\s+)?open (?:until|tot)\b[^:\[]*\[[^,\]]+,[^\]]+\]", re.I)
CONT = re.compile(r"(?:-cont|-fix)+$", re.I)  # KN-1-cont-cont (a hand-over) and KN-1-fix (a review sent back) continue KN-1


def parse_task(line: str) -> dict | None:
    """A task line "open until <date> [<owner>, <ID>] from <requester>, start <when>: <what>" as fields, None for any
    other line. A line without from/start is legacy and starts now. Only a line (list item) that starts with "open
    until": an index line "- [<title>](memo.md)" quoting one is a link, never a task."""
    if not ((m := kb_lint.OPEN_DUE.match(TASK_ITEM.sub("", line, 1))) and (o := TASK_OWNER.match(m.group(2)))):
        return None
    rest, (owner, _, pin) = m.group(2)[o.end():], o.group(1).partition("@")  # [Claude@<device>, ID]: only that device
    f = TASK_START.match(rest)
    return {"owner": owner.strip(), "device": pin.strip(), "id": o.group(2).strip(), "due": kb_lint.latest_date(m.group(1)),
            "from": f.group(1).strip() if f else "", "start": " ".join(f.group(2).split()) if f else "now",
            "legacy": not f, "claimed": bool(re.search(r"\((?:claimed|bezig)\b", line, re.I)),
            "worker_claim": bool(re.search(r"\((?:claimed|bezig)\b[^)]*\bworker\b", line, re.I)),
            "text": (rest[f.end():] if f else rest).lstrip(": ").strip(), "body": TASK_ITEM.sub("", line.strip())}


def new_results(before: str, after: str, known: dict | None = None) -> dict:
    """{ID: signature} of the result lines a change added or reworded, so they get this writer. `known` (a rescan, which
    sees every memo as new after a restart): a line whose record fits, or has no signature yet, stays its writer's."""
    old, out = set(before.splitlines()), {}
    for line in after.splitlines():
        if line not in old and (d := DONE_LINE.match(line)):
            i = re.sub(r"-done$", "-done", d.group(2).strip(), flags=re.I)
            sig = hashlib.sha256(line.strip().encode()).hexdigest()[:16]
            rec = (known or {}).get(i)
            if known is None or not rec or rec.get("sig") not in (None, sig):
                out[i] = sig
    return out


def task_sig(t: dict) -> str:
    """What a task asks, as a short hash: owner, requester, start and text. A claim marker or a new due date leave it."""
    what = re.sub(r"\((?:claimed|bezig)\b[^)]*\)", "", t["text"], flags=re.I)
    owner = t["owner"] + ("@" + t["device"] if t.get("device") else "")  # pinning to another device is another task
    return hashlib.sha256("\n".join((owner.lower(), t["from"].lower(), t["start"].lower(), " ".join(what.split()))).encode()).hexdigest()[:16]


def edit_overlaps(me: str, edits: dict, now: float, fresh: float) -> list:
    """[(other session key, its entry, shared files)] for the sessions whose unlanded changes (files changed against the
    shared main, reported with each /tasks ask) touch the files session `me` changes in the same repository. Two writers
    of one file are where merges conflict; being told early is the plan-with-files that halves conflicts (CooperBench)."""
    mine = edits.get(me)
    if not mine or not mine["files"]:
        return []
    out = []
    for key, e in edits.items():
        if key != me and e["repo"] == mine["repo"] and now - e["at"] < fresh and (shared := sorted(set(e["files"]) & set(mine["files"]))):
            out.append((key, e, shared))
    return out


if __name__ == "__main__":
    ed = {"a": {"repo": "r", "files": ["x.py", "y.py"], "at": 100}, "b": {"repo": "r", "files": ["y.py"], "at": 100},
          "c": {"repo": "other", "files": ["y.py"], "at": 100}, "d": {"repo": "r", "files": ["y.py"], "at": 0}}
    assert [(k, s) for k, _, s in edit_overlaps("a", ed, 150, 60)] == [("b", ["y.py"])], "same repo, fresh, shared file only"
    assert edit_overlaps("b", {**ed, "b": {**ed["b"], "files": []}}, 150, 60) == [], "nothing changed: no overlap"
    assert edit_overlaps("z", ed, 150, 60) == [], "an unknown session"
    res = "- [Codex, R-1-done]: ok\n"
    sig = new_results("", res)["R-1-done"]
    assert not new_results(res, res) and new_results(res, "- [Codex, R-1-done]: other\n").keys() == {"R-1-done"}
    assert not new_results("", res, {"R-1-done": {"by": "guest", "sig": sig}}), "a restart keeps the writer"
    assert not new_results("", res, {"R-1-done": {"by": "guest"}}), "a record from before signatures too"
    assert new_results("", res, {"R-1-done": {"by": "guest", "sig": "other"}}) == {"R-1-done": sig}
    assert new_results("", res, {}) == {"R-1-done": sig}
    assert parse_task("- open tot 1 jan 2099 [Gemini, VS-1]: water")["legacy"]
    assert parse_task("* open until 2099-01-01 [Codex, K-2] from Claude, start after K-1: build: it")["body"].startswith("open until")
    assert "R-1-done" in new_results("", "- [Codex, R-1-Done]: ok\n"), "case-insensitive -Done suffix normalized"
    assert task_sig({"owner": "A", "from": "Claude", "start": "now", "text": "x"}) == task_sig({"owner": "a", "from": "claude", "start": "now", "text": "x"}), "task_sig case-insensitive"
    assert parse_task("- open until 2099-01-01 [Codex, K-2] from Claude, start after K-1: build: it")["start"] == "after K-1"
    assert parse_task("- open until 2099-01-01 [Codex, K-2] from Claude, start after K-1: build: it")["text"] == "build: it"
    assert parse_task("- open until 2099-01-01 [Codex, K-3] (claimed Codex, 2026-10-31) from Ann, start 2 nov 2026: x")["from"] == "Ann"
    assert parse_task("- open until 2099-01-01 [Codex, K-4] from Ann, start after approval: x")["start"] == "after approval"
    assert parse_task("- open until 2099-01-01: no owner") is None
    assert parse_task("- [open until 2099-01-01 [Codex, I-1] from A, start now: x](f.md) — d") is None  # an index line
    assert (lambda t: (t["owner"], t["device"]))(parse_task("- open until 2099-01-01 [Claude@Lap-1, P-1] from Ann, start now: x")) == ("Claude", "Lap-1")
    assert WORKER_HANDBACK.search("- [Claude, K-1-done]: works; branch task/K-1; done: all; left: nothing")
    assert not WORKER_HANDBACK.search("- [Claude, K-1-done]: works")
    print("ok")
