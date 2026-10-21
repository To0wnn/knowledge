#!/usr/bin/env python3
"""Measure the knowledge-gap thresholds (WEAK_SCORE, DOUBT_SCORE, MIN_COVERAGE in server.py) against the live server:
questions the knowledge answers must not count as a gap, plausible questions it cannot answer should. Re-run after
changing the embedding model, the thresholds or a lot of the knowledge; questions are Anthony's homelab, so edit
both lists for another deployment. Uses /semantic (not logged as a gap). Standard library only.
  python3 calibrate_gaps.py [token-file]   (default ~/.config/knowledge-server/report.token)
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://knowledge.to0wnn.com"
ANSWERABLE = [
    "welk IP heeft de NAS BMC", "hoe herstart ik de Wolf gaming server", "waar staat de Mailcow container",
    "wat is het energiecontract per maand", "welke blocklists gebruikt Technitium", "hoe update ik OPNsense",
    "waarom bevriest de NAS op dinsdag", "hoe maak ik een nieuw geheim in Vaultwarden", "welke GPU zit in VM 111",
    "hoe draait de PA van Linda", "waar draait Uptime Kuma en hoeveel monitors", "Velux plissé looptijd",
    "hoe werkt de deurbel camera go2rtc", "Keychron profiel gamepad", "waar staan de forge database dumps",
    "trade bot bar cache", "hoe verbind ik een nieuwe client met de knowledge server", "Roborock laadprobleem batterij",
    "GPON stick wachtwoord LuCI", "Traefik schijf vol door access log"]
UNANSWERABLE = [
    "welk IP heeft de Synology DS920", "hoe herstart ik de Minecraft server", "waar staat de Jenkins container",
    "wat kost de internetaansluiting per maand", "welke VLAN gebruikt de Ring deurbel",
    "hoe update ik de Ubiquiti Dream Machine", "waarom crasht de Raspberry Pi in de schuur",
    "hoe maak ik een nieuw geheim in HashiCorp Vault", "welke GPU zit in de laptop van Linda", "hoe draait de Alexa routine",
    "waar draait Zabbix", "Somfy rolluik looptijd", "hoe werkt de Nest thermostaat", "Logitech muis profiel",
    "waar staan de Oracle database dumps", "crypto bot binance api", "hoe koppel ik Obsidian sync",
    "Dyson stofzuiger filter", "Fritzbox wachtwoord", "Nginx certificaat verlopen"]


def ask(q: str, token: str) -> dict:
    req = urllib.request.Request(f"{SERVER}/semantic?" + urllib.parse.urlencode({"q": q}),
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> int:
    token = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".config/knowledge-server/report.token").read_text().strip()
    if "chunks" not in (status := ask("x", token)["status"]):
        sys.exit(f"semantic index not ready: {status}")
    false_alarms = [q for q in ANSWERABLE if ask(q, token)["gap"]]
    missed = [q for q in UNANSWERABLE if not ask(q, token)["gap"]]
    print(f"gaps caught: {len(UNANSWERABLE) - len(missed)} of {len(UNANSWERABLE)} (missed: {missed})")
    print(f"false alarms: {len(false_alarms)} of {len(ANSWERABLE)} {false_alarms}")
    return 1 if false_alarms else 0  # a false alarm buries the real gaps; a missed gap only hides one


if __name__ == "__main__":
    sys.exit(main())
