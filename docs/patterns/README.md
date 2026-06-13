# Playbook Patterns

Reusable Ansible/AAP patterns that come up repeatedly on large fleets:

| Doc | What it solves |
|---|---|
| [Aggregating Inventory Data to CSV](aggregating-inventory-to-csv.md) | Collecting data from every host into a single output file (CSV/JSON) — the five patterns that actually work, including what changes under Job Slicing and ephemeral EE filesystems. |
| [Role Timing & Timeouts](role-timing-and-timeouts.md) | Timing each role, applying a global per-task timeout, and bounding runaway tasks — `task_timeout` vs `async` vs the shell `timeout` command, with the trade-offs of each. |
