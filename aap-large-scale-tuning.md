---
title: AAP Large-Scale Job Tuning & Troubleshooting Runbook
type: runbook
product: Red Hat Ansible Automation Platform
versions: [AAP 2.4, AAP 2.5]
deployments:
  - VM RPM installer (AAP 2.4 and 2.5)
  - VM containerized installer (AAP 2.5)
  - OpenShift Operator (AAP 2.4 — AutomationController CR)
  - OpenShift Operator (AAP 2.5 — AnsibleAutomationPlatform CR with Platform Gateway)
audience: senior platform / automation engineers
tags: [aap, ansible, openshift, performance, troubleshooting]
status: living-document
---

# AAP Large-Scale Job Tuning & Troubleshooting

> [!summary]
> Symptom: an Ansible job in **Red Hat Ansible Automation Platform** runs against hundreds or thousands of hosts and appears to **freeze mid-run**. Slicing the inventory into ~30-host batches works.
>
> This runbook covers how to **diagnose**, **explain**, and **tune** AAP for large-scale jobs across all four supported deployment topologies:
>
> | # | Topology | Versions |
> |---|---|---|
> | 1 | **VM — RPM installer** (systemd services on RHEL) | AAP 2.4, AAP 2.5 (deprecated in 2.5) |
> | 2 | **VM — containerized installer** (Podman containers on RHEL) | AAP 2.5 only |
> | 3 | **OpenShift Operator — `AutomationController` CR** | AAP 2.4 |
> | 4 | **OpenShift Operator — `AnsibleAutomationPlatform` CR + Platform Gateway** | AAP 2.5 |
>
> The product is AAP — not stand-alone Ansible — so the failure modes include the **Controller task system, callback receiver, dispatcher, Receptor mesh, execution environments, Redis (2.5), and PostgreSQL**, not just `ansible-playbook` and SSH.

> [!info] Version-mode legend
> Throughout this document, blocks are tagged with one of:
> `[2.4 RPM]` · `[2.5 RPM]` · `[2.5 Containerized]` · `[2.4 OCP]` · `[2.5 OCP]`
> Where instructions apply to all topologies, no tag is shown.

---

<a id="sec-1"></a>
## 1. Architecture Recap

<a id="sec-1-1"></a>
### 1.1 What runs where

| Component | `[2.4 RPM]` | `[2.5 RPM]` | `[2.5 Containerized]` | `[2.4 OCP]` | `[2.5 OCP]` |
|---|---|---|---|---|---|
| Web / API entry | `automation-controller` (NGINX) | `automation-controller` + new platform gateway | platform gateway container | `<ctrl>-web` Deployment | platform gateway Deployment |
| Task / dispatcher / callback receiver | `automation-controller` task service | same | `automation-controller-task` container | `<ctrl>-task` Deployment | `<aap>-controller-task` Deployment |
| Job execution | Podman EE on the controller / execution node | same | Podman EE inside the AAP host (sibling container) | EE in `automation-job-<id>` Pod | EE in `automation-job-<id>` Pod |
| Mesh | Receptor between control / hop / execution nodes | same | Receptor between containers and execution nodes | Receptor over the cluster network | same |
| Database | PostgreSQL on a managed/external RHEL host | same | PostgreSQL container | PostgreSQL Pod (managed) or external | same |
| **Redis** (cache / pub-sub) | not present | RPM-installed Redis | Redis container | not present | Redis Deployment |
| **Platform Gateway** (unified UI / SSO) | not present | yes (2.5 unified UI) | yes | not present | yes |

> [!important]
> Two architectural shifts in 2.5 matter for scale:
> 1. **Platform Gateway** is now the entry point and SSO/auth layer. It can become a bottleneck for the WebSocket event stream that the UI subscribes to.
> 2. **Redis** is now used as cache and pub-sub. A degraded Redis manifests as UI lag, missing events, or dispatcher stalls — symptoms that look like "freeze."

<a id="sec-1-2"></a>
### 1.2 The event pipeline

```
            ansible-playbook (in EE)
                    │  stdout events (JSON)
                    ▼
              ansible-runner
                    │  Receptor stream
                    ▼
controller-task ── callback receiver ──▶ PostgreSQL (main_jobevent)
                                    │
                                    ├──▶ Redis pub-sub  [2.5 only]
                                    └──▶ websocket
                                              │
                              ┌───────────────┴───────────────┐
                              ▼                               ▼
                      controller-web [2.4]          platform-gateway [2.5]
                              │                               │
                              └──────────────▶ UI ◀───────────┘
```

> [!important]
> A "frozen" job in AAP is almost never a frozen `ansible-playbook` process. It is usually one of:
> 1. The **EE process / pod** hitting CPU, memory, or fd limits.
> 2. The **callback receiver** falling behind on event ingestion.
> 3. **PostgreSQL** I/O saturation on `main_jobevent` writes.
> 4. **Redis** (2.5) saturation or connectivity issue.
> 5. **Receptor** backpressure between control and execution.
> 6. **Platform Gateway** (2.5) WebSocket stall.
> 7. The classic Ansible-level issues (`linear` strategy, low forks, SSH ControlPersist).

---

<a id="sec-2"></a>
## 2. Diagnostic Workflow — VM-based AAP

<a id="sec-2-1"></a>
### 2.1 `[2.4 RPM]` and `[2.5 RPM]` — systemd path

#### Triage while the job is "frozen"

- [ ] Identify the controller / execution node actually running the job (Jobs → Details → **Execution Node**).
- [ ] SSH to that node.

