---
name: project_dns
description: Recursive DNS with blocklists on two nodes
metadata:
  type: project
---

## Stand
Two resolvers in a pair, eight blocklists. Clients get both via DHCP from [[project_firewall]].

## Keuzes
- **Keep it simple** (1 Oct 2026, owner): one tool per job; rejected: an all-in-one suite.
