---
name: incident_dns_outage
description: DNS outage after a blocklist update
metadata:
  type: project
---

## Stand
A broken list made both resolvers drop all names. Fix: validate lists before load. Memo [[project_dns]].

## Keuzes
- **Keep it simple** (1 Oct 2026, owner): one tool per job; rejected: an all-in-one suite.