```bash
# Service health
sudo systemctl status automation-controller
sudo systemctl status receptor-awx           # mesh service
sudo systemctl status nginx                  # API/web frontend

# Recent logs (2.4 and 2.5 RPM both use journald)
sudo journalctl -u automation-controller --since "10 min ago" | \
  grep -iE 'callback|dispatcher|capacity|oom|killed|timeout|error'

# Is the EE container alive and consuming CPU?
sudo podman ps --filter "label=ansible_runner" --format \
  "table {{.ID}} {{.Names}} {{.Status}} {{.Command}}"
sudo podman stats --no-stream

EE=$(sudo podman ps --filter "label=ansible_runner" -q | head -1)
sudo podman top "$EE"
sudo podman exec "$EE" ps -ef | grep -E 'ansible-playbook|python'
```

#### `[2.5 RPM]` only — additional services

```bash
sudo systemctl status automation-gateway     # platform gateway (2.5)
sudo systemctl status redis                  # 2.5 introduced Redis
redis-cli -a "$REDIS_PASS" ping
redis-cli -a "$REDIS_PASS" info clients
redis-cli -a "$REDIS_PASS" info memory
```

<a id="sec-2-2"></a>
### 2.2 `[2.5 Containerized]` — Podman path

In the 2.5 containerized installer, services run as Podman containers managed by **systemd user units** under the `aap` user (or whichever install user you chose). `systemctl --user` and `podman` are the correct entry points; `journalctl -u automation-controller` will return nothing.

```bash
# Become the AAP user (default name: aap)
sudo -iu aap

# Containers
podman ps --format "table {{.Names}} {{.Status}} {{.Image}}"
podman stats --no-stream

# Per-container logs
podman logs --tail=300 automation-controller-task
podman logs --tail=300 automation-controller-web
podman logs --tail=300 automation-gateway
podman logs --tail=300 receptor
podman logs --tail=300 redis

# systemd user units (the install creates these)
systemctl --user list-units --type=service | grep -E 'automation|receptor|redis|postgres'
systemctl --user status automation-controller-task.service
journalctl --user -u automation-controller-task.service --since "10 min ago"

# OS-level pressure on the host
top -b -n1 | head -25
free -h
iostat -xz 2 5
```

> [!tip]
> If `loginctl show-user aap | grep Linger` reports `Linger=no`, user services stop when the user logs out. That alone has caused mysterious post-reboot "freezes" on containerized 2.5 — fix with `sudo loginctl enable-linger aap`.

<a id="sec-2-3"></a>
### 2.3 Callback receiver / dispatcher / capacity (all VM modes)

Run the same `awx-manage` commands but from the right shell:

```bash
# [2.4 RPM] / [2.5 RPM]
sudo awx-manage callback_stats
sudo awx-manage run_dispatcher --status
sudo awx-manage list_instances
sudo awx-manage list_instance_groups

# [2.5 Containerized] — exec into the task container
sudo -iu aap podman exec -it automation-controller-task awx-manage callback_stats
sudo -iu aap podman exec -it automation-controller-task awx-manage list_instances
```

> [!tip]
> If `callback_stats` shows a growing backlog while the EE container is idle and the playbook has finished, the bottleneck is **callback receiver → PostgreSQL** (or **→ Redis** in 2.5), not Ansible.

<a id="sec-2-4"></a>
### 2.4 PostgreSQL pressure

```bash
# [2.4 RPM] / [2.5 RPM]
sudo -u postgres psql -d awx -c \
  "SELECT count(*) FROM main_jobevent WHERE job_id = <job_id>;"
sudo -u postgres psql -d awx -c \
  "SELECT pid, state, wait_event_type, wait_event, query_start, left(query,80)
   FROM pg_stat_activity WHERE datname='awx' ORDER BY query_start;"

# [2.5 Containerized]
sudo -iu aap podman exec -it postgresql psql -U awx -d awx -c \
  "SELECT count(*) FROM main_jobevent WHERE job_id = <job_id>;"

# Disk and WAL pressure (host level on RPM, host or container volume on containerized)
df -h /var/lib/pgsql        # RPM
df -h ~aap/aap/postgresql/  # Containerized (path varies)
iostat -xz 2 5
```

<a id="sec-2-5"></a>
### 2.5 Receptor mesh

```bash
# [2.4 RPM] / [2.5 RPM]
sudo receptorctl --socket /var/run/awx-receptor/receptor.sock status
sudo receptorctl --socket /var/run/awx-receptor/receptor.sock ping <node>
sudo journalctl -u receptor-awx --since "30 min ago" | \
  grep -iE 'error|backpressure|disconnect'

# [2.5 Containerized]
sudo -iu aap podman exec -it receptor receptorctl status
```

<a id="sec-2-6"></a>
### 2.6 SSH / fd exhaustion on the EE host

```bash
EE_PID=$(pgrep -f ansible-playbook | head -1)
ls /proc/$EE_PID/fd | wc -l
ls ~awx/.ansible/cp/ 2>/dev/null | wc -l   # [2.4 RPM] / [2.5 RPM]
ls ~aap/.ansible/cp/ 2>/dev/null | wc -l   # [2.5 Containerized] (if EE shares the home)
ss -s
ulimit -n
```

---

<a id="sec-3"></a>
## 3. Diagnostic Workflow — AAP on OpenShift

> [!important]
> Pod and Deployment names differ between 2.4 and 2.5:
> - **`[2.4 OCP]`** — Operator manages an `AutomationController` named e.g. `controller`. Deployments: `controller-web`, `controller-task`. Hub is a separate `AutomationHub` CR.
> - **`[2.5 OCP]`** — Operator manages a single `AnsibleAutomationPlatform` named e.g. `myaap`. Deployments are prefixed with that name and the component, e.g. `myaap-controller-task`, `myaap-gateway`, `myaap-eda-api`, `myaap-redis`.

