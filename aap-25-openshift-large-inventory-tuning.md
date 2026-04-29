---
title: Tuning AAP 2.5 on OpenShift for Large Inventories
type: guide
product: Red Hat Ansible Automation Platform
version: AAP 2.5
deployment: OpenShift Operator (AnsibleAutomationPlatform CR)
scope: 500 — 5000 managed hosts per job
audience: senior platform / automation engineers
tags: [aap, aap-2.5, openshift, performance, tuning, scale, large-inventory]
status: living-document
---

# Tuning AAP 2.5 on OpenShift for Large Inventories

> [!summary]
> A focused, opinionated guide for running Ansible jobs against **500 to 5000 managed hosts** from an AAP 2.5 deployment on OpenShift, using the unified `AnsibleAutomationPlatform` CR. Covers everything from cluster sizing and CR shape, through PostgreSQL, Gateway, Redis, Receptor, container groups, and EE design, to a reproducible benchmark plan and observability hooks.
>
> If you have not yet hit the freeze symptom described in the companion troubleshooting runbook, you should still read this end-to-end before scaling past ~300 hosts per job — most of these settings cannot be bolted on after the fact without an outage.

> [!info] What this guide assumes
> - AAP 2.5 deployed via the **Ansible Automation Platform Operator** on OpenShift 4.14 or later.
> - A single `AnsibleAutomationPlatform` CR (`aap.ansible.com/v1alpha1`).
> - Managed hosts are **Linux, reachable over SSH** from worker nodes hosting the EE container group.
> - Workload pattern is **batch automation** — a few large jobs per day, not thousands of small jobs per minute. Different patterns require different tuning.

---

<a id="sec-toc"></a>
## Contents

