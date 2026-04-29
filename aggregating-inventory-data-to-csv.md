---
title: Aggregating Per-Host Data Into a Single File from Ansible / AAP
type: guide
product: Ansible / Red Hat Ansible Automation Platform
versions: [Ansible 2.14+, AAP 2.4, AAP 2.5]
scope: Writing data from many hosts into a single artifact (CSV, JSON, etc.)
audience: Ansible playbook authors and AAP operators
tags: [ansible, aap, csv, aggregation, run_once, delegate_to, job-slicing, patterns]
status: living-document
---

# Aggregating Per-Host Data Into a Single File from Ansible / AAP

> [!summary]
> A focused guide on **how to collect data from every host in an inventory and write it into a single output file** — typically CSV. Covers the four patterns that actually work, why the obvious approach (every host appends to the same file) doesn't, and what changes when you turn on **AAP Job Slicing** so the job runs as several parallel sibling jobs in different pods.
>
> Examples use a fleet OS report (`hostname, distribution, version, kernel`) but the patterns apply to any per-host aggregation: open ports, package inventory, compliance findings, hardware specs, etc.

> [!info] What this guide assumes
> - You are writing an Ansible playbook that runs against many hosts (tested examples target 1000-host fleets).
> - The deliverable is a **single file** with one row per host.
> - You may or may not be using **AAP Job Slicing**. The slicing-aware patterns are in [§5](#sec-5).
> - On AAP/OpenShift, every job runs in an ephemeral container Pod with its own filesystem — there is no persistent "the controller's filesystem."

---

<a id="sec-toc"></a>
## Contents

1. [The core problem](#sec-1)
2. [Pattern 1 — `run_once` + Jinja template (recommended default)](#sec-2)
3. [Pattern 2 — Why "everyone appends" looks tempting and why to avoid it](#sec-3)
4. [Pattern 3 — Robust CSV via Python for messy data](#sec-4)
5. [Pattern 4 — When the job is sliced](#sec-5)
6. [Pattern 5 — Aggregating to a shared backing store](#sec-6)
7. [Where the file actually goes (AAP artifacts)](#sec-7)
8. [Performance notes for large inventories](#sec-8)
9. [Decision tree — pick a pattern](#sec-9)
10. [Full working examples](#sec-10)

---

<a id="sec-1"></a>
## 1. The Core Problem

Ansible's execution model is **per-host, in parallel**. Given `forks: 100`, the same task runs on 100 hosts simultaneously. If each of those parallel executions tries to write to the same file, three things go wrong:

1. **Race conditions** — two forks `open(path, 'a')` at the same instant, both write a line, the second `write()` clobbers part of the first.
2. **Lost writes** — appends are not atomic across processes for arbitrary lengths; a row can be split between two writes from different forks.
3. **No shared filesystem** (on AAP/OpenShift) — every job runs in its own Pod with its own ephemeral `/tmp`, `/runner/`, and home directory. There is no "the controller's filesystem" that all hosts can write to.
4. **No shared filesystem across slices** — when AAP Job Slicing creates N sibling jobs, each runs in a separate Pod. They can't see each other's files.

The pattern that solves all four is always:

> **Gather data per-host into facts (parallel), then aggregate to a single file in one place at the end (serialized).**

Everything below is variations on that theme.

---

<a id="sec-2"></a>
## 2. Pattern 1 — `run_once` + Jinja Template (Recommended Default)

This is the canonical Ansible pattern. Every host populates a fact in parallel; one host renders a Jinja template that walks `hostvars` for all hosts.

### 2.1 Playbook

```yaml
- name: Inventory OS report
  hosts: all
  gather_facts: true

  tasks:
    - name: Capture per-host record
      ansible.builtin.set_fact:
        host_record:
          hostname: "{{ inventory_hostname }}"
          fqdn: "{{ ansible_facts['fqdn'] | default('') }}"
          distribution: "{{ ansible_facts['distribution'] | default('') }}"
          version: "{{ ansible_facts['distribution_version'] | default('') }}"
          major: "{{ ansible_facts['distribution_major_version'] | default('') }}"
          kernel: "{{ ansible_facts['kernel'] | default('') }}"

    - name: Render single CSV on the controller
      ansible.builtin.template:
        src: os_report.csv.j2
        dest: "{{ output_dir | default(playbook_dir + '/artifacts') }}/os_report.csv"
        mode: "0644"
      run_once: true
      delegate_to: localhost
```

### 2.2 Template — `templates/os_report.csv.j2`

```jinja
hostname,fqdn,distribution,version,major,kernel
{% for host in ansible_play_hosts | sort %}
{{ hostvars[host].host_record.hostname }},{{ hostvars[host].host_record.fqdn }},{{ hostvars[host].host_record.distribution }},{{ hostvars[host].host_record.version }},{{ hostvars[host].host_record.major }},{{ hostvars[host].host_record.kernel }}
{% endfor %}
```

### 2.3 Why this works

- **`set_fact` runs in parallel** on every host — that's the per-host part.
- **`run_once: true` + `delegate_to: localhost`** picks one host (whichever Ansible touches first) and runs the template task **on the controller**, exactly once.
- **`hostvars` is global** — at template-render time, the chosen host can read every other host's `host_record` via `hostvars[host]`.
- **`ansible_play_hosts | sort`** gives deterministic CSV ordering, so diffs across runs are meaningful.

> [!tip] `ansible_play_hosts` vs `ansible_play_hosts_all`
> - `ansible_play_hosts` — only hosts still in scope at this point (failed/unreachable hosts are dropped).
> - `ansible_play_hosts_all` — every host the play started with, including ones that failed earlier.
> - For inventory reports use `ansible_play_hosts_all` so failed hosts still appear (probably with empty fields). For "what we successfully verified" reports, use `ansible_play_hosts`.

### 2.4 Failed-host handling

If a host fails before `set_fact`, its `hostvars[host].host_record` will be undefined and the template render will error. Defensive template:

```jinja
hostname,fqdn,distribution,version,major,kernel,status
{% for host in ansible_play_hosts_all | sort %}
{% set rec = hostvars[host].host_record | default({}) %}
{{ host }},{{ rec.fqdn | default('') }},{{ rec.distribution | default('') }},{{ rec.version | default('') }},{{ rec.major | default('') }},{{ rec.kernel | default('') }},{{ 'ok' if rec else 'failed' }}
{% endfor %}
```

This produces a CSV that includes **every** host with an explicit `status` column distinguishing successful from failed gathers.

---

<a id="sec-3"></a>
## 3. Pattern 2 — Why "Everyone Appends" Looks Tempting (and Why to Avoid It)

Many first attempts at this look like:

```yaml
# Don't do this
- name: Append host line to shared CSV
  ansible.builtin.lineinfile:
    path: /tmp/os_report.csv
    line: "{{ inventory_hostname }},{{ ansible_distribution_version }}"
    create: true
  delegate_to: localhost
```

Three reasons not to use it:

1. **Concurrency** — `lineinfile` reads the file, modifies it, writes it back. With `forks: 100` you have 100 read-modify-write cycles racing each other. Lines silently disappear.
2. **No deterministic ordering** — even if writes were atomic, hosts complete in non-deterministic order, so the CSV layout differs run-to-run.
3. **No header management** — first writer needs to add the header, subsequent ones must not. `lineinfile` has no notion of "first."

You can sometimes make it work with `throttle: 1` or a serializing lock, but at that point you've turned a parallel playbook into a serial one — slow, fragile, and harder to read than Pattern 1.

> [!warning]
> The exception is `lineinfile` with `delegate_to` to a **single named delegation host** combined with `throttle: 1`. This serializes writes correctly. It's still slower and more brittle than Pattern 1 — there's almost never a reason to prefer it.

---

<a id="sec-4"></a>
## 4. Pattern 3 — Robust CSV via Python for Messy Data

Pattern 1 is fine when fields are well-behaved (no commas, quotes, or newlines). Real-world data often isn't. If any field could contain a comma (e.g., RAM string `"16,384 MB"`), a quote, or a newline, use Python's `csv` module rather than hand-rolled Jinja.

### 4.1 Inline approach using `copy` + `to_nice_json`

The simplest variant: write the per-host records as JSON, then post-process to CSV outside Ansible:

```yaml
- name: Aggregate records as JSON
  ansible.builtin.copy:
    dest: "{{ output_dir }}/os_report.json"
    mode: "0644"
    content: |
      {{
        ansible_play_hosts_all
        | map('extract', hostvars, 'host_record')
        | list
        | to_nice_json
      }}
  run_once: true
  delegate_to: localhost
```

JSON is far more forgiving of weird characters. Convert to CSV with `jq`, `pandas`, `csvkit`, or whatever your downstream pipeline prefers.

### 4.2 Inline Python via `script` or `command`

If the deliverable must be CSV produced inside the playbook, drop into Python:

```yaml
- name: Materialize aggregated records to a temp file
  ansible.builtin.copy:
    dest: "/tmp/os_report.json"
    content: "{{ ansible_play_hosts_all | map('extract', hostvars, 'host_record') | list | to_json }}"
  run_once: true
  delegate_to: localhost

- name: Render with Python's csv module
  ansible.builtin.shell: |
    python3 - <<'PY'
    import csv, json, sys
    rows = json.load(open('/tmp/os_report.json'))
    fields = ['hostname','fqdn','distribution','version','major','kernel']
    with open('{{ output_dir }}/os_report.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})
    PY
  run_once: true
  delegate_to: localhost
```

This handles embedded commas, quotes, newlines, and Unicode correctly. For most inventory-style data Pattern 1 is fine; reach for this only when fields are user-controlled or include free-text descriptions.

---

<a id="sec-5"></a>
## 5. Pattern 4 — When the Job Is Sliced

**AAP Job Slicing** creates N parallel sibling jobs from a single template. Each slice runs against `inventory_size / N` hosts in **its own Pod with its own ephemeral filesystem**. Pattern 1's `delegate_to: localhost` becomes "delegate to *this slice's* localhost" — the four slice pods produce four separate CSVs that never see each other.

There are two ways to handle this.

### 5.1 Option A — Each slice writes its own file, merge afterward

Each slice writes a distinctly-named file into the artifacts directory, then a final workflow step concatenates them.

```yaml
- name: Inventory OS report (slice-aware)
  hosts: all
  gather_facts: true

  tasks:
    - name: Capture per-host record
      ansible.builtin.set_fact:
        host_record:
          hostname: "{{ inventory_hostname }}"
          fqdn: "{{ ansible_facts['fqdn'] | default('') }}"
          distribution: "{{ ansible_facts['distribution'] | default('') }}"
          version: "{{ ansible_facts['distribution_version'] | default('') }}"
          major: "{{ ansible_facts['distribution_major_version'] | default('') }}"
          kernel: "{{ ansible_facts['kernel'] | default('') }}"

    - name: Write per-slice CSV to AAP artifacts
      ansible.builtin.template:
        src: os_report.csv.j2
        # AAP exposes these via env / extra_vars on every job
        dest: >-
          /runner/artifacts/{{ awx_job_id | default('local') }}/os_report_slice_{{
            awx_job_slice_number | default(0) }}_of_{{ awx_job_slice_count | default(1) }}.csv
        mode: "0644"
      run_once: true
      delegate_to: localhost
```

Each slice produces:

```
os_report_slice_0_of_4.csv
os_report_slice_1_of_4.csv
os_report_slice_2_of_4.csv
os_report_slice_3_of_4.csv
```

These appear in each slice job's **Artifacts** in the AAP UI.

#### Merging the slice files

In AAP, do this as a **final node in a Workflow Template** after the sliced job converges. The merge node runs a tiny playbook against `localhost` that:

1. Uses the `awx.awx` collection to query the parent workflow for the slice job IDs.
2. Downloads each slice's `os_report_slice_*.csv` from the Controller artifacts API.
3. Concatenates them — keeping one header — into `os_report.csv`.

Sketch of the merge playbook:

```yaml
- name: Merge sliced CSV outputs
  hosts: localhost
  gather_facts: false
  vars:
    controller_host: "{{ tower_host }}"
    parent_workflow_id: "{{ awx_parent_job_id }}"
  tasks:
    - name: List child jobs of the slicing parent
      ansible.builtin.uri:
        url: "https://{{ controller_host }}/api/v2/workflow_jobs/{{ parent_workflow_id }}/workflow_nodes/"
        method: GET
        validate_certs: false
        headers:
          Authorization: "Bearer {{ controller_token }}"
        return_content: true
      register: nodes

    - name: Download each slice's CSV artifact
      ansible.builtin.uri:
        url: "https://{{ controller_host }}/api/v2/jobs/{{ item.summary_fields.job.id }}/stdout/?format=txt"
        dest: "/tmp/slice_{{ item.summary_fields.job.id }}.csv"
        validate_certs: false
        headers:
          Authorization: "Bearer {{ controller_token }}"
      loop: "{{ nodes.json.results }}"

    - name: Concatenate, deduplicating header
      ansible.builtin.shell: |
        head -n 1 /tmp/slice_*.csv | head -n 1 > /runner/artifacts/os_report.csv
        tail -n +2 -q /tmp/slice_*.csv >> /runner/artifacts/os_report.csv
      args:
        executable: /bin/bash
```

> [!note]
> The exact API endpoint for downloading job artifacts varies slightly between AAP 2.4 and 2.5; the `awx.awx.controller_api` module abstracts this. The shape above is illustrative — adapt to your version's API.

### 5.2 Option B — Each slice writes directly to a shared backing store

Skip the per-slice files entirely. Each host writes its row to a service all slices can talk to. Aggregation becomes "read everything out of the store and emit a CSV." See [§6](#sec-6).

### 5.3 Detecting whether you're sliced

If your playbook may be run either sliced or not, branch on `awx_job_slice_count`:

```yaml
- name: Choose output strategy based on slice context
  ansible.builtin.set_fact:
    is_sliced: "{{ (awx_job_slice_count | default(1) | int) > 1 }}"
    output_path: >-
      {%- if (awx_job_slice_count | default(1) | int) > 1 -%}
      /runner/artifacts/{{ awx_job_id }}/os_report_slice_{{ awx_job_slice_number }}_of_{{ awx_job_slice_count }}.csv
      {%- else -%}
      /runner/artifacts/os_report.csv
      {%- endif -%}
  run_once: true
  delegate_to: localhost
```

---

<a id="sec-6"></a>
## 6. Pattern 5 — Aggregating to a Shared Backing Store

For large fleets, recurring reports, or anything that has downstream consumers (BI, CMDB, dashboards), don't try to reconcile files. Have every host **write its row to a shared service** and produce the CSV from a single query at the end.

### 6.1 Why this beats file-merging at scale

- No per-slice file shuffle.
- The same approach scales from 1 to 50 slices without changing the playbook.
- The data is queryable beyond just the CSV — you can build dashboards, alerts, or differential reports.
- Failed hosts can still be enumerated (you know who *should* have written and didn't).

### 6.2 Choices of backing store

| Store | Best for | Trade-off |
|---|---|---|
| PostgreSQL / MySQL | Long-term, queryable inventory state | Need a schema + migrations |
| Redis | Ephemeral aggregation per run; you already have it in AAP 2.5 | Not durable; not great for huge values |
| HTTP collector / webhook | Decoupling AAP from the storage layer | One more service to operate |
| S3 / object storage | Append-only audit log of inventory snapshots | Listing + merging is slower |
| Automation Hub / CMDB | Authoritative state, multi-tool consumers | Integration cost |

### 6.3 Sketch using Redis (already present in AAP 2.5)

```yaml
- name: Push per-host record to Redis
  community.general.redis_data:
    login_host: "{{ aap_redis_host }}"
    login_port: 6379
    login_db: 2                       # dedicated DB for these reports
    key: "os_report:{{ awx_job_id | default('local') }}:{{ inventory_hostname }}"
    value: "{{ host_record | to_json }}"
    expiration: 86400                 # auto-cleanup after 24h
  delegate_to: localhost
```

Then in a final workflow step:

```yaml
- name: Export Redis rows to CSV
  hosts: localhost
  gather_facts: false
  vars:
    parent_job_id: "{{ awx_workflow_job_id | default(awx_job_id) }}"
  tasks:
    - name: Get all keys for this report run
      community.general.redis_data_info:
        login_host: "{{ aap_redis_host }}"
        login_port: 6379
        login_db: 2
        key: "os_report:{{ parent_job_id }}:*"
      register: keys

    - name: Fetch values
      community.general.redis_data:
        login_host: "{{ aap_redis_host }}"
        login_port: 6379
        login_db: 2
        command: get
        key: "{{ item }}"
      loop: "{{ keys.keys }}"
      register: rows

    - name: Render unified CSV
      ansible.builtin.copy:
        dest: "/runner/artifacts/os_report.csv"
        content: |
          hostname,fqdn,distribution,version,major,kernel
          {% for r in rows.results %}
          {% set rec = r.value | from_json %}
          {{ rec.hostname }},{{ rec.fqdn }},{{ rec.distribution }},{{ rec.version }},{{ rec.major }},{{ rec.kernel }}
          {% endfor %}
```

### 6.4 Sketch using PostgreSQL

For durable reports, the cleanest pattern. Schema:

```sql
CREATE TABLE inventory_os_report (
    job_id        bigint        NOT NULL,
    captured_at   timestamptz   NOT NULL DEFAULT now(),
    hostname      text          NOT NULL,
    fqdn          text,
    distribution  text,
    version       text,
    major         text,
    kernel        text,
    PRIMARY KEY (job_id, hostname)
);
```

Per-host insert:

```yaml
- name: Insert host record
  community.postgresql.postgresql_query:
    login_host: "{{ pg_host }}"
    login_user: "{{ pg_user }}"
    login_password: "{{ pg_password }}"
    db: inventory_reports
    query: |
      INSERT INTO inventory_os_report
        (job_id, hostname, fqdn, distribution, version, major, kernel)
      VALUES
        (%(job_id)s, %(hostname)s, %(fqdn)s, %(distribution)s,
         %(version)s, %(major)s, %(kernel)s)
      ON CONFLICT (job_id, hostname) DO UPDATE SET
        fqdn         = EXCLUDED.fqdn,
        distribution = EXCLUDED.distribution,
        version      = EXCLUDED.version,
        major        = EXCLUDED.major,
        kernel       = EXCLUDED.kernel,
        captured_at  = now();
    named_args:
      job_id: "{{ awx_job_id | default(0) | int }}"
      hostname: "{{ inventory_hostname }}"
      fqdn: "{{ ansible_facts['fqdn'] | default('') }}"
      distribution: "{{ ansible_facts['distribution'] | default('') }}"
      version: "{{ ansible_facts['distribution_version'] | default('') }}"
      major: "{{ ansible_facts['distribution_major_version'] | default('') }}"
      kernel: "{{ ansible_facts['kernel'] | default('') }}"
  delegate_to: localhost
```

Final export — pure SQL, no Ansible needed:

```sql
\copy (SELECT hostname, fqdn, distribution, version, major, kernel
       FROM inventory_os_report
       WHERE job_id = :job_id
       ORDER BY hostname) TO 'os_report.csv' CSV HEADER;
```

> [!tip] Don't use the AAP-managed PostgreSQL for this
> The PG instance bundled with AAP is for the controller's own state. Putting your own report tables in there will work but is operationally messy and mixes lifecycles. Run a separate database — even a tiny one — for your inventory reports.

---

<a id="sec-7"></a>
## 7. Where the File Actually Goes (AAP Artifacts)

The output path matters as much as the playbook logic.

### 7.1 `/runner/artifacts/<job_id>/`

This is the directory `ansible-runner` carves out for each job in AAP. Files written here:

- Are visible in the Controller UI under the job's **Artifacts** section.
- Survive the job pod's lifetime (collected before the pod terminates).
- Can be downloaded via the Controller API: `/api/v2/jobs/<id>/artifacts/`.

> [!example] Recommended path
> `/runner/artifacts/{{ awx_job_id | default('local') }}/os_report.csv`
>
> Falls back to `local` for direct CLI runs, uses the real job ID under AAP.

### 7.2 `set_stats` for cross-job data passing

If the file is small enough to be a few values, expose it as **job stats** instead of a file:

```yaml
- name: Expose summary as job stats
  ansible.builtin.set_stats:
    data:
      total_hosts: "{{ ansible_play_hosts_all | length }}"
      el8_hosts: "{{ ansible_play_hosts | map('extract', hostvars, ['host_record','major']) | select('eq','8') | list | length }}"
      el9_hosts: "{{ ansible_play_hosts | map('extract', hostvars, ['host_record','major']) | select('eq','9') | list | length }}"
    per_host: false
  run_once: true
  delegate_to: localhost
```

These appear in the job summary and can be consumed by later workflow nodes via `{{ artifacts['total_hosts'] }}`. Use this for *summary statistics*, not for the CSV body itself.

### 7.3 Don't write to `/tmp`

`/tmp` inside an EE pod is ephemeral and not collected as an artifact. The file vanishes when the pod terminates. Always write to `/runner/artifacts/<job_id>/` if you want to retrieve the file later.

---

<a id="sec-8"></a>
## 8. Performance Notes for Large Inventories

### 8.1 Narrow `gather_facts`

If the report only needs `distribution`, `kernel`, and `fqdn`, scope the gather:

```yaml
- hosts: all
  gather_facts: false
  pre_tasks:
    - name: Minimal fact gather
      ansible.builtin.setup:
        gather_subset:
          - '!all'
          - '!min'
          - distribution
          - kernel
        gather_timeout: 10
```

At 1000 hosts this turns a 5-minute fact gather into roughly 30–60 seconds.

### 8.2 `hostvars` size

`hostvars[host]` includes **everything** every host has set. For large fleets the in-memory size matters. Patterns to keep it lean:

- Only `set_fact` the small dict you actually need (`host_record`), not the full `ansible_facts`.
- Don't `set_fact` with `cacheable: true` for the report record unless you want it persisted in the fact cache.

### 8.3 Template render cost

Pattern 1's template iterates `ansible_play_hosts | sort` once. At 1000 hosts × 6 fields that's ~6000 dictionary lookups — sub-second on the controller. No tuning needed.

If you push past 10,000 hosts and the template render gets noticeable, switch to Pattern 3 (Python-rendered) or Pattern 5 (database-rendered) — both scale linearly with much smaller per-row cost.

### 8.4 Slicing decisions for report-only jobs

For a pure inventory report against 1000 hosts:

- **Forks=75-100, Job Slicing=1** — completes in a few minutes, produces one clean CSV via Pattern 1. Recommended.
- **Forks=75, Job Slicing=4** — completes faster but forces you into Pattern 4 (per-slice files + merge step) for the same report. Only worth it if other steps in the workflow need slicing.

Don't slice just to slice. Pattern 1 with no slicing is the simplest, fastest path for most inventory reports.

---

<a id="sec-9"></a>
## 9. Decision Tree — Pick a Pattern

```
1. Is the playbook sliced (AAP Job Slicing > 1)?
   ├── Yes → §5 (Pattern 4) or §6 (Pattern 5)
   │        ├── Report is one-off / per-run     → §5 Option A: per-slice files + merge
   │        └── Report is recurring / queryable → §6 (Redis or Postgres)
   └── No  → continue
2. Could fields contain commas, quotes, or newlines?
   ├── Yes → §4 (Pattern 3, Python csv module)
   └── No  → §2 (Pattern 1, run_once + Jinja)  ← the default for most cases
3. Does the data have downstream consumers (BI, CMDB, dashboards)?
   ├── Yes → §6 (Pattern 5) regardless of slicing — files are not the right abstraction
   └── No  → stick with the choice from steps 1-2
```

---

<a id="sec-10"></a>
## 10. Full Working Examples

### 10.1 Pattern 1 — Single-job OS report

`os_report.yml`:

```yaml
---
- name: Inventory OS report
  hosts: all
  gather_facts: false

  pre_tasks:
    - name: Minimal fact gather
      ansible.builtin.setup:
        gather_subset:
          - '!all'
          - '!min'
          - distribution
          - kernel
        gather_timeout: 10

  tasks:
    - name: Capture per-host record
      ansible.builtin.set_fact:
        host_record:
          hostname: "{{ inventory_hostname }}"
          fqdn: "{{ ansible_facts['fqdn'] | default('') }}"
          distribution: "{{ ansible_facts['distribution'] | default('') }}"
          version: "{{ ansible_facts['distribution_version'] | default('') }}"
          major: "{{ ansible_facts['distribution_major_version'] | default('') }}"
          kernel: "{{ ansible_facts['kernel'] | default('') }}"

    - name: Render single CSV on the controller
      ansible.builtin.template:
        src: os_report.csv.j2
        dest: "/runner/artifacts/{{ awx_job_id | default('local') }}/os_report.csv"
        mode: "0644"
      run_once: true
      delegate_to: localhost
```

`templates/os_report.csv.j2`:

```jinja
hostname,fqdn,distribution,version,major,kernel,status
{% for host in ansible_play_hosts_all | sort %}
{% set rec = hostvars[host].host_record | default({}) %}
{{ host }},{{ rec.fqdn | default('') }},{{ rec.distribution | default('') }},{{ rec.version | default('') }},{{ rec.major | default('') }},{{ rec.kernel | default('') }},{{ 'ok' if rec else 'failed' }}
{% endfor %}
```

### 10.2 Pattern 4 — Sliced version, per-slice files

Same playbook with the dest path changed:

```yaml
    - name: Render per-slice CSV
      ansible.builtin.template:
        src: os_report.csv.j2
        dest: >-
          /runner/artifacts/{{ awx_job_id | default('local') }}/os_report_slice_{{
            awx_job_slice_number | default(0) }}_of_{{ awx_job_slice_count | default(1) }}.csv
        mode: "0644"
      run_once: true
      delegate_to: localhost
```

Plus a merge node in the AAP Workflow Template (see [§5.1](#sec-5)).

### 10.3 Pattern 5 — Redis-backed aggregation (works regardless of slicing)

Per-host:

```yaml
    - name: Push host record to Redis
      community.general.redis_data:
        login_host: "{{ aap_redis_host }}"
        login_port: 6379
        login_db: 2
        key: "os_report:{{ awx_workflow_job_id | default(awx_job_id) }}:{{ inventory_hostname }}"
        value: "{{ host_record | to_json }}"
        expiration: 86400
      delegate_to: localhost
```

Final workflow node (separate template `os_report_export.yml`):

```yaml
---
- name: Export Redis-aggregated inventory report to CSV
  hosts: localhost
  gather_facts: false

  vars:
    parent_id: "{{ awx_workflow_job_id | default(awx_job_id) }}"

  tasks:
    - name: Get all keys for this run
      community.general.redis_data_info:
        login_host: "{{ aap_redis_host }}"
        login_port: 6379
        login_db: 2
        key: "os_report:{{ parent_id }}:*"
      register: keys

    - name: Fetch values
      community.general.redis_data:
        login_host: "{{ aap_redis_host }}"
        login_port: 6379
        login_db: 2
        command: get
        key: "{{ item }}"
      loop: "{{ keys.keys }}"
      register: rows

    - name: Render unified CSV
      ansible.builtin.copy:
        dest: "/runner/artifacts/{{ awx_job_id }}/os_report.csv"
        mode: "0644"
        content: |
          hostname,fqdn,distribution,version,major,kernel
          {% for r in rows.results %}
          {% set rec = r.value | from_json %}
          {{ rec.hostname }},{{ rec.fqdn }},{{ rec.distribution }},{{ rec.version }},{{ rec.major }},{{ rec.kernel }}
          {% endfor %}
```

---

## 11. Quick Reference

| Need | Pattern | Section |
|---|---|---|
| One-off report, no slicing | `run_once` + Jinja | [§2](#sec-2) |
| One-off report, fields may have commas | `run_once` + Python csv | [§4](#sec-4) |
| Sliced job, ad-hoc | per-slice files + merge | [§5.1](#sec-5) |
| Sliced job, recurring | Redis or PG aggregation | [§6](#sec-6) |
| Inventory state for CMDB/BI | PG aggregation | [§6.4](#sec-6) |
| Quick summary numbers only | `set_stats` | [§7.2](#sec-7) |
| Avoid this | everyone-appends with `lineinfile` | [§3](#sec-3) |
