---
title: AAP Performance Cheat Sheet
type: cheat-sheet
product: Red Hat Ansible Automation Platform
versions: [AAP 2.4, AAP 2.5]
audience: AAP-literate engineers — checklist for review
status: living-document
---

# AAP Performance Cheat Sheet

> Quick checklist of every knob that matters for large-fleet AAP performance. Each item: **what** it does, **why** it matters. Full example at the bottom.

---

## Job template

- **Forks** — concurrent hosts in flight. Default 5 is for demos. *Why:* throughput is bounded by `forks × gather_time`. Set to 50–150 for large fleets.
- **Job Slicing** — splits a template into N parallel sibling jobs. *Why:* horizontal scale across multiple EE pods/nodes; survives single-pod failure better than one giant job.
- **Verbosity** — 0 is quiet, 4 is firehose. *Why:* every level multiplies event volume into PostgreSQL. Verbosity ≥2 at 1000 hosts buries the callback receiver.
- **Timeout** — explicit per-template timeout. *Why:* a hung host without a timeout becomes a hung job slot forever.
- **Instance Group** — pin large jobs to a dedicated container group. *Why:* keeps fleet jobs off the default group and isolates resource impact.
- **Concurrent Jobs** — allow multiple runs of the same template. *Why:* enable only after capacity testing; otherwise queues silently grow.

---

## Playbook & `ansible.cfg` (baked into the EE)

- **`strategy: free`** (or `host_pinned`) — replaces default `linear`. *Why:* `linear` waits for the slowest host on every task; one stuck SSH halts everything.
- **`gather_facts: smart` + fact caching** — reuse facts across runs. *Why:* `setup` against 1000 hosts is the single biggest event volume source; cache it.
- **`gather_subset`** — narrow facts to what you actually use. *Why:* default `all` returns megabytes of facts per host; most playbooks need a fraction of it.
- **`pipelining = true`** — fewer SSH round-trips per task. *Why:* roughly 2× speedup on most tasks; requires `requiretty` off in sudoers (almost always already off on RHEL).
- **`ControlMaster=auto`, `ControlPersist=60s`** — reuse SSH connections. *Why:* avoids re-handshaking TCP+TLS+auth for every task.
- **`control_path_dir`** — short path under `/tmp`. *Why:* the Unix socket path has a 108-char limit; long paths cause silent failures at scale.
- **`forks` (cfg-level)** — matches template forks. *Why:* the EE-level default applies to ad-hoc and sub-plays inside roles.
- **`internal_poll_interval = 0.001`** — internal scheduler tick. *Why:* default is conservative; lowering improves throughput at high forks.
- **`no_log: true` on item-heavy loops** — suppresses per-item events. *Why:* a 200-item loop × 1000 hosts = 200k events for one task.

---

## Execution Environment (EE)

- **Custom EE built with `ansible-builder`** — bake in `ansible.cfg`, collections, CA bundle. *Why:* eliminates per-template extra-vars drift; deterministic runtime.
- **Pinned image tag/digest** — never `:latest`. *Why:* reproducibility and safe rollback.
- **Image pre-pulled to AAP worker nodes** (DaemonSet) — *Why:* eliminates 60–90s cold-start on first job after an idle period.
- **Image hosted in cluster-local registry / mirror** — *Why:* faster pull, avoids registry rate limits, works in air-gapped.

---

## Container Group / `pod_spec_override` (OpenShift)

- **CPU/memory requests + limits** — explicitly sized per inventory tier. *Why:* the default is far too small; OOMKill at 300+ hosts of fact gather is the canonical failure mode.
- **`nodeSelector` + `tolerations`** — pin job pods to dedicated AAP execution nodes. *Why:* isolation from latency-sensitive workloads; deterministic capacity.
- **`topologySpreadConstraints`** — spread slice siblings across nodes. *Why:* one node failure doesn't kill an entire sliced job.
- **`imagePullPolicy: IfNotPresent`** — *Why:* combined with pre-pull, fastest job start.
- **`automountServiceAccountToken: false`** — *Why:* the EE doesn't talk to the Kubernetes API; remove unnecessary attack surface.

---

## Controller (instance / system settings)