1. [Architecture quick reference](#sec-1)
2. [Pre-flight: cluster requirements](#sec-2)
3. [Sizing matrix by inventory size](#sec-3)
4. [Operator CR — the full tuned manifest](#sec-4)
5. [Container groups and `pod_spec_override`](#sec-5)
6. [Execution Environment image management](#sec-6)
7. [PostgreSQL tuning on OpenShift](#sec-7)
8. [Platform Gateway and Redis](#sec-8)
9. [Receptor and networking](#sec-9)
10. [Job template, playbook, and `ansible.cfg`](#sec-10)
11. [Observability and metrics](#sec-11)
12. [Benchmark methodology](#sec-12)
13. [Common failure modes](#sec-13)
14. [Validation checklist](#sec-14)
15. [References](#sec-15)

---

<a id="sec-1"></a>
## 1. Architecture Quick Reference

AAP 2.5 on OpenShift is a four-tier system. Tuning anywhere is bounded by the slowest tier.

```
┌─────────────────────────────────────────────────────────────────┐
│ Tier 1 — Front door                                             │
│   <name>-gateway       (Platform UI, SSO, websocket fan-out)    │
│   <name>-redis         (gateway cache + pub-sub)                │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│ Tier 2 — Control plane                                          │
│   <name>-controller-web    (API, UI assets, websocket origin)   │
│   <name>-controller-task   (dispatcher, callback receiver,      │
│                             Receptor controller)                │
└──────────────────────────────┬──────────────────────────────────┘
                               │  Receptor stream
┌──────────────────────────────▼──────────────────────────────────┐
│ Tier 3 — Execution                                              │
│   automation-job-<id>-<hash>    (one Pod per job, EE container) │
│      ansible-runner → ansible-playbook → SSH to managed hosts   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│ Tier 4 — State                                                  │
│   <name>-postgres-15    (PG 15: jobs, events, inventory state)  │
└─────────────────────────────────────────────────────────────────┘
```

> [!important]
> For large inventories the binding constraint is almost always **Tier 3 → Tier 2 → Tier 4**: the EE pod produces events faster than the controller-task callback receiver can write them to PostgreSQL. Every setting in this guide either reduces the event rate or expands the pipeline that drains it.

---

<a id="sec-2"></a>
## 2. Pre-flight: Cluster Requirements

Before applying any tuning, validate the cluster itself can support the workload.

### 2.1 Node sizing for AAP workers

Dedicate a **MachineSet** (or labeled subset of workers) to AAP execution. Job pods are spiky in CPU and memory; mixing with latency-sensitive workloads is asking for trouble.

- [ ] Worker nodes for AAP execution: **8 vCPU / 32 GiB minimum, 16 vCPU / 64 GiB recommended** for ≥1000-host runs.
- [ ] Label them: `oc label machineset <name> aap-workload=execution`
- [ ] Optionally taint: `oc adm taint nodes -l aap-workload=execution aap-workload=execution:NoSchedule`
- [ ] Match the taint with tolerations in the container group `pod_spec_override` (see [§5](#sec-5)).

### 2.2 Storage classes

PostgreSQL is the most common non-obvious bottleneck.

- [ ] **PG PVC**: a StorageClass with **provisioned IOPS**, not a "general purpose" tier.
  - AWS: `io2` or `gp3` with explicitly raised IOPS (≥3000 baseline, 6000+ for ≥2500 hosts).
  - Azure: `Premium SSD v2` or `UltraSSD`.
  - On-prem: NVMe-backed CSI with confirmed sustained write IOPS.
- [ ] **EE artifacts** (if persisted): standard block storage is fine; emptyDir is acceptable.
- [ ] **WAL on a separate PVC** if the CSI supports multi-volume PG (`postgres_extra_volumes` in the CR).

### 2.3 Registry

EE pods start fast only if the EE image is local to the node. At 1000+ hosts the **first job after a quiet period** is the worst case for image pull latency.

- [ ] Mirror the EE image to a registry inside or close to the cluster:
  - OpenShift internal registry, or
  - Mirror registry for Red Hat OpenShift, or
  - A pull-through cache.
- [ ] Set `imagePullPolicy: IfNotPresent` (default) and pre-pull the EE image to AAP worker nodes via a tiny DaemonSet — see [§6](#sec-6).

### 2.4 Networking

- [ ] **No idle timeout under 1 hour** on any L4/L7 between the controller-task pod and the job pods. Receptor streams events for the entire job duration.
- [ ] If using a service mesh: **exclude the AAP namespace** from sidecar injection unless explicitly tested. Sidecars add latency to every event hop.
- [ ] **Egress** from job pods to managed hosts: confirm SNAT egress IP allowlist has been sized for `forks × concurrent_jobs` simultaneous TCP/22 connections.
- [ ] **Route timeout** for the gateway: set `haproxy.router.openshift.io/timeout: 1h` annotation. Default is 30s on some clusters and breaks long-running websocket streams.

### 2.5 Quotas and limits

- [ ] `oc -n <aap-ns> describe quota` — confirm the namespace can hold the largest job pod plus running control plane.
- [ ] `oc -n <aap-ns> describe limitrange` — the LimitRange must allow CPU/memory the pod_spec_override declares; LimitRange overrides Pod-level values silently.
- [ ] `oc -n <aap-ns> get networkpolicy` — confirm policies allow `controller-task ↔ job pods` and `controller-* ↔ postgres / redis`.

---

<a id="sec-3"></a>
## 3. Sizing Matrix by Inventory Size

Use this as a **starting point**. Always validate with the benchmark in [§12](#sec-12) before declaring sizing complete.

| Component | 500 hosts | 1000 hosts | 2500 hosts | 5000 hosts |
|---|---|---|---|---|
| `controller-task` replicas | 2 | 2 | 3 | 4 |
| `controller-task` cpu req / lim | 1 / 4 | 1 / 4 | 2 / 6 | 2 / 8 |
| `controller-task` mem req / lim | 4Gi / 8Gi | 4Gi / 8Gi | 6Gi / 12Gi | 8Gi / 16Gi |
| `controller-web` replicas | 2 | 2 | 3 | 3 |
| `gateway` replicas | 2 | 2 | 3 | 3 |
| `gateway` cpu req / lim | 500m / 2 | 500m / 2 | 1 / 4 | 2 / 4 |
| `gateway` mem req / lim | 1Gi / 2Gi | 1Gi / 2Gi | 2Gi / 4Gi | 2Gi / 4Gi |
| `redis` mode | standalone | standalone | cluster | cluster |
| `redis` cpu req / lim | 250m / 1 | 250m / 1 | 500m / 2 | 1 / 2 |
| `redis` mem req / lim | 512Mi / 1Gi | 1Gi / 2Gi | 2Gi / 4Gi | 4Gi / 8Gi |
| Postgres cpu req / lim | 1 / 2 | 2 / 4 | 4 / 8 | 4 / 8 |
| Postgres mem req / lim | 4Gi / 8Gi | 4Gi / 8Gi | 8Gi / 16Gi | 16Gi / 32Gi |
| Postgres PVC IOPS | 3000 | 3000 | 6000 | 10000 |
| **Job pod cpu req / lim** | 1 / 2 | 1 / 4 | 2 / 6 | 2 / 8 |
| **Job pod mem req / lim** | 2Gi / 4Gi | 4Gi / 8Gi | 6Gi / 12Gi | 8Gi / 16Gi |
| Job template **Forks** | 50 | 75 | 100 | 100–150 |
| Job template **Job Slicing** | 1 | 4 | 8 | 16 |

> [!note]
> Forks × Slices ≈ effective parallelism. At 5000 hosts with Forks=100 and Slices=16, you have up to 1600 concurrent SSH sessions across slice pods. Confirm SNAT/egress and managed-host SSHD `MaxStartups` can absorb it.

---

<a id="sec-4"></a>
## 4. Operator CR — Full Tuned Manifest

The unified `AnsibleAutomationPlatform` CR is the source of truth for everything except UI-managed runtime settings (capacity, callback, fact cache, etc.). Avoid drift by keeping this in Git and reconciling via GitOps.

```yaml
apiVersion: aap.ansible.com/v1alpha1
kind: AnsibleAutomationPlatform
metadata:
  name: aap
  namespace: aap
spec:
  # ---------------------------------------------------------------
  # Image registry
  # ---------------------------------------------------------------
  image_pull_policy: IfNotPresent
  image_pull_secrets:
    - redhat-registry

  # ---------------------------------------------------------------
  # Platform Gateway (Tier 1)
  # ---------------------------------------------------------------
  gateway:
    disabled: false
    replicas: 2
    resource_requirements:
      requests: { cpu: "500m", memory: "1Gi" }
      limits:   { cpu: "2",    memory: "2Gi" }
    extra_settings:
      - setting: GATEWAY_PROXY_URL_IGNORE_CERT
        value: 'False'
    # Spread replicas across worker nodes
    affinity:
      podAntiAffinity:
        preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels:
                  app.kubernetes.io/component: gateway

  # ---------------------------------------------------------------
  # Redis (Tier 1 — gateway cache / pub-sub)
  # ---------------------------------------------------------------
  redis:
    mode: standalone        # 'cluster' once inventory > 2000 hosts
    resource_requirements:
      requests: { cpu: "250m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "2Gi" }

  # ---------------------------------------------------------------
  # Controller (Tier 2)
  # ---------------------------------------------------------------
  controller:
    disabled: false
    replicas: 2
    task_replicas: 2
    web_replicas: 2

    task_resource_requirements:
      requests: { cpu: "1",    memory: "4Gi" }
      limits:   { cpu: "4",    memory: "8Gi" }

    web_resource_requirements:
      requests: { cpu: "500m", memory: "2Gi" }
      limits:   { cpu: "2",    memory: "4Gi" }

    # Default control-plane EE (used for project syncs, inventory updates)
    ee_resource_requirements:
      requests: { cpu: "500m", memory: "2Gi" }
      limits:   { cpu: "2",    memory: "4Gi" }

    # Spread controller pods across nodes
    task_affinity:
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          - topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels:
                app.kubernetes.io/component: controller-task

    # Schedule controller on a node pool separate from job pods
    task_node_selector:
      aap-workload: control
    web_node_selector:
      aap-workload: control

  # ---------------------------------------------------------------
  # Hub / EDA — disable if not in scope, they consume resources even idle
  # ---------------------------------------------------------------
  hub:
    disabled: true
  eda:
    disabled: true

  # ---------------------------------------------------------------
  # PostgreSQL (Tier 4) — managed by the Operator
  # ---------------------------------------------------------------
  database:
    resource_requirements:
      requests: { cpu: "2",    memory: "4Gi" }
      limits:   { cpu: "4",    memory: "8Gi" }
    postgres_storage_requirements:
      requests:
        storage: 200Gi
    postgres_storage_class: io2-csi      # or your high-IOPS class
    postgres_extra_args:
      - '-c'
      - 'max_connections=1024'
      - '-c'
      - 'shared_buffers=2GB'
      - '-c'
      - 'work_mem=32MB'
      - '-c'
      - 'maintenance_work_mem=512MB'
      - '-c'
      - 'effective_cache_size=6GB'
      - '-c'
      - 'checkpoint_timeout=15min'
      - '-c'
      - 'checkpoint_completion_target=0.9'
      - '-c'
      - 'wal_compression=on'
      - '-c'
      - 'random_page_cost=1.1'
      - '-c'
      - 'autovacuum_vacuum_scale_factor=0.05'
      - '-c'
      - 'autovacuum_analyze_scale_factor=0.02'
```

> [!warning] CR patching at scale
> Editing this CR triggers reconciliation of **all** sub-components. For runtime knobs (forks, verbosity, callback, fact cache, instance group definitions) prefer the **Controller UI / API**. Reserve CR edits for resource sizing, replica counts, image references, and `extra_settings`.

---

<a id="sec-5"></a>
## 5. Container Groups and `pod_spec_override`

The default container group runs job pods with conservative limits. **This is where 90% of "freeze" symptoms originate** when you scale beyond a few hundred hosts.

### 5.1 Create a dedicated container group for large jobs

In Controller UI → **Instance Groups → Add → Container Group**, name it `large-fleet`, then click **Customize pod spec** and paste:

```yaml
apiVersion: v1
kind: Pod
metadata:
  namespace: aap
  labels:
    aap.example.com/workload: large-fleet
spec:
  serviceAccountName: default
  automountServiceAccountToken: false

  # Schedule on AAP execution nodes only
  nodeSelector:
    aap-workload: execution

  tolerations:
    - key: aap-workload
      operator: Equal
      value: execution
      effect: NoSchedule

  # Spread across nodes if multiple slices run concurrently
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels:
          aap.example.com/workload: large-fleet

  containers:
    - name: worker
      image: 'image-registry.openshift-image-registry.svc:5000/aap/custom-ee-rhel9:1.4.2'
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
        - name: ANSIBLE_STDOUT_CALLBACK
          value: "default"
        - name: ANSIBLE_CALLBACKS_ENABLED
          value: "profile_tasks,timer"
```

### 5.2 Pin large-fleet templates to it

In the Job Template, set **Instance Groups: large-fleet**. Default templates continue to use the default group; the large-fleet group is reserved for capacity-hungry jobs.

### 5.3 Why each setting matters

- `nodeSelector` + `tolerations` — keep job pods off the control plane and away from latency-sensitive workloads.
- `topologySpreadConstraints` — when a slice job creates 4 / 8 / 16 sibling pods, spread them so a single node failure doesn't kill the run.
- `imagePullPolicy: IfNotPresent` — combined with the pre-pull DaemonSet from [§6](#sec-6), keeps job start time below 5 seconds.
- `automountServiceAccountToken: false` — the EE doesn't talk to the K8s API; mounting the token is unnecessary attack surface.
- `requests` ≪ `limits` — bin-packing efficiency without throttling under load. Tune `requests` to your steady-state EE memory; tune `limits` to peak (fact gathering at job start).

---

<a id="sec-6"></a>
## 6. Execution Environment Image Management

### 6.1 Build a custom EE

Don't use the stock `ee-supported-rhel9` for production scale. Bake your own with `ansible-builder` and include:

- The **`ansible.cfg`** from [§10.2](#sec-10) at `/etc/ansible/ansible.cfg`.
- Your collections, pinned by version (`ansible.posix`, `community.general`, `redhatinsights.insights`, etc.).
- Your private CA bundle under `/etc/pki/ca-trust/source/anchors/` followed by `update-ca-trust`.
- Any system packages your modules need (e.g., `sshpass`, `python3-jmespath`, `python3-netaddr`).

Push to the cluster's internal registry:

```bash
podman tag custom-ee:1.4.2 \
  default-route-openshift-image-registry.<cluster>/aap/custom-ee-rhel9:1.4.2
podman push default-route-openshift-image-registry.<cluster>/aap/custom-ee-rhel9:1.4.2
```

### 6.2 Pre-pull the EE to AAP worker nodes

A tiny DaemonSet ensures the image is on every execution node before the first job tries to pull it.

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ee-prepull
  namespace: aap
spec:
  selector:
    matchLabels: { app: ee-prepull }
  template:
    metadata:
      labels: { app: ee-prepull }
    spec:
      nodeSelector:
        aap-workload: execution
      tolerations:
        - key: aap-workload
          operator: Equal
          value: execution
          effect: NoSchedule
      containers:
        - name: prepull
          image: image-registry.openshift-image-registry.svc:5000/aap/custom-ee-rhel9:1.4.2
          imagePullPolicy: IfNotPresent
          command: ['/bin/sh', '-c', 'sleep infinity']
          resources:
            requests: { cpu: 10m, memory: 32Mi }
            limits:   { cpu: 50m, memory: 64Mi }
```

Update the `image:` tag in this DaemonSet whenever the EE is rebuilt; the rolling update pre-pulls the new image cluster-wide before the next job needs it.

---

<a id="sec-7"></a>
## 7. PostgreSQL Tuning on OpenShift

### 7.1 Storage is non-negotiable

Slow PG storage produces the canonical "job is Running but no events" symptom. Validate sustained write throughput before tuning anything else:

```bash
oc -n aap rsh deploy/aap-postgres-15 \
  bash -c "pg_test_fsync -f /var/lib/pgsql/data/userdata/fsync.test"
```

Targets: `fdatasync` ≥ 5000 ops/sec on a healthy NVMe-backed PVC. Anything under 1000 will struggle past 500 hosts.

### 7.2 Postgres parameters worth setting

Already embedded in the CR's `postgres_extra_args` ([§4](#sec-4)). For 2500+ hosts, additionally:

```yaml
postgres_extra_args:
  - '-c'
  - 'max_wal_size=4GB'
  - '-c'
  - 'min_wal_size=1GB'
  - '-c'
  - 'wal_buffers=16MB'
  - '-c'
  - 'synchronous_commit=off'   # only if RPO allows; speeds up event ingest dramatically
```

> [!warning]
> `synchronous_commit=off` trades a small window of un-flushed transactions for a large gain in event ingest throughput. Acceptable for AAP because job event loss in the last ~200ms is recoverable from the artifacts directory. **Do not** use for the controller's encryption key tables.

### 7.3 Event retention

`main_jobevent` is the table that grows under big runs. Without aggressive cleanup it dominates the database within weeks.

- [ ] Schedule **Management Job: Cleanup Job Details** in the Controller UI — run nightly with `Days of data to keep: 30`.
- [ ] Schedule **Management Job: Cleanup Activity Stream** — same cadence.
- [ ] Monitor `pg_relation_size('main_jobevent')` over time; alert at >100 GiB.

---

<a id="sec-8"></a>
## 8. Platform Gateway and Redis

New to 2.5 and easy to under-size. The gateway terminates the **websocket connection** that streams job events to every open browser tab. Redis is the pub/sub backbone behind it.

### 8.1 Gateway sizing rule of thumb

- Steady state: 256 MiB RAM + 100m CPU per gateway replica.
- Peak: per concurrent open Platform UI session watching a running job: ~10 MiB + 50m CPU. Size for 90th-percentile concurrent sessions, not average.
- Always run **at least 2 replicas** with anti-affinity. A single-replica gateway is a single point of failure for the entire UI.

### 8.2 Route timeout

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: aap-gateway
  namespace: aap
  annotations:
    haproxy.router.openshift.io/timeout: 1h
    haproxy.router.openshift.io/balance: roundrobin
    haproxy.router.openshift.io/disable_cookies: 'true'
spec:
  to:
    kind: Service
    name: aap-gateway-service
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
```

Default 30s idle timeout drops websockets mid-job. Symptom: jobs complete normally but the UI shows "lost connection" partway through.

### 8.3 Redis sizing and mode

```yaml
spec:
  redis:
    mode: standalone     # standalone is fine up to ~2000 hosts / few concurrent jobs
    resource_requirements:
      requests: { cpu: "250m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "2Gi" }
```

Switch to `mode: cluster` when:

- Inventory regularly exceeds 2000 hosts in a single job, **or**
- More than 3 large jobs run concurrently, **or**
- The `INFO memory used_memory_peak` value approaches the limit.

`cluster` mode roughly doubles the resource footprint and adds three sentinel pods, but eliminates Redis as a single point of failure for the Platform UI.

### 8.4 Watching it under load

```bash
oc -n aap rsh deploy/aap-redis redis-cli INFO clients
oc -n aap rsh deploy/aap-redis redis-cli INFO memory | grep -E 'used_memory_human|used_memory_peak_human|maxmemory_human'
oc -n aap rsh deploy/aap-redis redis-cli INFO stats | grep -E 'instantaneous_ops_per_sec|rejected_connections|evicted_keys'
```

> [!tip]
> `evicted_keys > 0` under load means Redis is at memory limit and dropping pub-sub messages. Symptom in the UI: missing or out-of-order events. Raise `redis.resource_requirements.limits.memory` and reconcile.

---

<a id="sec-9"></a>
## 9. Receptor and Networking

Receptor lives inside the `controller-task` pod and streams stdout from job pods back to the callback receiver. At 1000+ hosts the stream is a near-constant data flow for the duration of the run.

### 9.1 NetworkPolicy

The Operator manages baseline NetworkPolicies. If you've layered your own:

- [ ] Allow `controller-task` → all pods labeled `ansible-awx-job-id` on the dynamically-allocated Receptor port (default `27199/tcp`, but auto-negotiated for the in-cluster mesh).
- [ ] Allow `controller-*` → `postgres-15` (5432) and `redis` (6379).
- [ ] Allow gateway → controller-web (8080) and the Platform UI websocket origin.

### 9.2 Service mesh

If Istio / OpenShift Service Mesh is enabled cluster-wide, **exclude the AAP namespace**:

```yaml
apiVersion: maistra.io/v1
kind: ServiceMeshMemberRoll
metadata:
  name: default
  namespace: istio-system
spec:
  members:
    # - aap        # explicitly excluded
    - app1
    - app2
```

The sidecar adds latency to every Receptor frame; under a 1000-host fact gather that compounds into multi-minute wallclock cost.

### 9.3 Egress to managed hosts

- [ ] Confirm SNAT egress IP allowlist on the perimeter firewall has been sized for `forks × concurrent_jobs` simultaneous TCP/22 connections plus overhead.
- [ ] If using **EgressIP**, pin the AAP namespace to a dedicated egress IP so audit / firewall rules are deterministic.
- [ ] Managed hosts: validate `MaxStartups` in `sshd_config` — default `10:30:100` will reject inbound at high parallelism. Set to `100:30:200` for hosts in scope of large jobs.

---

<a id="sec-10"></a>
## 10. Job Template, Playbook, and `ansible.cfg`

### 10.1 Job template settings

In the Controller UI for any template targeting >300 hosts:

- [ ] **Forks**: per the matrix in [§3](#sec-3).
- [ ] **Job Slicing**: per the matrix; this creates parallel sibling jobs across the inventory.
- [ ] **Verbosity**: 0 or 1. **Never 3+ at scale** — the event volume will saturate PG.
- [ ] **Limit**: empty. Use slicing, not manual `--limit`.
- [ ] **Timeout**: explicit. For 1000-host fact gather, 1800s. For 1000-host configuration run, sized to the slowest expected host plus 50%.
- [ ] **Concurrent jobs**: enabled only after capacity testing.
- [ ] **Instance Groups**: pin to `large-fleet` ([§5](#sec-5)).
- [ ] **Extra Variables**: nothing sensitive — those go in vault or credential types.

### 10.2 `ansible.cfg` baked into the EE

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
display_skipped_hosts = False
display_ok_hosts = True

# Fact caching — shared in-memory via Redis is best at scale
fact_caching = redis
fact_caching_connection = aap-redis-svc:6379:1
fact_caching_timeout = 7200

[ssh_connection]
pipelining = True
ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o ServerAliveInterval=15 -o PreferredAuthentications=publickey
control_path_dir = /tmp/ansible-cp
control_path = %(directory)s/%%h-%%r
retries = 3

[persistent_connection]
command_timeout = 60
connect_timeout = 30
```

> [!note] Fact caching against the platform Redis
> Using the AAP-managed Redis as a fact cache is supported but loads the same Redis instance that backs the gateway. For >2500-host environments either run a **dedicated Redis** for fact caching, or use the `jsonfile` backend pointed at a PVC mounted into the EE.

### 10.3 Playbook hygiene

```yaml
- hosts: all
  gather_facts: smart
  pre_tasks:
    - name: Targeted fact gather
      ansible.builtin.setup:
        gather_subset:
          - '!all'
          - '!min'
          - network
          - virtual
        gather_timeout: 30
      tags: always

  tasks:
    - name: Apply baseline configuration
      ansible.builtin.include_role:
        name: rh.baseline
      loop_control:
        label: "{{ item.name | default('item') }}"
      no_log: "{{ baseline_no_log | default(true) }}"
```

`no_log` on item-heavy loops is the single biggest event-volume reducer. A `loop` over 200 items in verbose mode produces 200 events per host per task — at 1000 hosts that's 200,000 events for one task.

---

<a id="sec-11"></a>
## 11. Observability and Metrics

AAP exposes Prometheus metrics at `/api/v2/metrics/`. Wire them into OpenShift Monitoring or a parallel Prometheus and watch:

| Metric | Meaning | Alert threshold |
|---|---|---|
| `awx_pending_jobs_total` | Jobs queued, not yet running | >10 sustained |
| `awx_running_jobs_total` | Currently running | informational |
| `awx_instance_capacity` | Total fork capacity | informational |
| `awx_instance_consumed_capacity` | Forks in use | >90% of capacity for >5min |
| `awx_database_connections` | Active DB connections | >80% of `max_connections` |
| `awx_callback_receiver_events_queue_size` | **Callback backlog** | **>1000** |
| `awx_callback_receiver_events_insert_seconds` | PG insert latency | p95 >100ms |

Pod-level metrics from `kube-state-metrics` worth dashboarding next to the AAP set:

- `kube_pod_container_resource_limits` vs `container_memory_working_set_bytes` for `controller-task`, `postgres-15`, job pods.
- `kube_persistentvolumeclaim_status_phase` — pending PVCs delay everything.
- `kube_pod_container_status_terminated_reason` filtered to `OOMKilled` — the canary for under-sized job pods.

Suggested PromQL examples:

```promql
# Callback backlog growing
deriv(awx_callback_receiver_events_queue_size[5m]) > 0

# Job pod nearing memory limit
max by (pod) (
  container_memory_working_set_bytes{namespace="aap", pod=~"automation-job-.*"}
  /
  on(pod) group_left
  kube_pod_container_resource_limits{resource="memory", namespace="aap", pod=~"automation-job-.*"}
) > 0.85

# Postgres write latency
histogram_quantile(0.95, rate(awx_callback_receiver_events_insert_seconds_bucket[5m]))
```

---

<a id="sec-12"></a>
## 12. Benchmark Methodology

Before declaring tuning complete, run this matrix and record the outputs. A spreadsheet or Obsidian table is fine; the absolute numbers matter less than the trend.

### 12.1 Test playbook

Use a deliberately simple playbook so you measure AAP and not your own tasks:

```yaml
- hosts: all
  gather_facts: smart
  tasks:
    - name: Ping
      ansible.builtin.ping:

    - name: Tiny fact
      ansible.builtin.debug:
        msg: "{{ ansible_facts['default_ipv4']['address'] | default('n/a') }}"
```

### 12.2 Run grid

| Phase | Inventory | Forks | Slicing | Verbosity | Concurrency |
|---|---|---|---|---|---|
| 0 | 50    | 25  | 1  | 0 | 1 |
| 1 | 200   | 50  | 1  | 0 | 1 |
| 2 | 500   | 50  | 2  | 0 | 1 |
| 3 | 1000  | 75  | 4  | 0 | 1 |
| 4 | 2500  | 100 | 8  | 0 | 1 |
| 5 | 1000  | 75  | 4  | 0 | 3 (concurrent runs) |
| 6 | 1000  | 75  | 4  | 1 | 1 (verbosity stress) |

### 12.3 Captured per phase

- [ ] Total runtime (Controller UI → Job → Started/Finished).
- [ ] Peak job pod memory: `oc adm top pod` snapshots every 30s, or kube_pod metrics in Prometheus.
- [ ] Peak `controller-task` memory and CPU.
- [ ] Callback receiver backlog peak: `awx_callback_receiver_events_queue_size`.
- [ ] PG `main_jobevent` row delta and p95 insert latency.
- [ ] Any `OOMKilled`, `Evicted`, `BackOff`, or pending-pod events.
- [ ] Redis `used_memory_peak`, `evicted_keys`, `rejected_connections`.
- [ ] Gateway pod CPU peak and any 5xx in route logs.
- [ ] UI responsiveness during the run (subjective, 1–5).

### 12.4 Pass criteria

- No `OOMKilled` job pods at any phase.
- Callback backlog returns to 0 within 60s of playbook end.
- Phase 4 total runtime ≤ 4 × Phase 1 total runtime (acceptable super-linear scaling for fact gather).
- Phase 5 (concurrency) does not extend any single job runtime by >2× the Phase 3 baseline.

If any criterion fails, return to [§3](#sec-3), bump the relevant component one tier, and re-run.

---

<a id="sec-13"></a>
## 13. Common Failure Modes

| Symptom | Most likely cause | Fix |
|---|---|---|
| Job pod `OOMKilled` minutes in | `pod_spec_override` memory limit too low for fact volume | Raise to next tier in [§3](#sec-3) |
| Job "Running" forever after playbook printed PLAY RECAP | Callback receiver backlog into PG | Scale `task_replicas`, tune PG WAL ([§7](#sec-7)) |
| Job pod `Pending` forever | `ResourceQuota`, `LimitRange`, or no node fits | `oc describe pod` + `oc describe limitrange` |
| All jobs queue, never start | Capacity = 0 for the assigned instance group | Check `awx-manage list_instances`; node selector matching |
| Platform UI 502 during big run | Gateway CPU starved or replica count too low | [§8.1](#sec-8) |
| UI shows missing events | Redis evicting under memory pressure | Raise Redis limits or switch to `cluster` mode |
| One stuck host blocks the entire play | Default `linear` strategy | `strategy: free` in playbook ([§10.3](#sec-10)) |
| Sporadic SSH timeouts at scale | Managed-host `MaxStartups` too low or SNAT exhaustion | [§9.3](#sec-9) |
| EE pull takes 90s on first job after idle period | Image not on node | Pre-pull DaemonSet ([§6.2](#sec-6)) |
| Fact gather works once, fails on retries with stale data | Fact cache TTL too long combined with restarts | Lower `fact_caching_timeout`, validate Redis persistence |
| Postgres CPU pegged during runs | `synchronous_commit=on` + slow disk + high event rate | Either raise IOPS or `synchronous_commit=off` ([§7.2](#sec-7)) |

---

<a id="sec-14"></a>
## 14. Validation Checklist

Use before every production scaling event.

**Cluster**

- [ ] AAP execution nodes labeled and tainted, capacity ≥ next tier in [§3](#sec-3).
- [ ] PG StorageClass benchmarked at target IOPS.
- [ ] EE image mirrored and pre-pulled.
- [ ] No NetworkPolicy or service-mesh sidecar in the AAP namespace's data path.
- [ ] Egress allowlist sized for `forks × concurrent_jobs`.

**AAP CR**

- [ ] `controller`, `gateway`, `redis`, `database` all sized to current tier.
- [ ] `task_replicas ≥ 2`, `web_replicas ≥ 2`, `gateway.replicas ≥ 2`.
- [ ] `postgres_extra_args` includes WAL and autovacuum tuning.
- [ ] Hub / EDA disabled if not in scope.

**Container group**

- [ ] `large-fleet` group exists with full `pod_spec_override` from [§5](#sec-5).
- [ ] Custom EE image referenced by digest or specific tag, not `:latest`.
- [ ] Topology spread + node selector + tolerations applied.

**Job templates**

- [ ] Forks, Slicing, Verbosity per [§3](#sec-3) / [§10](#sec-10).
- [ ] Pinned to `large-fleet` instance group.
- [ ] Explicit timeout.

**Observability**

- [ ] AAP metrics scraped by Prometheus.
- [ ] Alerts wired for callback backlog, OOMKilled, PG insert latency.
- [ ] Dashboard for the matrix in [§11](#sec-11).

**Operational**

- [ ] Cleanup management jobs scheduled.
- [ ] Backup job for PG verified within last 7 days.
- [ ] Benchmark from [§12](#sec-12) re-run within last 90 days.

---

<a id="sec-15"></a>
## 15. References

- Red Hat — *AAP 2.5 Installing on OpenShift Container Platform*
- Red Hat — *AAP 2.5 Operator Performance and Scaling*
- Red Hat — *AAP 2.5 Containerized Installation Guide* (for VM topology comparison)
- Red Hat — *Automation Controller Administration Guide* — Capacity, Job Slicing, Container Groups
- Ansible Project — *Strategies* (`linear`, `free`, `host_pinned`)
- ansible-runner / ansible-builder upstream documentation
- Receptor project documentation
- OpenShift documentation — Pod scheduling, Topology Spread Constraints, NetworkPolicy
