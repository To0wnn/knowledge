---
name: project_firewall
description: Firewall: VLANs, rules and the update routine
metadata:
  type: project
---

## Stand
Four VLANs (lan, servers, iot, guests). DNS forced to [[project_dns]]. Updates follow `network.md`. VPN: [[project_wireguard]].

## Keuzes
- **Keep it simple** (1 Oct 2026, owner): one tool per job; rejected: an all-in-one suite.