<a id="sec-3-1"></a>
### 3.1 First-pass triage while the job is "frozen"

```bash
NS=aap   # adjust

# Find the live job Pod (label is the same in 2.4 and 2.5)
oc -n $NS get pods -l ansible-awx-job-id \
   --sort-by=.metadata.creationTimestamp

# Resource pressure
oc -n $NS adm top pod <automation-job-pod>
oc -n $NS describe pod <automation-job-pod> | \
   grep -A3 -E 'Limits|Requests|State|Last State|Reason|Message'
```

> [!warning]
> If `Last State` shows `Terminated / OOMKilled`, that is the answer. Bump the **container group pod spec** memory limit (see [§6.4](#sec-6-4)). The default is far too small for 300+ hosts of fact gathering.

<a id="sec-3-2"></a>
### 3.2 Inspect the running EE process inside the job Pod

```bash
oc -n $NS rsh <automation-job-pod>
# inside the pod:
ps -ef | grep -E 'ansible-runner|ansible-playbook'
ls /runner/artifacts/<job_id>/ 2>/dev/null
tail -f /runner/artifacts/<job_id>/stdout
```

<a id="sec-3-3"></a>
### 3.3 Controller task pod (callback receiver / dispatcher)

```bash
# [2.4 OCP]
TASK_DEPLOY=deploy/controller-task
# [2.5 OCP]
TASK_DEPLOY=deploy/myaap-controller-task   # adjust the AAP name

oc -n $NS logs $TASK_DEPLOY -c <task-container> --tail=300 | \
   grep -iE 'callback|dispatcher|capacity|task manager|OOM|redis'

oc -n $NS rsh $TASK_DEPLOY awx-manage callback_stats
oc -n $NS rsh $TASK_DEPLOY awx-manage run_dispatcher --status
oc -n $NS rsh $TASK_DEPLOY awx-manage list_instances
oc -n $NS rsh $TASK_DEPLOY awx-manage list_instance_groups
```

<a id="sec-3-4"></a>
### 3.4 PostgreSQL Pod

```bash
PG_LABEL='app.kubernetes.io/name=postgres'   # works for both, verify in your cluster

oc -n $NS get pods -l $PG_LABEL
oc -n $NS adm top pod -l $PG_LABEL

oc -n $NS rsh <postgres-pod> psql -d awx -c \
  "SELECT count(*) FROM main_jobevent WHERE job_id=<id>;"
oc -n $NS rsh <postgres-pod> psql -d awx -c \
  "SELECT pid, state, wait_event_type, wait_event, query_start, left(query,80)
   FROM pg_stat_activity WHERE datname='awx' ORDER BY query_start;"
```

<a id="sec-3-5"></a>
### 3.5 `[2.5 OCP]` — Redis and Platform Gateway

These do not exist in 2.4. In 2.5 they are first-class failure surfaces:

```bash
# Redis
oc -n $NS get pods -l app.kubernetes.io/name=redis
oc -n $NS adm top pod -l app.kubernetes.io/name=redis
oc -n $NS rsh <redis-pod> redis-cli ping
oc -n $NS rsh <redis-pod> redis-cli info clients
oc -n $NS rsh <redis-pod> redis-cli info memory

# Platform Gateway
oc -n $NS logs deploy/myaap-gateway --tail=200 | \
   grep -iE 'websocket|timeout|upstream|error'
oc -n $NS adm top pod -l app.kubernetes.io/component=gateway
```

<a id="sec-3-6"></a>
### 3.6 Receptor inside the controller task pod

```bash
# [2.4 OCP]
oc -n $NS rsh deploy/controller-task \
  receptorctl --socket /var/run/receptor/receptor.sock status

# [2.5 OCP]
oc -n $NS rsh deploy/myaap-controller-task \
  receptorctl --socket /var/run/receptor/receptor.sock status
```

<a id="sec-3-7"></a>
### 3.7 Cluster-level pressure

```bash
oc -n $NS get events --sort-by=.lastTimestamp | tail -30
oc adm top nodes
oc -n $NS describe quota
oc -n $NS describe limitrange
```

> [!tip]
> A pod that schedules but never starts often means a **ResourceQuota** or **LimitRange** is rejecting the requested CPU/memory. Always check both before assuming it is an AAP problem.

---

<a id="sec-4"></a>
## 4. Possible Issues Catalogue

Each issue lists: **symptom → cause → where it bites → fix reference**.

<a id="sec-4-1"></a>
### 4.1 Ansible-level (applies everywhere)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| A1 | Job stops progressing on one task; some hosts never report | Default `linear` strategy waits for slowest host; one stuck SSH = whole play stalls | [§7.1](#sec-7-1) — set `strategy: free` or `strategy: host_pinned` |
| A2 | Long total runtime, low parallelism | Default `forks=5` (Ansible) or template-level forks too low | [§6.5](#sec-6-5) / [§7.1](#sec-7-1) — raise forks |
| A3 | Fact gathering takes minutes | No fact caching, full `setup` on every run | [§7.2](#sec-7-2) — enable fact cache, narrow `gather_subset` |
| A4 | Sporadic SSH timeouts at scale | ControlPersist socket churn, 108-char Unix socket path limit, fd exhaustion | [§7.3](#sec-7-3) — short `control_path_dir`, raise `nofile` |
| A5 | Huge stdout, slow UI | `verbosity >= 2` at 1000 hosts → multi-GB events | [§6.5](#sec-6-5) — verbosity 0–1, `no_log` on noisy loops |

<a id="sec-4-2"></a>
### 4.2 AAP Controller-level (all topologies)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| C1 | Job sits in **Pending** / **Waiting** | Instance group capacity = 0 or saturated | [§6.2](#sec-6-2) — capacity & forks; check `list_instances` |
| C2 | UI shows job "Running" but no new events for minutes; playbook actually finished | Callback receiver backlog into PostgreSQL | [§6.1](#sec-6-1) / [§6.3](#sec-6-3) — scale `task` replicas, tune PG |
| C3 | Job killed mid-run with no clear error | Job memory exceeds container limit (OCP) or `ANSIBLE_CALLBACK_PLUGINS` OOM on controller (VM) | [§6.4](#sec-6-4) — raise EE / pod memory |
| C4 | "Capacity adjustment" too low | `SYSTEM_TASK_FORKS_CPU` / `SYSTEM_TASK_FORKS_MEM` defaults underestimate node | [§6.2](#sec-6-2) |
| C5 | Long `main_jobevent` insert times | PostgreSQL on slow storage, no autovacuum tuning, no partition pruning | [§6.3](#sec-6-3) |

<a id="sec-4-3"></a>
### 4.3 `[2.4 RPM]` / `[2.5 RPM]` specific

| # | Symptom | Cause | Fix |
|---|---|---|---|
| V1 | Single controller node pegged at 100 % CPU during large jobs | All execution co-located on control plane | [§5.1](#sec-5-1) — add **execution nodes** |
| V2 | Mesh disconnects during long jobs | Receptor TLS/keepalive, MTU mismatch on tunnels | [§5.1](#sec-5-1) — `receptorctl ping`, MTU, firewall |
| V3 | Disk fills `/var/lib/awx/job_status/` or `/var/log` | Verbose runs + retained artifacts | [§6.3](#sec-6-3) — retention, verbosity |
| V4 | EE container can't reach managed hosts | Podman networking / proxy not propagated into EE | [§7.4](#sec-7-4) — `--container-options`, env in EE |

<a id="sec-4-4"></a>
### 4.4 `[2.5 Containerized]` specific

| # | Symptom | Cause | Fix |
|---|---|---|---|
| K1 | All AAP services dead after reboot | `loginctl enable-linger` not set on the install user | `sudo loginctl enable-linger aap` |
| K2 | Containers OOM-killed under load | Default Podman cgroup limits / host overcommit | Raise memory in inventory `*_extra_args`; verify `MemoryHigh` on `.service` units |
| K3 | Slow disk on `/home/aap/...` | Install on a single root volume without provisioned IOPS | Move data dirs to a fast volume, re-link |
| K4 | Redis log shows `MISCONF` | Disk full / fsync errors | Free space, set `stop-writes-on-bgsave-error no` only if you accept the risk |
| K5 | Receptor container disconnects | Host firewalld blocks Podman bridge | Open required ports / move to `network=host` only if intentional |

<a id="sec-4-5"></a>
### 4.5 `[2.4 OCP]` / `[2.5 OCP]` common

| # | Symptom | Cause | Fix |
|---|---|---|---|
| O1 | Job pod `OOMKilled` after a few minutes | Container group pod spec memory limit too low for fact volume | [§6.4](#sec-6-4) — raise `pod_spec_override` limits |
| O2 | Job pod `Pending` forever | Namespace `ResourceQuota` / `LimitRange`, or no node fits the request | [§3.7](#sec-3-7) |
| O3 | Controller `task` pod restarts during big runs | `task_resource_requirements` too low; callback receiver OOM | [§6.1](#sec-6-1) |
| O4 | PostgreSQL pod IOPS-bound | Default block-storage class at low baseline IOPS; `main_jobevent` write-heavy | [§6.3](#sec-6-3) — provisioned IOPS, separate WAL |
| O5 | Receptor stream stalls | `controller-task` pod CPU throttled, NetworkPolicy drops long-lived connections | [§6.1](#sec-6-1) — raise CPU, audit NetworkPolicies |
| O6 | EE image pull slow at job start | No image pre-pull / no registry mirror | [§6.4](#sec-6-4) — `imagePullPolicy`, mirrored registry |

<a id="sec-4-6"></a>
### 4.6 `[2.5 OCP]` only (Platform Gateway / Redis)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| G1 | UI hangs but jobs complete in DB | Platform Gateway WebSocket buffer saturated | Scale `gateway` replicas; raise `gateway_resource_requirements` |
| G2 | Login storms time out at scale | Gateway auth backed by external IdP saturating under WebSocket churn | Tune IdP timeouts, increase gateway CPU |
| G3 | Dispatcher logs show `ConnectionError: redis` | Redis pod CPU/memory pressure or NetworkPolicy | Scale Redis pod; verify `app.kubernetes.io/name=redis` reachable from `controller-task` |
| G4 | Sporadic event loss in UI | Redis pub-sub backlog | `redis-cli info clients`, `info memory`; raise Redis limits |

---

<a id="sec-5"></a>
## 5. Workflows

<a id="sec-5-1"></a>
### 5.1 Workflow A — VM RPM (`[2.4 RPM]` / `[2.5 RPM]`)

> [!example] Goal
> Run a 1000-host playbook reliably from an RPM-installed AAP cluster.

#### Topology recommendation

- **At least one dedicated execution node** (do not run large jobs on the control plane).
- Hop nodes if traversing network zones.
- PostgreSQL on its own host, with fast local storage (NVMe / equivalent).
- Time-synced (`chrony`) across all nodes — Receptor is sensitive to clock skew.
- `[2.5 RPM]`: Redis on its own node (or co-located on the gateway node) with sufficient RAM.

#### Sizing baseline

| Role | vCPU | RAM | Disk |
|---|---|---|---|
| Control plane (per node) | 4–8 | 16–32 GB | 60 GB OS + 200 GB `/var/lib/awx` |
| Execution node (per node) | 8–16 | 32–64 GB | 100 GB |
| PostgreSQL | 8 | 32 GB | NVMe ≥ 300 GB, separate WAL volume if possible |
| Platform Gateway `[2.5]` | 4 | 8 GB | 40 GB |
| Redis `[2.5]` | 2 | 8 GB | 40 GB |

<a id="sec-5-2"></a>
### 5.2 Workflow B — VM Containerized (`[2.5 Containerized]`)

> [!example] Goal
> Run a 1000-host playbook on the AAP 2.5 containerized installer (the strategic install path going forward).

#### Topology recommendation

- Use the **enterprise topology** inventory file from the bundle for production scale (the **growth/all-in-one** topology is for labs and small teams).
- Same logical layout as RPM — controller, hub, gateway, EDA, database — but each is a Podman container managed by a `systemd --user` unit.
- **`loginctl enable-linger`** for the install user is mandatory.
- Persist data directories on a fast volume mounted into the install path; do not rely on the root volume.
- Mirror or pre-pull EE images locally.

#### Sizing baseline

Same per-role sizing as Workflow A, plus:

- Allocate host RAM with the assumption that **all containers share the host page cache** — over-provision RAM by ~20 % vs RPM equivalent.
- `MemoryHigh=` and `MemoryMax=` on systemd user units protect noisy-neighbor behaviour between containers on the same host.

#### Test plan (Workflows A and B)

- [ ] Run the **fact-gathering-only** playbook against 50 → 200 → 500 → 1000 hosts.
- [ ] Track per-step: total runtime, peak EE RSS, callback lag, PG `main_jobevent` insert time.
- [ ] `[2.5]`: also track Redis client count and memory.
- [ ] Apply settings from [§6](#sec-6) and [§7](#sec-7) incrementally — change one variable at a time.
- [ ] Use **Job Slicing** as a fallback (template setting; built into AAP), not manual splitting.

<a id="sec-5-3"></a>
### 5.3 Workflow C — OpenShift `[2.4 OCP]` (`AutomationController` CR)

> [!example] Goal
> Same 1000-host run, on AAP 2.4 deployed via the Operator.

- Single-component CRDs: `AutomationController` for Controller, `AutomationHub` for Hub.
- No Platform Gateway, no Redis.
- Container group → job pod path is identical to 2.5.

<a id="sec-5-4"></a>
### 5.4 Workflow D — OpenShift `[2.5 OCP]` (`AnsibleAutomationPlatform` CR + Platform Gateway)

> [!example] Goal
> Same 1000-host run, on AAP 2.5 deployed via the Operator.

- Single unified `AnsibleAutomationPlatform` CR (`aap.ansible.com/v1alpha1`) with `spec.controller`, `spec.hub`, `spec.eda` subspecs.
- **Platform Gateway** Deployment fronts everything; tune it explicitly at scale.
- **Redis** Deployment is part of the platform; tune it explicitly at scale.

#### Topology recommendation (C and D)

- Dedicated **container group** for large jobs, scheduled to a worker MachineSet sized for the EE pod.
- Node selector / taint so job pods do not co-locate with the control plane.
- StorageClass with **provisioned IOPS** for PostgreSQL (e.g. `io2` on AWS, `Premium SSD v2` on Azure, equivalent on-prem CSI). Separate PVC for WAL if the CSI supports it.
- Internal registry or mirrored registry hosting the EE image so pod start is fast.
- `[2.5 OCP]`: also place Redis on a node with adequate RAM headroom; do not let it share a node with PG WAL writes.

#### Sizing baseline (Operator CRs)

See [§6.1](#sec-6-1) for the full CR snippets in both API versions. Starting point:

| Component | requests | limits |
|---|---|---|
| `controller-task` | 1 CPU / 4 GiB | 4 CPU / 8 GiB |
| `controller-web` | 500m / 2 GiB | 2 CPU / 4 GiB |
| Default control-plane EE | 500m / 2 GiB | 2 CPU / 4 GiB |
| **Job pod (container group)** | **1 CPU / 4 GiB** | **4 CPU / 8 GiB** |
| PostgreSQL | 500m / 2 GiB | 2 CPU / 8 GiB |
| Platform Gateway `[2.5 OCP]` | 500m / 1 GiB | 2 CPU / 4 GiB |
| Redis `[2.5 OCP]` | 250m / 1 GiB | 1 CPU / 4 GiB |

#### Test plan (Workflows C and D)

- [ ] Confirm `oc adm top nodes` headroom before testing.
- [ ] Run fact-gathering playbook at 50 → 200 → 500 → 1000 hosts.
- [ ] After each run: `oc describe pod <job-pod>` → look at `Last State`, `Reason`, peak memory.
- [ ] `awx-manage callback_stats` during the run from inside the task deployment.
- [ ] Watch `oc adm top pod` for `controller-task` and the PG pod.
- [ ] `[2.5 OCP]`: also watch the gateway and Redis pods.
- [ ] Apply [§6](#sec-6) / [§7](#sec-7) settings incrementally.

---

<a id="sec-6"></a>
## 6. Settings Reference

<a id="sec-6-1"></a>
### 6.1 Operator CRs — both API versions

#### `[2.4 OCP]` — `AutomationController` (and separate `AutomationHub`)

```yaml
apiVersion: automationcontroller.ansible.com/v1beta1
kind: AutomationController
metadata:
  name: controller
  namespace: aap
spec:
  replicas: 1                      # number of controller-web pods (older field)
  task_replicas: 2
  web_replicas: 2

  task_resource_requirements:
    requests: { cpu: "1",   memory: "4Gi" }
    limits:   { cpu: "4",   memory: "8Gi" }

  web_resource_requirements:
    requests: { cpu: "500m", memory: "2Gi" }
    limits:   { cpu: "2",   memory: "4Gi" }

  ee_resource_requirements:
    requests: { cpu: "500m", memory: "2Gi" }
    limits:   { cpu: "2",   memory: "4Gi" }

  postgres_resource_requirements:
    requests: { cpu: "500m", memory: "2Gi" }
    limits:   { cpu: "2",   memory: "8Gi" }
```

Hub is a separate CR (`AutomationHub`) — not relevant to job throughput.

#### `[2.5 OCP]` — unified `AnsibleAutomationPlatform`

```yaml
apiVersion: aap.ansible.com/v1alpha1
kind: AnsibleAutomationPlatform
metadata:
  name: myaap
  namespace: aap
spec:
  # Platform Gateway — new in 2.5
  gateway:
    disabled: false
    replicas: 2
    resource_requirements:
      requests: { cpu: "500m", memory: "1Gi" }
      limits:   { cpu: "2",   memory: "4Gi" }

  controller:
    disabled: false
    task_replicas: 2
    web_replicas: 2

    task_resource_requirements:
      requests: { cpu: "1",   memory: "4Gi" }
      limits:   { cpu: "4",   memory: "8Gi" }

    web_resource_requirements:
      requests: { cpu: "500m", memory: "2Gi" }
      limits:   { cpu: "2",   memory: "4Gi" }

    ee_resource_requirements:
      requests: { cpu: "500m", memory: "2Gi" }
      limits:   { cpu: "2",   memory: "4Gi" }

  hub:
    disabled: false                # set to true if you do not use private hub

  eda:
    disabled: true                 # set to false if you use Event-Driven Ansible

  # Redis — new in 2.5
  redis:
    resource_requirements:
      requests: { cpu: "250m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "4Gi" }

  # Database
  postgres_resource_requirements:
    requests: { cpu: "500m", memory: "2Gi" }
    limits:   { cpu: "2",   memory: "8Gi" }
```

> [!note]
> Field names under `spec.controller` in the 2.5 unified CR mirror those in the 2.4 `AutomationController` CR. The lift-and-shift is mostly mechanical — you indent the existing 2.4 spec under `spec.controller` and add `gateway`, `redis`, `eda`, `hub` siblings.

<a id="sec-6-2"></a>
### 6.2 Controller settings (UI / API — applies to all topologies)

Available under **Settings → Jobs** (UI) or via API / `awx-manage settings_set`:

| Setting | Default | Recommended for 1000-host jobs | Notes |
|---|---|---|---|
| `DEFAULT_FORKS` | 5 | 50–100 | Per-template Forks overrides this |
| `MAX_FORKS` | 200 | 200–500 | Hard cap protecting the controller |
| `SYSTEM_TASK_FORKS_CPU` | 4 | leave default unless undersized | Capacity per CPU |
| `SYSTEM_TASK_FORKS_MEM` | 100 | leave default | MiB of RAM per fork |
| `EVENT_STDOUT_MAX_BYTES_DISPLAY` | 1024 | 1024 | Truncate huge events |
| `STDOUT_MAX_BYTES_DISPLAY` | 1048576 | 1048576 | UI display cap |
| `MAX_WEBSOCKET_EVENT_RATE_SECONDS` | 0 | 0.25–0.5 | Smooths UI streaming |
| `AWX_CLEANUP_PATHS` | true | true | Cleans `/tmp/awx_*` |

<a id="sec-6-3"></a>
### 6.3 PostgreSQL tuning

Same `postgresql.conf` knobs apply everywhere; values for a 1000-host workload:

```ini
shared_buffers = 4GB
work_mem = 32MB
maintenance_work_mem = 512MB
effective_cache_size = 12GB
max_connections = 1024
checkpoint_timeout = 15min
checkpoint_completion_target = 0.9
wal_compression = on
random_page_cost = 1.1          # for SSD/NVMe
log_min_duration_statement = 1000
autovacuum_vacuum_scale_factor = 0.05
autovacuum_analyze_scale_factor = 0.02
```

Also:

- [ ] Run `awx-manage cleanup_jobs --days=<N>` on a schedule (Management Job in the UI).
- [ ] Run `awx-manage cleanup_activitystream --days=<N>` similarly.
- [ ] OpenShift: ensure the PG PVC StorageClass provides predictable IOPS; consider `io2` / `Premium SSD v2` / NVMe-backed CSI.

<a id="sec-6-4"></a>
### 6.4 Execution Environment & job pod sizing

#### `[2.4 RPM]` / `[2.5 RPM]` / `[2.5 Containerized]`

The EE container runs under Podman on the execution node. Resource caps come from:

- The **instance / instance group** definition (Controller UI → Instance Groups).
- For the containerized installer, also the systemd user unit `MemoryHigh=` / `CPUQuota=` directives.

Bake a **custom EE** with `ansible-builder` that includes the `ansible.cfg` from [§7.1](#sec-7-1), your collections, and any private CA bundles.

#### `[2.4 OCP]` / `[2.5 OCP]` — container group `pod_spec_override`

Controller UI → **Instance Groups → Container Group → Customize pod spec**:

```yaml
apiVersion: v1
kind: Pod
metadata:
  namespace: aap
spec:
  serviceAccountName: default
  automountServiceAccountToken: false
  containers:
    - name: worker
      # [2.5] image:
      image: 'registry.redhat.io/ansible-automation-platform-25/ee-supported-rhel9:latest'
      # [2.4] image:
      # image: 'registry.redhat.io/ansible-automation-platform-24/ee-supported-rhel9:latest'
      imagePullPolicy: IfNotPresent
      args: ['ansible-runner', 'worker', '--private-data-dir=/runner']
      resources:
        requests:
          cpu: "1"
          memory: "4Gi"
        limits:
          cpu: "4"
          memory: "8Gi"
      env:
        - name: ANSIBLE_FORCE_COLOR
          value: "0"
```

> [!tip]
> The container group pod spec is the **single biggest lever** for the freeze symptom on OpenShift. Memory limit `<` peak fact-gather memory `=` `OOMKilled`.

<a id="sec-6-5"></a>
### 6.5 Job template settings (UI — all topologies)

Per-template:

- [ ] **Forks**: 50–100 (start at 50; raise once stable).
- [ ] **Job Slicing**: 4–10 (creates parallel sibling jobs across the inventory; capacity-aware).
- [ ] **Verbosity**: 0 or 1.
- [ ] **Limit**: keep empty for full inventory; use slicing instead of manual `--limit` splits.
- [ ] **Timeout**: explicit, e.g. 3600 s; never leave at 0 for production.
- [ ] **Concurrent jobs**: enable only if capacity has been tested.
- [ ] **Instance Groups**: pin large-fleet templates to the appropriately sized group.

<a id="sec-6-6"></a>
### 6.6 Receptor and mesh

VM (all RPM and containerized):

- [ ] Use TLS between control / hop / execution nodes.
- [ ] Verify MTU end-to-end (`tracepath`, `ping -M do -s 1472`).
- [ ] Open required ports (default `27199/tcp`); confirm with `receptorctl status`.

OpenShift (both 2.4 and 2.5):

- [ ] Ensure no NetworkPolicy blocks long-lived connections between `controller-task` and job pods.
- [ ] If using a service mesh, exclude AAP namespace from sidecar injection unless explicitly tested.

<a id="sec-6-7"></a>
### 6.7 `[2.5]` Platform Gateway and Redis

`[2.5 OCP]` — fields nested in the `AnsibleAutomationPlatform` CR:

```yaml
spec:
  gateway:
    replicas: 2                             # scale horizontally for WS fan-out
    resource_requirements:
      requests: { cpu: "500m", memory: "1Gi" }
      limits:   { cpu: "2",   memory: "4Gi" }

  redis:
    resource_requirements:
      requests: { cpu: "250m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "4Gi" }
```

`[2.5 RPM]` / `[2.5 Containerized]` — Redis is configured via installer inventory variables (`redis_*`). Set `redis_mode=standalone` only for small footprints; production should use the clustered/HA mode documented in the installer guide.

---

<a id="sec-7"></a>
## 7. Playbook & EE-level Settings

<a id="sec-7-1"></a>
### 7.1 `ansible.cfg` baked into the EE

```ini
[defaults]
strategy = free
forks = 100
gather_timeout = 30
timeout = 30
internal_poll_interval = 0.001
host_key_checking = False
callbacks_enabled = profile_tasks, timer
stdout_callback = default

fact_caching = jsonfile
fact_caching_connection = /runner/artifacts/fact_cache
fact_caching_timeout = 7200

[ssh_connection]
pipelining = True
ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o ServerAliveInterval=15 -o PreferredAuthentications=publickey
control_path_dir = /tmp/ansible-cp
control_path = %(directory)s/%%h-%%r
retries = 3
```

> [!note]
> On OpenShift the EE filesystem is ephemeral; `fact_caching_connection` should point under `/runner/artifacts/` (persisted for the run) or use `redis` against an in-cluster Redis for cross-run caching.
>
> `[2.5 OCP]`: the cluster already runs a Redis Pod for the platform itself. **Do not reuse it for fact caching** — stand up a separate Redis instance for fact caching to avoid cross-tenant noise.

<a id="sec-7-2"></a>
### 7.2 Fact gathering hygiene

```yaml
- hosts: all
  gather_facts: smart
  vars:
    ansible_facts_parallel: true
  pre_tasks:
    - name: Targeted fact gather
      ansible.builtin.setup:
        gather_subset:
          - '!all'
          - '!min'
          - network
          - virtual
        gather_timeout: 30
```

<a id="sec-7-3"></a>
### 7.3 OS-level limits on VM execution nodes

`/etc/security/limits.d/awx.conf` (RPM) or under the install user (containerized):

```
awx soft nofile 65535
awx hard nofile 65535
awx soft nproc  32768
awx hard nproc  32768
```

`/etc/sysctl.d/99-awx.conf`:

```
net.ipv4.ip_local_port_range = 15000 65000
net.core.somaxconn = 1024
net.ipv4.tcp_tw_reuse = 1
fs.file-max = 2097152
```

For `[2.5 Containerized]` also raise the user-level limits:

```ini
# /etc/systemd/system/user-1001.slice.d/override.conf  (UID of the install user)
[Slice]
TasksMax=infinity
```

<a id="sec-7-4"></a>
### 7.4 Proxy / corporate CA inside the EE

When managed hosts or Galaxy/automation hub are behind a proxy:

- [ ] Bake CA bundle into the EE under `/etc/pki/ca-trust/source/anchors/` and run `update-ca-trust` in the build.
- [ ] Set `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` via the job template **Extra Variables → environment** or via the container group pod spec env.

---

<a id="sec-8"></a>
## 8. Test Matrix

| Phase | Inventory size | Forks | Job Slicing | Verbosity | What to measure |
|---|---|---|---|---|---|
| Baseline | 50 | 25 | 1 | 0 | Sanity, end-to-end runtime |
| Step 1 | 200 | 50 | 1 | 0 | Linear scaling check |
| Step 2 | 500 | 50 | 2 | 0 | Slicing behaviour |
| Step 3 | 1000 | 100 | 4 | 0 | Target workload |
| Stress | 1000 | 100 | 4 | 1 | Verbosity impact on PG / callback lag |

For each row capture:

- [ ] Total runtime
- [ ] Peak EE / job-pod memory (`podman stats` or `oc adm top pod`)
- [ ] `awx-manage callback_stats` peak backlog
- [ ] PG `main_jobevent` row count delta and insert latency
- [ ] `[2.5]`: Redis client count and memory
- [ ] `[2.5 OCP]`: Platform Gateway pod CPU and active WebSocket count
- [ ] Any `OOMKilled` / `Terminated` events
- [ ] UI responsiveness during the run

---

<a id="sec-9"></a>
## 9. Decision Tree — "the job froze"

```
1. Is the EE process / job pod alive and consuming CPU?
   ├── No, OOMKilled / Terminated  ─▶ [§6.4](#sec-6-4) (raise EE / pod limits)
   ├── No, idle but UI says Running ─▶ [§6.2](#sec-6-2) + [§6.3](#sec-6-3) (callback receiver / PG / Redis)
   └── Yes, progressing slowly      ─▶ continue
2. Does callback_stats show backlog?
   ├── Yes ─▶ scale task replicas, tune PG, lower verbosity
   └── No  ─▶ continue
3. [2.5 only] Is Redis healthy?
   ├── No  ─▶ [§6.7](#sec-6-7), scale Redis, check NetworkPolicy
   └── Yes ─▶ continue
4. Is one host "stuck" in the play?
   ├── Yes ─▶ [§7.1](#sec-7-1) strategy: free; investigate that host
   └── No  ─▶ continue
5. Is capacity per instance/group sufficient?
   ├── No  ─▶ [§6.2](#sec-6-2), raise forks / add execution nodes / scale group
   └── Yes ─▶ continue
6. Mesh / network?
   ├── VM            ─▶ receptorctl status, MTU, firewall
   ├── [2.5 Containerized] ─▶ also check podman network and linger
   └── OCP            ─▶ NetworkPolicy, service mesh, node pressure
7. [2.5 OCP] UI lag but jobs finish?
   └── Platform Gateway; scale gateway replicas ([§6.7](#sec-6-7))
```

---

<a id="sec-10"></a>
## 10. Fast-Path Recommendations

> [!tip] If you only do five things
> 1. Set **template Forks = 50–100** and **Job Slicing = 4** instead of slicing manually.
> 2. Set **`strategy: free`** in your playbook (or `host_pinned`).
> 3. Add **fact caching** in the EE's `ansible.cfg` and use `gather_subset`.
> 4. **OpenShift**: raise the container group pod spec limits to **4 CPU / 8 GiB**.
>    **VM**: add a **dedicated execution node**.
> 5. Drop **verbosity to 0–1** for production, and put **PostgreSQL on fast storage**.

> [!tip] `[2.5]` add-ons
> 6. Confirm **Redis** is healthy and adequately sized (`redis-cli info clients|memory`).
> 7. `[2.5 OCP]` — give **Platform Gateway** at least 2 replicas with 2 CPU / 4 GiB limits.
> 8. `[2.5 Containerized]` — `loginctl enable-linger <install-user>`, and put data dirs on a fast volume.

---

<a id="sec-11"></a>
## 11. Version & Topology Quick Reference

| Question | `[2.4 RPM]` | `[2.5 RPM]` | `[2.5 Containerized]` | `[2.4 OCP]` | `[2.5 OCP]` |
|---|---|---|---|---|---|
| Install method | RPM bundle | RPM bundle (deprecated in 2.5) | Containerized installer (Podman) | Operator | Operator |
| Operator CRD | n/a | n/a | n/a | `automationcontroller.ansible.com/v1beta1` `AutomationController` | `aap.ansible.com/v1alpha1` `AnsibleAutomationPlatform` |
| Platform Gateway | no | yes | yes | no | yes |
| Redis | no | yes | yes | no | yes |
| Service control | `systemctl` | `systemctl` | `systemctl --user` + `podman` | `oc` | `oc` |
| Logs | `journalctl -u <unit>` | `journalctl -u <unit>` | `podman logs <ctr>` / `journalctl --user -u <unit>` | `oc logs deploy/<name>` | `oc logs deploy/<name>` |
| `awx-manage` | host root shell | host root shell | `podman exec -it automation-controller-task awx-manage …` | `oc rsh deploy/controller-task awx-manage …` | `oc rsh deploy/<aap>-controller-task awx-manage …` |
| Job pod / container | local Podman | local Podman | local Podman | `automation-job-<id>` Pod | `automation-job-<id>` Pod |

---

<a id="sec-12"></a>
## 12. References

- Red Hat — *AAP 2.4 Operator Installation Guide* — `AutomationController` CRD reference
- Red Hat — *AAP 2.5 Installing on OpenShift* — `AnsibleAutomationPlatform` CRD, Platform Gateway
- Red Hat — *AAP 2.5 Containerized Installation Guide* — growth and enterprise topologies
- Red Hat — *AAP Performance Considerations for Operator-based Installations*
- Red Hat — *Automation Controller Administration Guide* — Capacity, Job Slicing, Container Groups
- Ansible Project — *Strategies* (`linear`, `free`, `host_pinned`)
- ansible-runner / ansible-builder upstream documentation
- Receptor project documentation
