---
title: AAP Config Snippets — Canonical Reference
type: reference
product: Red Hat Ansible Automation Platform
versions: [AAP 2.4, AAP 2.5, AAP 2.6]
audience: AAP platform / automation engineers
status: living-document
---

# AAP Config Snippets — Canonical Reference

> [!NOTE]
> This is the **single source of truth** for the configuration blocks that recur across the tuning and architecture guides. Each block is presented in its **base form**, followed by the **valid variants** and the **trade-off of each** — because at scale there is rarely one "right" answer, only choices with different costs.
>
> The narrative guides keep a short, context-specific version inline and link back here for the full block and the alternatives. If you change a value here, it is the canonical version to reconcile the others against.

## Contents

1. [Execution Environment `ansible.cfg`](#1-execution-environment-ansiblecfg)
2. [Container group `pod_spec_override`](#2-container-group-pod_spec_override)
3. [PostgreSQL tuning](#3-postgresql-tuning)
4. [OS-level limits (VM execution nodes)](#4-os-level-limits-vm-execution-nodes)
5. [Managed-host SSHD](#5-managed-host-sshd)

---

## 1. Execution Environment `ansible.cfg`

Baked into a custom EE with `ansible-builder` at `/etc/ansible/ansible.cfg`. This is the base every guide starts from:

```ini
[defaults]
strategy = free
forks = 100
gather_timeout = 30
timeout = 30
internal_poll_interval = 0.001
host_key_checking = False
display_skipped_hosts = False
callbacks_enabled = profile_tasks, timer
stdout_callback = default

# Fact caching — pick a backend below
fact_caching = jsonfile
fact_caching_connection = /runner/artifacts/fact_cache
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

### Fact-caching backends — three valid ways

The one line worth deciding deliberately is `fact_caching`. All three work; they differ in **where the cache lives and whether it survives across runs**.

| Backend | Connection | Cross-run reuse | Best for | Trade-off |
|---|---|---|---|---|
| `jsonfile` | `/runner/artifacts/fact_cache` | No (per-run on OCP — EE filesystem is ephemeral) | Simplest default; single-job reports | No reuse between jobs unless the path is on a persisted PVC |
| `redis` | `aap-redis-svc:6379:1` | Yes | Repeated runs against the same fleet at scale | Needs a reachable Redis; adds an external dependency |
| `memory` | — (default) | No | Tiny ad-hoc runs | Lost the instant the play ends |

```ini
# Variant: Redis-backed fact cache (cross-run reuse)
fact_caching = redis
fact_caching_connection = aap-redis-svc:6379:1
fact_caching_timeout = 7200
```

> [!IMPORTANT]
> On **AAP 2.5 OpenShift** the platform already runs a Redis pod for the gateway. **Do not reuse it for fact caching** — stand up a *separate* Redis instance, or use the `jsonfile` backend on a persisted PVC, to avoid cross-tenant noise on the Redis that backs the UI.

---

## 2. Container group `pod_spec_override`

Set in Controller UI → **Instance Groups → Container Group → Customize pod spec**. This is the **single biggest lever** for the "freeze / OOMKilled" symptom on OpenShift. The full annotated form:

```yaml
apiVersion: v1
kind: Pod
metadata:
  namespace: aap
  labels:
    aap.example.com/workload: large-fleet
spec:
  serviceAccountName: default
  automountServiceAccountToken: false        # EE never talks to the K8s API — drop the token

  # Keep job pods off the control plane and away from latency-sensitive workloads
  nodeSelector:
    aap-workload: execution
  tolerations:
    - key: aap-workload
      operator: Equal
      value: execution
      effect: NoSchedule

  # Spread slice siblings so one node failure doesn't kill the whole run
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
      imagePullPolicy: IfNotPresent           # paired with a pre-pull DaemonSet → fast start
      args: ['ansible-runner', 'worker', '--private-data-dir=/runner']
      resources:
        requests: { cpu: "1", memory: "4Gi" } # steady-state EE footprint → bin-packing
        limits:   { cpu: "4", memory: "8Gi" } # peak (fact gather at job start) → OOM ceiling
      env:
        - name: ANSIBLE_FORCE_COLOR
          value: "0"
        - name: ANSIBLE_STDOUT_CALLBACK
          value: "default"
        - name: ANSIBLE_CALLBACKS_ENABLED
          value: "profile_tasks,timer"
```

> [!TIP]
> `requests` ≪ `limits` is deliberate: size `requests` to steady-state EE memory for efficient bin-packing, and `limits` to the peak during fact gathering. Memory limit `<` peak fact-gather memory `=` `OOMKilled`.

**Image, by version** — the only line that changes between releases:

```yaml
# [2.5] registry.redhat.io/ansible-automation-platform-25/ee-supported-rhel9:latest
# [2.4] registry.redhat.io/ansible-automation-platform-24/ee-supported-rhel9:latest
# Production: pin a custom EE by tag/digest, never :latest
```

> [!WARNING]
> A namespace `LimitRange` silently overrides Pod-level `resources`. If `LimitRange.max` is below these `limits`, pods schedule with the clamped values. Always `oc describe limitrange` when sizing seems ignored.

---

## 3. PostgreSQL tuning

The same knobs, expressed two ways depending on how PostgreSQL is deployed.

### VM / external PG — `postgresql.conf`

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

### OpenShift Operator — `postgres_extra_args` in the CR

Same values, passed as `-c` flags on the managed PG container:

```yaml
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
  - 'random_page_cost=1.1'
  - '-c'
  - 'autovacuum_vacuum_scale_factor=0.05'
  - '-c'
  - 'autovacuum_analyze_scale_factor=0.02'
```

For 2500+ hosts, additionally: `max_wal_size=4GB`, `min_wal_size=1GB`, `wal_buffers=16MB`.

### The `synchronous_commit` trade-off

```ini
synchronous_commit = off
```

> [!WARNING]
> `synchronous_commit = off` trades a ~200 ms window of un-flushed transactions for a large gain in event-ingest throughput. **Acceptable for AAP** because job events in that window are recoverable from the artifacts directory. **Never** use it for the controller's encryption-key tables.

---

## 4. OS-level limits (VM execution nodes)

`/etc/security/limits.d/aap.conf`:

```
awx soft nofile 65535
awx hard nofile 65535
awx soft nproc  32768
awx hard nproc  32768
```

`/etc/sysctl.d/99-aap.conf`:

```
net.ipv4.ip_local_port_range = 15000 65000
net.core.somaxconn = 1024
net.ipv4.tcp_tw_reuse = 1
fs.file-max = 2097152
```

> [!NOTE]
> On the **2.5 containerized** installer, also raise the user-slice limit so the install user's systemd units aren't capped:
> ```ini
> # /etc/systemd/system/user-<UID>.slice.d/override.conf
> [Slice]
> TasksMax=infinity
> ```
> And remember `loginctl enable-linger <install-user>`, or all `--user` units stop on logout.

---

## 5. Managed-host SSHD

On every host in scope of large, high-fork jobs — `sshd_config`:

```
MaxStartups 100:30:200
```

> [!TIP]
> The default `10:30:100` rejects inbound SSH once ~10 unauthenticated connections are in flight. At high `forks × slices` this surfaces as random "unreachable" failures that look like an AAP problem but are the managed host refusing the connection.
