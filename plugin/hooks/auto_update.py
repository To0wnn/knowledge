#!/usr/bin/env python3
"""SessionStart (Claude Code only): turn on auto-update for this plugin's marketplace, once. Third-party marketplaces
do not auto-update by default, so a device that installed the plugin by hand (not with knowledge-agent.py) would stay
on its first version. A choice the person made stays: any autoUpdate value already set, in settings or with the
/plugin toggle, is left alone. The source is the one the marketplace was added with (a local checkout stays local).
Prints one line into the session context when it changes something; never breaks the session.
  --self-test  run the checks of this file
"""
import json
import os
import sys
from pathlib import Path

NAME = "knowledge"  # the marketplace name in .claude-plugin/marketplace.json


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def enable(config: Path) -> bool:
    """Set autoUpdate on the marketplace entry in settings.json; True when it changed something."""
    settings = config / "settings.json"
    data = read(settings)
    entry = data.get("extraKnownMarketplaces", {}).get(NAME, {})
    known = read(config / "plugins" / "known_marketplaces.json").get(NAME, {})
    if "autoUpdate" in entry or "autoUpdate" in known:
        return False
    source = entry.get("source") or known.get("source")
    if not source:  # not installed from a marketplace this machine knows: nothing to point at
        return False
    data.setdefault("extraKnownMarketplaces", {})[NAME] = {**entry, "source": source, "autoUpdate": True}
    tmp = settings.with_name(settings.name + ".knowledge-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings)
    return True


def main() -> None:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        if enable(config):
            print("knowledge plugin: auto-update is now on for its marketplace, so new versions arrive by themselves "
                  "(turn it off under /plugin > Marketplaces).")
    except (OSError, ValueError) as e:  # an unreadable settings file: say so, but never stop the session
        print(f"knowledge plugin: could not turn on auto-update ({type(e).__name__}: {e})", file=sys.stderr)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        import tempfile
        c = Path(tempfile.mkdtemp())
        (c / "plugins").mkdir()
        assert not enable(c), "no marketplace known: no change"
        src = {"source": "github", "repo": "owner/knowledge"}
        (c / "plugins" / "known_marketplaces.json").write_text(json.dumps({NAME: {"source": src}}))
        (c / "settings.json").write_text(json.dumps({"theme": "dark"}))
        assert enable(c) and read(c / "settings.json") == {"theme": "dark", "extraKnownMarketplaces": {NAME: {"source": src, "autoUpdate": True}}}
        assert not enable(c), "second run: nothing to do"
        (c / "settings.json").write_text(json.dumps({"extraKnownMarketplaces": {NAME: {"source": src, "autoUpdate": False}}}))
        assert not enable(c) and read(c / "settings.json")["extraKnownMarketplaces"][NAME]["autoUpdate"] is False, "a choice stays"
        (c / "settings.json").unlink()
        (c / "plugins" / "known_marketplaces.json").write_text(json.dumps({NAME: {"source": src, "autoUpdate": False}}))
        assert not enable(c) and not (c / "settings.json").exists(), "the /plugin toggle's choice stays"
        print("ok")
    else:
        main()
