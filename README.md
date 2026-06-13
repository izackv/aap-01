# AAP at Scale — Field Notes & Toolkits

Working notes, runbooks, and runnable tooling for operating **Red Hat Ansible Automation Platform (AAP)** at fleet scale — hundreds to thousands of managed hosts, on both VM and OpenShift deployments. Written for engineers who already know Ansible and OpenShift and need the *AAP-specific* failure modes, tuning knobs, and patterns that the product docs scatter across a dozen pages.

Everything here is a **living document**, drawn from real large-scale engagements. The guides read top-to-bottom as articles; the [reference](reference/config-snippets.md) and the [cheat sheet](docs/performance/cheatsheet.md) are for fast lookup mid-incident.

> [!NOTE]
> This repo is authored to render cleanly in **both Obsidian and GitHub** — relative links, standard Markdown, and GitHub-style alert callouts throughout.

---

## Start here — what do you need to do?

| I need to… | Go to |
|---|---|
| **Diagnose a job that freezes / runs forever** at scale | [Large-Scale Tuning & Troubleshooting Runbook](docs/performance/large-scale-tuning.md) |
| **Scan a quick checklist** of every knob that matters | [Performance Cheat Sheet](docs/performance/cheatsheet.md) |
| **Tune AAP 2.5 on OpenShift** for 500–5000-host inventories | [OpenShift Large-Inventory Tuning](docs/performance/openshift-large-inventory.md) |
| **Plan, install, or audit** a new AAP-on-OpenShift deployment | [Architect's Field Guide](docs/architecture/aap-on-openshift-architects-guide.md) |
| Understand why **only N pods run** when I set Job Slicing high | [Job Slicing & Concurrency](docs/concurrency/job-slicing-and-concurrency.md) |
| **Collect data from every host into one CSV** | [Aggregating Inventory Data to CSV](docs/patterns/aggregating-inventory-to-csv.md) |
| **Time roles** or bound runaway tasks with a timeout | [Role Timing & Timeouts](docs/patterns/role-timing-and-timeouts.md) |
| **Copy-paste a config block** (`ansible.cfg`, `pod_spec`, PG, OS limits) | [Config Snippets reference](reference/config-snippets.md) |
| **Discover what's actually on the network** (live hosts, SSH, OS) | [identify-hosts toolkit](toolkits/identify-hosts/) |

---

## Version coverage

AAP's deployment story shifted across releases (RPM installer deprecated in 2.5, removed in 2.7; Platform Gateway + Redis introduced in 2.5). Each doc states the versions it targets — at a glance:

| AAP version | Where it's covered |
|---|---|
| **2.4** | [Cheat Sheet](docs/performance/cheatsheet.md), [Large-Scale Runbook](docs/performance/large-scale-tuning.md), [Slicing & Concurrency](docs/concurrency/job-slicing-and-concurrency.md) |
| **2.5** | All of the above + [OpenShift Large-Inventory Tuning](docs/performance/openshift-large-inventory.md) |
| **2.6** (and 2.7 preview) | [Architect's Field Guide](docs/architecture/aap-on-openshift-architects-guide.md) |

> [!IMPORTANT]
> AAP 2.4 reaches end of Maintenance Support on **30 June 2026**. If you're still on 2.4, the [Architect's Field Guide](docs/architecture/aap-on-openshift-architects-guide.md) covers the migration target.

---

## Repository map

```
.
├── docs/
│   ├── architecture/   AAP-on-OpenShift design: plan, install, audit, operate (2.6)
│   ├── performance/     scale tuning & troubleshooting — cheat sheet, runbook, OCP deep-dive
│   ├── concurrency/      job slicing and what really governs parallelism
│   └── patterns/          reusable playbook patterns (CSV aggregation, role timing)
├── reference/
│   └── config-snippets.md   canonical config blocks + the valid variants and trade-offs
└── toolkits/
    └── identify-hosts/    runnable nmap-gated fleet discovery → one CSV
```

---

## Conventions

- **Living documents** — these evolve; the `status` frontmatter and dates reflect the last substantive update.
- **One source of truth for config** — recurring blocks (the EE `ansible.cfg`, container-group `pod_spec_override`, PostgreSQL tuning, OS limits) live once in [`reference/config-snippets.md`](reference/config-snippets.md). The guides keep a short, context-specific snippet and link there for the full block.
- **Show the alternatives** — where there's more than one valid way to achieve something (fact-cache backends, CSV aggregation strategies, task-timeout mechanisms), the docs present each with its trade-offs rather than prescribing one.