- **`task_replicas` ≥ 2** — scale callback receiver horizontally. *Why:* one task pod is a bottleneck and a SPOF for event ingestion.
- **`web_replicas` ≥ 2** — *Why:* HA for API and UI, especially during heavy event streaming.
- **`task_resource_requirements`** — CPU/RAM for callback receiver + dispatcher. *Why:* under-sizing causes the "Running forever after PLAY RECAP" symptom.
- **`SYSTEM_TASK_FORKS_CPU` / `SYSTEM_TASK_FORKS_MEM`** — capacity formula inputs. *Why:* drives the controller's view of how many forks a node can host; wrong values hide capacity or oversubscribe.
- **`MAX_FORKS`** — hard cap. *Why:* protects the controller from a single template requesting 10,000 forks.
- **`EVENT_STDOUT_MAX_BYTES_DISPLAY`** — per-event truncation. *Why:* prevents pathological multi-MB events from saturating PG.
- **`MAX_WEBSOCKET_EVENT_RATE_SECONDS`** — UI streaming throttle. *Why:* keeps the web pod responsive when 1000 hosts emit events simultaneously.
- **Cleanup management jobs** — `cleanup_jobs`, `cleanup_activitystream`. *Why:* without them `main_jobevent` grows unboundedly and PG eventually melts.

---

## PostgreSQL

- **High-IOPS storage class** (provisioned IOPS, NVMe, `io2` / `Premium SSD v2`). *Why:* `main_jobevent` write rate is the actual bottleneck for large jobs; default "general purpose" tiers can't keep up.
- **`shared_buffers` ~25 % of RAM** — *Why:* PG's primary cache; default is tiny.
- **`work_mem`** — per-operation sort/hash memory. *Why:* small default forces disk spills on `main_jobevent` queries.
- **`max_connections`** — *Why:* AAP holds many connections; default 100 is too low for ≥2 task replicas + workers.
- **`checkpoint_timeout` 15min, `checkpoint_completion_target` 0.9** — *Why:* spreads WAL flushing; avoids latency spikes during heavy event ingest.
- **`wal_compression = on`** — *Why:* less WAL I/O at the cost of trivial CPU.
- **`autovacuum` aggressive scale factors** (0.05 / 0.02) — *Why:* event tables churn fast; default thresholds let bloat accumulate.
- **`synchronous_commit = off`** — *Why:* large throughput gain in event ingest. Trade-off: ~200ms RPO on crash. Acceptable for AAP because events are recoverable from artifacts; never for the encryption-key tables.

---

## Platform Gateway & Redis (AAP 2.5 only)

- **`gateway.replicas` ≥ 2** — *Why:* the gateway is the websocket front-door for the Platform UI; single replica = single point of failure.
- **Route annotation `haproxy.router.openshift.io/timeout: 1h`** — *Why:* default 30s drops websocket streams mid-job; users see "lost connection" while the job runs fine.
- **Redis `mode: cluster`** at >2000 hosts — *Why:* eliminates Redis as a SPOF; standalone is fine below that scale.
- **Redis memory limit** — *Why:* eviction under load drops pub-sub messages → missing UI events. Watch `evicted_keys`.

---

## Receptor & networking

- **No service-mesh sidecar** in the AAP namespace. *Why:* Istio/Linkerd add latency to every Receptor frame; compounds painfully under high event rates.
- **NetworkPolicy explicitly allows controller-task ↔ job pods**. *Why:* the Receptor stream is a long-lived TCP connection; default-deny breaks it silently.
- **Egress IP allowlist sized for `forks × concurrent_jobs`** — *Why:* SNAT/firewall connection-tracking limits silently drop SSH connections at high parallelism.
- **Managed-host `MaxStartups 100:30:200`** — *Why:* default `10:30:100` rejects inbound SSH at high parallelism, looks like random "unreachable" failures.

---

## OS-level (VM topology)

- **`ulimit -n 65535`** for the AAP user(s). *Why:* fd exhaustion on the controller is the classic large-fleet failure on bare metal.
- **`net.ipv4.ip_local_port_range = 15000 65000`** — *Why:* avoids ephemeral port exhaustion at high SSH parallelism.
- **`fs.file-max = 2097152`** — *Why:* system-wide cap; raise with the per-user `nofile`.
- **`loginctl enable-linger`** (containerized 2.5 only) — *Why:* keeps `--user` systemd units running after admin logout; otherwise containers die.
- **Time sync (`chrony`)** across all nodes — *Why:* Receptor TLS and JWT validation are sensitive to clock skew.

---

## Observability (so you know it's working)

- **Prometheus scraping `/api/v2/metrics/`** — *Why:* `awx_callback_receiver_events_queue_size` is the single most important metric; backlog growth = the ingest pipeline can't keep up.
- **Alert on `OOMKilled`** for job pods — *Why:* the only signal that `pod_spec_override` is under-sized.
- **Alert on PG `main_jobevent` insert p95 latency >100ms** — *Why:* leading indicator of "Running forever" symptom.
- **Dashboard for `awx_instance_consumed_capacity` vs `awx_instance_capacity`** — *Why:* sustained >90% means you're queueing.

---

## Example — putting it all together

`ansible.cfg` baked into the EE:

