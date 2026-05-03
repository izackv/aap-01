---
title: AAP Job Slicing & Concurrency — Understanding Real Parallelism
type: guide
product: Red Hat Ansible Automation Platform
versions: [AAP 2.4, AAP 2.5]
deployments: [VM, OpenShift]
audience: AAP operators and platform engineers
tags: [aap, ansible, slicing, concurrency, capacity, container-groups, instance-groups]
status: living-document
---

# AAP Job Slicing & Concurrency — Understanding Real Parallelism

> [!summary]
> You set **Job Slicing = 20** and only **3 pods are running**. Why?
>
> The slice count tells AAP how many sibling jobs to *create*. Whether they actually *run in parallel* is a completely separate decision controlled by **instance-group capacity**, **container-group limits**, **Controller-wide caps**, and finally **OpenShift / VM resource availability**. This guide walks every gate that throttles real concurrency, in the order AAP evaluates them, and shows exactly where to change each one.

> [!info] What this guide assumes
> - You understand what a Job Template is and have used Job Slicing at least once.
> - You may be on AAP 2.4 or 2.5, VM-based or OpenShift. Differences are called out where they matter.
> - The default container group on OpenShift ships with conservative limits — you will likely need to change them.

---

<a id="sec-toc"></a>
## Contents

1. [The mental model — what slicing actually does](#sec-1)
2. [The five gates of real concurrency](#sec-2)
3. [Capacity math — how AAP decides what runs](#sec-3)
4. [Where to set each control (UI / API / CR)](#sec-4)
5. [Worked examples — how to size for N slices](#sec-5)
6. [Checking what's actually happening](#sec-6)
7. [Common symptoms and what they mean](#sec-7)
8. [Decision tree — "why aren't my slices running?"](#sec-8)
9. [Recipes for typical scenarios](#sec-9)
10. [Reference — every setting in one table](#sec-10)

---

<a id="sec-1"></a>
## 1. The Mental Model — What Slicing Actually Does

When you set **Job Slicing = N** on a Job Template:

1. The Controller creates a **Workflow Job** that contains **N child Job Templates**, each scoped to `1/N` of the inventory.
2. Each child is an **independent job** that gets queued, dispatched, and executed separately.
3. Each child consumes capacity **as if it were a normal job** — the workflow doesn't reserve N slots up front.
4. The workflow is "complete" when all N children finish (success or failure).

> [!important]
> Slicing does **not** mean "20 things run at once." It means "20 separate jobs are created, each will run when capacity allows." How many run in parallel is governed by the gates in [§2](#sec-2).

Implications:

- If your container group can run 3 concurrent jobs, a 20-slice template runs as **three waves of ~7** (3 → 3 → 3 → 3 → 3 → 3 → 2).
- The 17 waiting slices show up as `Pending` or `Waiting` in the Jobs list with the gear icon spinning.
- Slice ordering is not guaranteed — slice 7 may finish before slice 2 starts.

---

<a id="sec-2"></a>
## 2. The Five Gates of Real Concurrency

Every slice goes through five gates, in order. The narrowest gate sets your real concurrency.

```
┌────────────────────────────────────────────────────────────────┐
│ Gate 1: Job Template settings                                  │
│   Forks, Job Slicing, Concurrent Jobs Allowed                  │
└────────────────────────┬───────────────────────────────────────┘
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Gate 2: Instance Group / Container Group capacity              │
│   max_concurrent_jobs, max_forks                               │
└────────────────────────┬───────────────────────────────────────┘
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Gate 3: Controller-wide system caps                            │
│   MAX_FORKS, SYSTEM_TASK_FORKS_CPU, SYSTEM_TASK_FORKS_MEM      │
└────────────────────────┬───────────────────────────────────────┘
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Gate 4: Platform infrastructure                                │
│   OpenShift: ResourceQuota, LimitRange, node capacity          │
│   VM:        ulimits, ports, disk, controller node sizing      │
└────────────────────────┬───────────────────────────────────────┘
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Gate 5: External dependencies                                  │
│   SSH MaxStartups on managed hosts, SNAT/egress, DNS, vault    │
└────────────────────────────────────────────────────────────────┘
```

### 2.1 Gate 1 — Job Template

- **Forks** — concurrency *within* one slice. A slice with `Forks=50` runs 50 hosts in parallel inside that slice's pod.
- **Job Slicing** — number of sibling slices created. Doesn't say anything about parallelism between them.
- **Concurrent Jobs Allowed** (template-level toggle, "Allow Simultaneous") — required for slicing. If unchecked, the children serialize one-at-a-time.

### 2.2 Gate 2 — Instance / Container Group Capacity

This is where most "only 3 pods running" surprises live.

- **`max_concurrent_jobs`** — hard ceiling on parallel jobs running on this group. `0` means "unlimited (governed only by `max_forks`)."
- **`max_forks`** — total fork capacity across the whole group. Each running job consumes `template.forks + 1` from this pool.

A slice will dispatch only if **both** conditions are satisfied:

```
running_jobs_in_group  <  max_concurrent_jobs   (or max_concurrent_jobs == 0)
forks_in_use_in_group + (template.forks + 1)  ≤  max_forks
```

If either is exhausted, the slice waits.

### 2.3 Gate 3 — Controller-Wide Caps

System-wide settings that bound every group:

- **`MAX_FORKS`** — global hard cap on a single job's forks. Default 200.
- **`SYSTEM_TASK_FORKS_CPU`** (default 4) — capacity formula input: forks per CPU when auto-computing.
- **`SYSTEM_TASK_FORKS_MEM`** (default 100 MiB) — capacity formula input: forks per RAM unit when auto-computing.

These matter most when an instance group has `max_forks = 0` and capacity is auto-derived from node size.

### 2.4 Gate 4 — Platform Infrastructure

You can tell AAP "run 20 pods" but if OpenShift won't schedule them, they sit `Pending`.

**OpenShift:**

- **ResourceQuota** on the namespace — total CPU/memory cap.
- **LimitRange** — silently rewrites individual pod resource requests.
- **Node capacity** — sum of `requests` across pending+running pods must fit on labeled nodes.
- **PVC/CSI** — stuck PVCs block pod scheduling.
- **Image pull** — 20 simultaneous pulls of a 2GB EE serialize on node-local registry pulls without pre-pull.

**VM:**

- **`ulimit -n`** for the AAP user.
- **Ephemeral port range** (`net.ipv4.ip_local_port_range`).
- **Controller node CPU/RAM** — execution co-located with control plane will throttle.
- **Disk space** for `/var/lib/awx`, `/tmp/awx_*`, `/var/log`.

### 2.5 Gate 5 — External Dependencies

Even with everything above sized correctly, the *managed hosts* and the network in between can throttle effective parallelism:

- **`MaxStartups` in `sshd_config`** — default `10:30:100` rejects inbound SSH at high parallelism.
- **SNAT / egress IP** — connection-tracking limits silently drop SSH.
- **DNS** — high lookup volume can saturate small resolver tiers.
- **Vault / credential providers** — every job hits these once at start.

These don't block dispatch but cause flaky failures that look like "AAP couldn't run them properly."

---

<a id="sec-3"></a>
## 3. Capacity Math — How AAP Decides What Runs

### 3.1 Job impact

Each running job consumes:

```
job_impact = template.forks + 1
```

The `+1` is fixed overhead for the controller-task work performed alongside the job (event ingestion, dispatcher accounting, etc.).

### 3.2 Group capacity

For an instance group or container group:

```
group_capacity_forks       = max_forks                  (if set > 0)
                           = sum(instance_capacity)     (otherwise, auto-computed)

group_capacity_concurrent  = max_concurrent_jobs        (if set > 0)
                           = unbounded                  (otherwise)
```

For traditional execution-node instance groups, `instance_capacity` is auto-computed per-node as:

```
instance_capacity = min(
    node_cpu  / SYSTEM_TASK_FORKS_CPU,
    node_mem  / SYSTEM_TASK_FORKS_MEM
)
```

For **container groups on OpenShift**, the auto-computation does not apply the same way — capacity is whatever you set in `max_forks` and `max_concurrent_jobs`. Defaults are conservative; they're the reason "only 3 pods" is the canonical surprise.

### 3.3 Dispatch decision

A pending job is dispatched when:

```
forks_in_use + job_impact ≤ group_capacity_forks
AND
running_jobs < group_capacity_concurrent
```

Otherwise it waits. AAP re-evaluates whenever a job finishes.

### 3.4 Worked example

Setup:

- Template: `Forks = 50`, `Job Slicing = 20`, pinned to container group `large-fleet`.
- Container group: `max_concurrent_jobs = 0` (unlimited), `max_forks = 200`.

Per-slice impact: `50 + 1 = 51`.
Concurrent slices: `floor(200 / 51) = 3`.

→ **You see 3 pods running, 17 queued.** Even though `max_concurrent_jobs` is "unlimited," the forks math gates you to 3.

To run all 20 in parallel:

```
required_forks = 20 × (50 + 1) = 1020
```

Set `max_forks ≥ 1020` (round up to 1100 for headroom). Now all 20 dispatch simultaneously — assuming Gate 4 allows.

---

<a id="sec-4"></a>
## 4. Where to Set Each Control

### 4.1 Job Template

**UI**: Templates → `<your template>` → Edit

| Field | Default | Notes |
|---|---|---|
| Forks | 0 (uses `DEFAULT_FORKS`) | Per-slice parallelism |
| Job Slicing | 1 | Number of sibling slices |
| Concurrent Jobs (Allow Simultaneous) | unchecked | **Must be checked for slicing to run in parallel** |
| Instance Groups | inherits | Pin to your sized group |
| Timeout | 0 (none) | Always set explicitly for production |
| Verbosity | 0 | Keep low at scale |

**API** (`POST/PATCH /api/v2/job_templates/<id>/`):

```json
{
  "forks": 50,
  "job_slice_count": 20,
  "allow_simultaneous": true,
  "timeout": 3600,
  "verbosity": 0
}
```

### 4.2 Instance Group / Container Group

**UI**: Instance Groups → `<your group>` → Edit

| Field | Meaning |
|---|---|
| Max concurrent jobs | Hard ceiling on parallel jobs in this group. `0` = unlimited. |
| Max forks | Total fork capacity. Each job consumes `forks + 1`. |
| Pod spec override | (Container groups only) — Kubernetes Pod manifest used for job pods. |

**API** (`POST/PATCH /api/v2/instance_groups/<id>/`):

```json
{
  "name": "large-fleet",
  "max_concurrent_jobs": 20,
  "max_forks": 1100,
  "is_container_group": true,
  "pod_spec_override": "{ ... }"
}
```

> [!note]
> The Operator on OpenShift does **not** manage instance groups via the CR. They live in the controller's database and you set them via the Controller UI / API. Treat them as runtime configuration, not infra-as-code (or manage them via `awx.awx.instance_group` in a bootstrap playbook).

### 4.3 Controller-wide settings

**UI**: Settings → Jobs

| Setting | Default | Recommended |
|---|---|---|
| `DEFAULT_FORKS` | 5 | 50 |
| `MAX_FORKS` | 200 | 200–500 |
| `SYSTEM_TASK_FORKS_CPU` | 4 | leave default unless undersized |
| `SYSTEM_TASK_FORKS_MEM` | 100 | leave default |
| `AWX_ANSIBLE_CALLBACK_PLUGINS` | (empty) | leave default |
| `EVENT_STDOUT_MAX_BYTES_DISPLAY` | 1024 | 1024 |
| `MAX_WEBSOCKET_EVENT_RATE_SECONDS` | 0 | 0.5 |

**API** (`PATCH /api/v2/settings/jobs/`):

```json
{
  "DEFAULT_FORKS": 50,
  "MAX_FORKS": 300
}
```

### 4.4 OpenShift platform — `pod_spec_override`

Sized in the container group's Pod spec, not in the AAP CR:

```yaml
apiVersion: v1
kind: Pod
spec:
  serviceAccountName: default
  automountServiceAccountToken: false
  nodeSelector: { aap-workload: execution }
  tolerations:
    - { key: aap-workload, operator: Equal, value: execution, effect: NoSchedule }
  containers:
    - name: worker
      image: image-registry.openshift-image-registry.svc:5000/aap/custom-ee-rhel9:1.4.2
      imagePullPolicy: IfNotPresent
      args: ['ansible-runner', 'worker', '--private-data-dir=/runner']
      resources:
        requests: { cpu: "1", memory: "4Gi" }
        limits:   { cpu: "4", memory: "8Gi" }
```

The **`requests`** field × `max_concurrent_jobs` must fit within Gate 4 (ResourceQuota and node capacity).

### 4.5 OpenShift platform — namespace controls

These are independent of AAP and live with your cluster admin:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata: { name: aap-quota, namespace: aap }
spec:
  hard:
    requests.cpu: "40"
    requests.memory: 160Gi
    limits.cpu: "120"
    limits.memory: 320Gi
    pods: "60"
---
apiVersion: v1
kind: LimitRange
metadata: { name: aap-limits, namespace: aap }
spec:
  limits:
    - type: Container
      max:    { cpu: "8",   memory: 16Gi }
      min:    { cpu: 100m,  memory: 128Mi }
      default:        { cpu: "4", memory: 8Gi }
      defaultRequest: { cpu: "1", memory: 4Gi }
```

> [!warning]
> If `LimitRange.max` is below your `pod_spec_override.resources.limits`, **the LimitRange wins silently**. Pods schedule with the LimitRange-clamped values. Always check `oc describe limitrange` first when sizing seems off.

---

<a id="sec-5"></a>
## 5. Worked Examples — How to Size for N Slices

### 5.1 Goal: 20 slices, all running in parallel

Template: `Forks = 50`, `Job Slicing = 20`.

| Gate | Required setting | Reason |
|---|---|---|
| Gate 1 | "Allow Simultaneous" checked | Otherwise children serialize |
| Gate 2 | `max_concurrent_jobs ≥ 20`, `max_forks ≥ 1100` | `20 × (50+1) = 1020` + headroom |
| Gate 3 | `MAX_FORKS ≥ 50` | Per-job cap; 50 satisfies it |
| Gate 4 (OCP) | Quota allows `20 × pod_spec.requests` | `20 × 1 CPU = 20 CPU req`, `20 × 4Gi = 80Gi req` |
| Gate 4 (OCP) | LimitRange max ≥ pod_spec.limits | `4 CPU / 8Gi limits` must be allowed |
| Gate 4 (OCP) | Node capacity sums to ≥ 80 GiB on labeled nodes | Else pods pend |
| Gate 5 | Managed-host `MaxStartups 100:30:200` | Else random SSH rejection |

### 5.2 Goal: 20 slices, but cap at 10 concurrent (run in two waves)

Same template, smaller capacity. Useful when the cluster has limited headroom or downstream systems can't take the full hit.

| Gate | Setting |
|---|---|
| Gate 2 | `max_concurrent_jobs = 10`, `max_forks = 550` |

The first 10 slices dispatch immediately; the second 10 dispatch as the first wave's pods complete. Total wallclock is roughly `2 × per-slice runtime`.

### 5.3 Goal: 20 slices but only 3 should run at a time (your current state)

You see this when defaults haven't been changed.

| Gate | What's happening |
|---|---|
| Gate 2 | `max_concurrent_jobs = 0` (unlimited), `max_forks = 200` (default-ish), template forks `50` → `floor(200/51) = 3` |

Fix: raise `max_forks` per [§5.1](#sec-5) or [§5.2](#sec-5).

### 5.4 Goal: per-slice forks=10, 50 slices, run all in parallel

Different shape — many small slices instead of few large ones. Often kinder to managed hosts because each pod opens fewer simultaneous SSH connections.

| Gate | Setting |
|---|---|
| Gate 1 | Forks=10, Job Slicing=50 |
| Gate 2 | `max_concurrent_jobs ≥ 50`, `max_forks ≥ 50 × 11 + 50 = 600` |
| Gate 4 | OCP quota for 50 pods. Pod spec can be smaller (`requests: 500m / 2Gi`) |

---

<a id="sec-6"></a>
## 6. Checking What's Actually Happening

### 6.1 Right now — what's running, what's queued

**UI**: Jobs → filter by Status (`Running`, `Pending`, `Waiting`).

**CLI** (Controller API):

```bash
# Currently running
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://aap.example.com/api/v2/jobs/?status=running&page_size=100" | \
  jq '.results[] | {id, name, status, instance_group: .summary_fields.instance_group.name}'

# Pending in your container group
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://aap.example.com/api/v2/jobs/?status=pending&page_size=100" | \
  jq '.results[] | {id, name, status, instance_group: .summary_fields.instance_group.name}'
```

### 6.2 Capacity view per group

**Inside the controller-task pod**:

```bash
# OpenShift 2.5
oc -n aap rsh deploy/aap-controller-task awx-manage list_instance_groups

# OpenShift 2.4
oc -n aap rsh deploy/aap-task awx-manage list_instance_groups

# VM
sudo awx-manage list_instance_groups
```

Output columns to watch:

| Column | Meaning |
|---|---|
| `instances` | Number of execution instances in the group |
| `capacity` | Total fork capacity (effective `max_forks`) |
| `consumed_capacity` | Forks currently in use |
| `jobs_running` | Jobs currently running in this group |
| `jobs_total` | Cumulative |

If `consumed_capacity == capacity` and you have queued jobs → Gate 2 is the bottleneck. Raise `max_forks`.

### 6.3 Live pod count on OpenShift

```bash
# Right now
oc -n aap get pods -l ansible-awx-job-id --no-headers | wc -l

# With status
oc -n aap get pods -l ansible-awx-job-id \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName

# Pending pods (Gate 4 stuck)
oc -n aap get pods -l ansible-awx-job-id \
  --field-selector=status.phase=Pending
```

### 6.4 Why a pod is `Pending`

```bash
oc -n aap describe pod <pending-pod> | grep -A5 -E 'Events|Conditions'
```

Common reasons:

| Reason in events | Gate | Fix |
|---|---|---|
| `FailedScheduling` + "Insufficient cpu/memory" | Gate 4 | Add nodes or shrink `pod_spec_override.requests` |
| `FailedScheduling` + "node(s) didn't match Pod's node affinity/selector" | Gate 4 | Label nodes per `nodeSelector` |
| `forbidden: exceeded quota` | Gate 4 | Raise `ResourceQuota` |
| `ImagePullBackOff` | Gate 4 | Pre-pull EE; verify pull secrets |

### 6.5 Slice-aware view

A workflow created by a sliced template appears as a Workflow Job containing N child jobs:

```bash
# UI: open the Workflow Job; see all N children with their statuses

# API: list children of workflow ID 12345
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://aap.example.com/api/v2/workflow_jobs/12345/workflow_nodes/" | \
  jq '.results[] | {
        id: .summary_fields.job.id,
        status: .summary_fields.job.status,
        slice_number: .summary_fields.job.job_slice_number,
        slice_count: .summary_fields.job.job_slice_count
      }'
```

### 6.6 Controller capacity during the run

Prometheus metrics from `/api/v2/metrics/`:

| Metric | Meaning |
|---|---|
| `awx_instance_capacity` | Total capacity of the controller |
| `awx_instance_consumed_capacity` | Forks in use |
| `awx_running_jobs_total` | Currently running |
| `awx_pending_jobs_total` | Queued |
| `awx_status_total{status="pending"}` | Same, by status |

If `pending > 0` and `consumed_capacity == capacity`, Gate 2 or Gate 3 is the bottleneck.

---

<a id="sec-7"></a>
## 7. Common Symptoms and What They Mean

| Symptom | Most likely gate | Quick check |
|---|---|---|
| 20 slices created, only 3 pods running | Gate 2 — `max_forks` too low | `awx-manage list_instance_groups` |
| 20 slices created, 0 pods running | Gate 1 — "Allow Simultaneous" unchecked, or wrong group pinned | Template settings |
| Pods exist but stuck `Pending` | Gate 4 — quota / node / image pull | `oc describe pod <pending>` |
| All slices dispatch but managed hosts refuse SSH randomly | Gate 5 — `MaxStartups` | sshd config on managed hosts |
| Slices run sequentially despite Slicing=N | "Allow Simultaneous" not enabled | Template setting |
| `consumed_capacity > capacity` shown in `list_instance_groups` | Capacity over-subscribed; harmless transient or stale dispatcher state | `awx-manage run_dispatcher --reload` |
| First batch fast, later batches slow | Image pull cache cold on new nodes | Pre-pull DaemonSet |
| Concurrent jobs limit hit but `max_concurrent_jobs=0` | Gate 3 — global `MAX_FORKS` cap | Settings → Jobs |

---

<a id="sec-8"></a>
## 8. Decision Tree — "Why Aren't My Slices Running?"

```
1. Are N child jobs visible in the workflow?
   ├── No  → Template not actually sliced. Check job_slice_count.
   └── Yes → continue

2. Is "Allow Simultaneous" checked on the template?
   ├── No  → Children serialize. Enable it.
   └── Yes → continue

3. Is the template pinned to the right Instance Group?
   ├── No  → Pin to your sized group.
   └── Yes → continue

4. From `awx-manage list_instance_groups`,
   does (capacity − consumed_capacity) ≥ (forks + 1)?
   ├── No  → Gate 2. Raise max_forks (or wait).
   └── Yes → continue

5. Is max_concurrent_jobs reached?
   ├── Yes → Gate 2. Raise max_concurrent_jobs (or wait).
   └── No  → continue

6. Are job pods visible in the namespace?
   ├── No  → Gate 3. Check MAX_FORKS, dispatcher status.
   └── Yes → continue

7. Are pods Running, or Pending?
   ├── Pending → Gate 4. `oc describe pod` to find which constraint.
   └── Running → Slices ARE running in parallel; you're fine.

8. If many pods are Running but managed hosts are flaking →
   Gate 5. Check SSH MaxStartups, SNAT, DNS.
```

---

<a id="sec-9"></a>
## 9. Recipes for Typical Scenarios

### 9.1 1000-host fleet, run as fast as possible, OpenShift

```
Template:        Forks=75, Slicing=4, Allow Simultaneous=on, IG=large-fleet
Container group: max_concurrent_jobs=4, max_forks=320 (4 × 76 + 16)
Pod spec:        requests 1 CPU / 4 Gi, limits 4 CPU / 8 Gi
Quota:           ≥ 4 CPU and 16 Gi requests, with headroom for control plane
```

→ 4 pods, each handling ~250 hosts at 75-way parallelism.

### 9.2 5000-host fleet, capacity-constrained cluster, OpenShift

```
Template:        Forks=50, Slicing=10, Allow Simultaneous=on, IG=large-fleet
Container group: max_concurrent_jobs=5, max_forks=255 (5 × 51)
Pod spec:        requests 1 CPU / 4 Gi, limits 4 CPU / 8 Gi
```

→ 5 pods running, 5 queued; total wallclock ~2× a single wave. Trade speed for cluster impact.

### 9.3 1000-host fleet, single VM controller node

```
Template:        Forks=50, Slicing=1, Allow Simultaneous=off
Instance group:  default, max_forks=100
Controller VM:   8 vCPU / 32 GiB, dedicated execution node
```

→ One job, one EE container running 50-way parallel. Slicing doesn't add value when there's only one execution target.

### 9.4 Same template runs hourly, never want to overlap

```
Template:        Allow Simultaneous = unchecked
Schedule:        every hour
```

→ If a run overruns its hour, the next is skipped (or queued if Allow Simultaneous is on but capacity is full). Picking the right behaviour is intentional, not a side effect.

---

<a id="sec-10"></a>
## 10. Reference — Every Setting in One Table

| Setting | Where | Default | Effect on concurrency |
|---|---|---|---|
| `forks` | Template | 0 (uses `DEFAULT_FORKS`) | Hosts in parallel inside one slice |
| `job_slice_count` | Template | 1 | Number of sibling jobs created |
| `allow_simultaneous` | Template | false | Required for slices to run in parallel |
| `instance_groups` | Template | inherits | Which group dispatches the job |
| `timeout` | Template | 0 (none) | Hard kill after N seconds |
| `max_concurrent_jobs` | Instance/Container Group | varies | Hard ceiling on parallel jobs |
| `max_forks` | Instance/Container Group | varies | Total fork capacity in the group |
| `pod_spec_override.resources.requests` | Container Group | conservative | Multiplied by `max_concurrent_jobs` for OCP quota planning |
| `nodeSelector` / `tolerations` | Container Group pod spec | none | Where pods can land |
| `DEFAULT_FORKS` | Settings → Jobs | 5 | Default for templates that don't set forks |
| `MAX_FORKS` | Settings → Jobs | 200 | Global per-job hard cap |
| `SYSTEM_TASK_FORKS_CPU` | Settings → Jobs | 4 | Auto-capacity formula input |
| `SYSTEM_TASK_FORKS_MEM` | Settings → Jobs | 100 | Auto-capacity formula input |
| `ResourceQuota` | OpenShift namespace | none | Total CPU/RAM/pod cap for AAP namespace |
| `LimitRange` | OpenShift namespace | none | Per-pod min/max/default — silently overrides pod spec |
| Node labels & capacity | OpenShift cluster | n/a | Must match `nodeSelector` and have CPU/RAM headroom |
| `MaxStartups` | Managed host `sshd_config` | `10:30:100` | Inbound SSH parallelism cap on each managed host |
| SNAT egress IPs | Network | n/a | Connection-tracking limits on outbound SSH |

---

## TL;DR

- **Slicing creates jobs; capacity decides how many run in parallel.**
- The two settings that almost always need raising on OpenShift: container group **`max_forks`** and **`max_concurrent_jobs`**.
- Required forks for N parallel slices: `N × (template_forks + 1)` plus headroom.
- Verify with `awx-manage list_instance_groups` and `oc get pods -l ansible-awx-job-id`.
- If capacity looks right but pods are `Pending`, the bottleneck is OpenShift (Gate 4), not AAP.