```ini
[defaults]
strategy = free
forks = 100
gather_timeout = 30
timeout = 30
internal_poll_interval = 0.001
host_key_checking = False
display_skipped_hosts = False
fact_caching = redis
fact_caching_connection = aap-redis-svc:6379:1
fact_caching_timeout = 7200
callbacks_enabled = profile_tasks, timer

[ssh_connection]
pipelining = True
ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o ServerAliveInterval=15 -o PreferredAuthentications=publickey
control_path_dir = /tmp/ansible-cp
control_path = %(directory)s/%%h-%%r
retries = 3
```

Playbook header:

```yaml
- hosts: all
  gather_facts: smart
  pre_tasks:
    - ansible.builtin.setup:
        gather_subset: ['!all','!min','distribution','network']
        gather_timeout: 30
  tasks:
    - ansible.builtin.include_role:
        name: rh.baseline
      no_log: "{{ baseline_no_log | default(true) }}"
```

Job template:

| Setting | Value |
|---|---|
| Forks | 100 |
| Job Slicing | 4 |
| Verbosity | 0 |
| Timeout | 3600 |
| Instance Group | `large-fleet` |
| Concurrent Jobs | enabled (after testing) |

Container group `pod_spec_override` (OpenShift):

```yaml
apiVersion: v1
kind: Pod
spec:
  serviceAccountName: default
  automountServiceAccountToken: false
  nodeSelector: { aap-workload: execution }
  tolerations:
    - { key: aap-workload, operator: Equal, value: execution, effect: NoSchedule }
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: ScheduleAnyway
      labelSelector: { matchLabels: { aap.example.com/workload: large-fleet } }
  containers:
    - name: worker
      image: image-registry.openshift-image-registry.svc:5000/aap/custom-ee-rhel9:1.4.2
      imagePullPolicy: IfNotPresent
      args: ['ansible-runner', 'worker', '--private-data-dir=/runner']
      resources:
        requests: { cpu: "1", memory: "4Gi" }
        limits:   { cpu: "4", memory: "8Gi" }
```

Operator CR (AAP 2.5, abridged to performance-relevant fields):

```yaml
apiVersion: aap.ansible.com/v1alpha1
kind: AnsibleAutomationPlatform
metadata: { name: aap, namespace: aap }
spec:
  controller:
    task_replicas: 2
    web_replicas: 2
    task_resource_requirements:
      requests: { cpu: "1", memory: "4Gi" }
      limits:   { cpu: "4", memory: "8Gi" }
  gateway:
    replicas: 2
    resource_requirements:
      requests: { cpu: "500m", memory: "1Gi" }
      limits:   { cpu: "2",    memory: "2Gi" }
  redis:
    mode: standalone     # cluster at >2000 hosts
    resource_requirements:
      requests: { cpu: "250m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "2Gi" }
  database:
    resource_requirements:
      requests: { cpu: "2", memory: "4Gi" }
      limits:   { cpu: "4", memory: "8Gi" }
    postgres_storage_class: io2-csi
    postgres_storage_requirements: { requests: { storage: 200Gi } }
    postgres_extra_args:
      - '-c'
      - 'shared_buffers=2GB'
      - '-c'
      - 'work_mem=32MB'
      - '-c'
      - 'maintenance_work_mem=512MB'
      - '-c'
      - 'effective_cache_size=6GB'
      - '-c'
      - 'max_connections=1024'
      - '-c'
      - 'checkpoint_timeout=15min'
      - '-c'
      - 'checkpoint_completion_target=0.9'
      - '-c'
      - 'wal_compression=on'
      - '-c'
      - 'autovacuum_vacuum_scale_factor=0.05'
      - '-c'
      - 'autovacuum_analyze_scale_factor=0.02'
      - '-c'
      - 'synchronous_commit=off'
```

Gateway Route:

```yaml
metadata:
  annotations:
    haproxy.router.openshift.io/timeout: 1h
```

OS-level (VM nodes):

```
# /etc/security/limits.d/aap.conf
awx soft nofile 65535
awx hard nofile 65535
# /etc/sysctl.d/99-aap.conf
net.ipv4.ip_local_port_range = 15000 65000
fs.file-max = 2097152
```

Controller settings (UI → Settings → Jobs):

| Setting | Value |
|---|---|
| `DEFAULT_FORKS` | 50 |
| `MAX_FORKS` | 300 |
| `EVENT_STDOUT_MAX_BYTES_DISPLAY` | 1024 |
| `MAX_WEBSOCKET_EVENT_RATE_SECONDS` | 0.5 |
| Cleanup Jobs (Mgmt Job) | nightly, 30 days |
| Cleanup Activity Stream | nightly, 30 days |
