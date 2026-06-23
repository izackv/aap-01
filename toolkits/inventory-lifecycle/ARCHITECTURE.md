# Inventory lifecycle — architecture

How the pieces fit, where each runs, why the design choices look the way they
do. The README is intent and usage; SPEC is contract and behavior; this is the
"if you're touching the internals, read this first" document.

Companion documents:
- [README.md](README.md) — purpose, usage, tunables
- [SPEC.md](SPEC.md) — state model, rules, output formats, removal mechanics, exit codes

---

## 1. Component map

```
┌────────────────────────────── EE (ephemeral) ──────────────────────────────┐
│                                                                            │
│  Core flow:   ingest.yml ──► evaluate.yml ──► act.yml ──► restore.yml      │
│                                                                            │
│  Operator:    ack.yml      rename-inventory.yml   retire-inventory.yml     │
│               add-hosts.yml                                                │
│                                                                            │
│  AAP-reaching playbooks (act, restore, add-hosts) call ─► AAP REST API     │
│  All others reach mng_host only.                                           │
└────────────────────────────────────────────────────────────────────────────┘
                                  │
┌─────────────────── mng_host (persistent) ──────────────────────────────────┐
│                                                                            │
│   files/lifecycle_db.py     files/classify_state.py                        │
│   files/evaluate_rules.py   files/aap_client.py                            │
│   files/prune_csv.py        files/add_hosts.py                             │
│                                                                            │
│   /myshare/data/                                                           │
│   ├── <inv>_<date>_<jobid>_servers_info.csv   ← from identify-hosts        │
│   ├── lifecycle/                                                           │
│   │   ├── state.sqlite                          ← DB lives here            │
│   │   └── reports/<inventory>/<UTC-date>/...    ← per-run output tree      │
│   └── …                                                                    │
└────────────────────────────────────────────────────────────────────────────┘
```

Mirrors `identify-hosts`: the EE is a thin orchestrator; the work happens on
`mng_host`. The EE only needs `python3` (stdlib) and a network path to AAP's
REST API for `act.yml`/`restore.yml`. The state is on a real, persistent host
because EEs are ephemeral by design.

### Module responsibilities

| Module | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `lifecycle_db.py` | open/create SQLite DB, run migrations, expose `ingest_csv(path)`, `load_allowlist_csv(path)`, `query_*`, `upsert_host_state(...)`, ack CRUD | DB path, CSV path | rows written/read |
| `classify_state.py` | pure function `row → observation_state` per [SPEC §1](SPEC.md#1-state-model) | one parsed CSV row | one of the 7 states |
| `evaluate_rules.py` | pure functions over the DB; one function per rule (§3.1–3.4); a top-level `evaluate_inventory(db, inventory, tunables)` that returns the three candidate lists + gate decisions, with ack filtering applied | DB connection, tunables | report-ready dicts |
| `aap_client.py` | thin stdlib `urllib` wrapper: `inventory_id_by_name`, `host_id_by_fqdn`, `disable_host`, `delete_host`, `enable_host`, `add_host`. No third-party deps. | URL, token | dict responses; raises on HTTP error |
| `add_hosts.py` | re-add / new-add CLI that prints prior lifecycle history then POSTs via `aap_client.add_host`. Standalone (no playbook required). | URL, token, inventory, fqdn(s) | per-host status lines + `actions` rows |
| `prune_csv.py` | safe-delete old CSVs honoring evidence rule | DB connection (to find evidence), `csv_glob`, `keep_csv_days` | list of deleted/kept paths |

These are **stdlib-only** Python modules. No `pandas`, no `sqlalchemy`, no
`requests`. That keeps the toolkit air-gap-safe and removes one entire class of
"the EE doesn't have package X" failures.

---

## 2. Data flow per run

```
identify-hosts CSV(s) on share
        │
        │ (1) glob, hash, skip-if-known
        ▼
ingest.yml ──→ lifecycle_db.ingest_csv() ──→ runs + observations rows
                       │
                       │ (2) recompute affected rows
                       ▼
                  host_state rows
                       │
                       │ (3) expire stale acks (ack_until < today)
                       ▼
                  acks rows updated
        │
        │ (4) read-only query
        ▼
evaluate.yml ──→ evaluate_rules.evaluate_inventory() ──→ candidate lists
                       │   (active acks filter the lists here)
                       │ (5) sanity gates
                       ▼
                  render reports → report_dir/<inventory>/<date>/
        │
        │ (6) opt-in
        ▼
act.yml ──→ aap_client.{disable_host|delete_host}() + prune_csv (preserves evidence)
                       │
                       ▼
                  actions table + actions.csv

restore.yml ──→ aap_client.enable_host()  (reads a prior actions.csv)
                       │
                       ▼
                  actions table appended (action_kind=reenable-host)

ack.yml ──→ lifecycle_db.{insert_ack|extend_ack|list_acks|remove_ack}()
                       │
                       ▼
                  acks table updated
```

Numbered steps map to the [SPEC §5 playbook descriptions](SPEC.md#5-playbooks--required-behavior).

---

## 3. SQLite schema

A single DB file at `db_path` (default `/myshare/data/lifecycle/state.sqlite`).
WAL mode for concurrent reads during writes. Schema version stored in
`PRAGMA user_version`; `lifecycle_db.py` refuses to open a DB whose version it
doesn't understand and refuses to silently migrate down.

```sql
PRAGMA user_version = 1;
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per inventory the toolkit has ever seen. Operator playbooks
-- (rename-inventory.yml, retire-inventory.yml) mutate this; ingest.yml
-- creates rows on first sight.
CREATE TABLE inventories (
    inventory     TEXT    PRIMARY KEY,
    status        TEXT    NOT NULL,    -- 'active' | 'renamed' | 'retired'
    first_seen    TEXT    NOT NULL,
    last_ingest   TEXT,
    retired_at    TEXT,
    renamed_from  TEXT,                -- audit: prior name, if any
    notes         TEXT
);

CREATE TABLE runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory         TEXT    NOT NULL,
    scan_date         TEXT    NOT NULL,   -- ISO date from filename
    aap_job_id        TEXT,
    csv_path          TEXT    NOT NULL,
    csv_sha256        TEXT    NOT NULL,
    total_hosts       INTEGER NOT NULL,
    scanned_hosts     INTEGER NOT NULL,
    dropped_rows      INTEGER NOT NULL DEFAULT 0,
    ingest_ts         TEXT    NOT NULL,   -- ISO ts of ingest, not scan
    is_successful_run INTEGER NOT NULL,   -- 1/0 per SPEC §3.5
    UNIQUE(csv_sha256)
);

CREATE TABLE observations (
    run_id            INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    inventory         TEXT    NOT NULL,
    fqdn              TEXT    NOT NULL,   -- lowercased on insert
    ipv4              TEXT,
    mac               TEXT,
    mac_vendor        TEXT,
    os                TEXT,
    state             TEXT    NOT NULL,   -- one of 7 observation states
    detection_method  TEXT,
    error_class       TEXT,
    raw_reachable     INTEGER,
    raw_ssh_open      INTEGER,
    last_checked      TEXT,
    PRIMARY KEY (run_id, inventory, fqdn)
);
CREATE INDEX idx_obs_inv_fqdn ON observations(inventory, fqdn);
CREATE INDEX idx_obs_state    ON observations(state);

CREATE TABLE host_state (
    inventory               TEXT    NOT NULL,
    fqdn                    TEXT    NOT NULL,
    first_seen              TEXT    NOT NULL,
    last_observed           TEXT    NOT NULL,
    last_seen_up            TEXT,
    consecutive_misses      INTEGER NOT NULL DEFAULT 0,
    consecutive_silent_runs INTEGER NOT NULL DEFAULT 0,  -- successful runs only
    current_state           TEXT    NOT NULL,   -- derived state per SPEC §1
    last_ipv4               TEXT,
    last_mac                TEXT,
    last_mac_vendor         TEXT,
    last_os                 TEXT,
    last_error_class        TEXT,
    PRIMARY KEY (inventory, fqdn)
);

-- Cached from vars/allowlist.csv on every ingest. Source of truth is the CSV.
CREATE TABLE allowlist (
    inventory  TEXT,                        -- NULL => applies to all inventories
    fqdn       TEXT    NOT NULL,
    reason     TEXT,
    owner      TEXT,
    added_at   TEXT    NOT NULL,
    ack_until  TEXT,
    PRIMARY KEY (inventory, fqdn)
);
-- The PRIMARY KEY relies on SQLite's NULL-allowed-in-PK behavior. Two rows
-- (NULL, 'x') and ('prod', 'x') coexist; two NULLs for the same fqdn fail at
-- the loader level (SPEC §8) before reaching the DB.

CREATE TABLE acks (
    ack_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory     TEXT,                     -- NULL => all inventories
    fqdn          TEXT    NOT NULL,
    signal_kind   TEXT    NOT NULL,         -- 'returned' | 'intermittent' | 'silent' | 'pending_remove'
    ack_until     TEXT    NOT NULL,         -- ISO date; row "active" iff ack_until >= today
    reason        TEXT    NOT NULL,
    added_at      TEXT    NOT NULL,
    added_by      TEXT
);
CREATE INDEX idx_acks_active ON acks(fqdn, signal_kind, ack_until);
-- No UNIQUE on (inventory, fqdn, signal_kind, active) — operators may layer
-- consecutive acks. The query for "active ack for X" picks MAX(ack_until).

CREATE TABLE actions (
    action_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    inventory        TEXT    NOT NULL,
    fqdn             TEXT,                  -- NULL for prune-csv actions
    action_kind      TEXT    NOT NULL,      -- recommend-remove | disable-host | delete-host | reenable-host | add-host | prune-csv
    mode             TEXT,                  -- 'disable' | 'delete' | NULL for non-act actions
    dry_run          INTEGER NOT NULL,
    executed         INTEGER NOT NULL DEFAULT 0,
    executed_at      TEXT,
    reason           TEXT    NOT NULL,
    triggering_runs  TEXT,                  -- JSON list of run_ids
    aap_host_id      INTEGER,
    aap_response     TEXT                   -- raw response body or error
);
CREATE INDEX idx_actions_kind ON actions(action_kind, executed);
```

### Why SQLite

- **Concurrent runs.** Two AAP jobs (e.g., one per inventory) can ingest at the
  same time. SQLite in WAL mode handles concurrent readers + one writer
  cleanly. JSONL would need hand-rolled locking; flat CSV state is the worst
  of both worlds.
- **Real queries.** "Hosts whose last `up` is > N days ago, across the last K
  successful runs, where no active ack matches" is one SQL statement, not a
  Python loop over a flat file.
- **Inspectable.** Operators can open the DB with `sqlite3` for ad-hoc forensics.
- **Single file.** Backups are `cp`. Disaster recovery is described in §5.

### Why not Postgres / the AAP DB

- Adds a hard dependency that doesn't exist in `identify-hosts`.
- Means a credential, a network path, a DBA conversation.
- Returns very little — this workload is read-heavy with one writer per
  inventory, well below SQLite's limits.
- AAP's own Postgres is reserved for AAP. Don't sit alongside.

If a future need pushes this past SQLite (multiple writers per inventory,
millions of observations, cross-region replication), the abstraction in
`lifecycle_db.py` is small enough to swap. That's a *later* problem.

---

## 4. Concurrency, idempotency, and crash recovery

### Concurrent ingest

Two ingest jobs targeting different inventories: safe, WAL handles it.

Two ingest jobs targeting the **same** inventory simultaneously: safe but
wasteful. The `csv_sha256` UNIQUE constraint means the second one's inserts
into `runs` fail; the playbook treats that as "already ingested, skip" and
moves on. No double-counted observations.

### Idempotency of `ingest.yml`

Running the same playbook against the same CSV directory twice produces
identical DB state. The dedup key is `csv_sha256`.

### Idempotency of `evaluate.yml`

`evaluate.yml` is read-only. Running it twice writes the reports twice
(overwriting) but never mutates the DB. The second run's reports are
byte-identical if no intervening ingest happened.

### Idempotency of `act.yml`

Re-running `act.yml` on the same `to_remove.csv` would try to disable the same
hosts. The implementation guards this two ways:
1. Before each AAP call, query the host's current `enabled` state; skip if
   already in the target state, record `action_kind=disable-host, executed=1,
   aap_response="already-disabled"`.
2. The `actions` table records what was attempted. Re-running consults it; a
   host already acted on for this `run_id` is skipped.

For `mode=delete`: the second run finds the host doesn't exist (`count=0` in
the lookup) — treat as "already gone," log as `already-deleted` in
`aap_response`, do not error.

### Idempotency of `ack.yml`

Inserting an ack for `(inv, fqdn, signal)` when an active one already exists
**extends** it (later `ack_until` wins) and appends to the reason. No
duplicate-row spam.

### Idempotency of `restore.yml`

Re-running against the same `actions.csv` is safe — the host is already
`enabled: true`, so the re-PATCH returns success without changes; the action
row is still appended (the audit story benefits from "user ran restore
twice").

### Crash recovery

A crash mid-ingest leaves the DB in a clean state because each CSV's ingest is
one transaction. Either the run row + all observations are present, or none
are. A crash mid-`act` may leave the AAP side partially updated — `actions.csv`
records what completed; the operator re-runs `act.yml` and the idempotency
check (above) finishes the rest.

---

## 5. Backup and disaster recovery

The DB is critical state. Operational recommendations:

- **Backups.** A nightly `sqlite3 state.sqlite ".backup state.sqlite.bak"` is
  enough. The DB is small (low MB per year, even at 50k hosts at daily cadence).
  Rotate `keep_db_backups` copies (default 7).
- **Schema version.** `PRAGMA user_version` is the source of truth.
  `lifecycle_db.py` checks on open. Mismatches refuse to proceed — never
  silent-migrate.
- **Bootstrap from CSVs.** If the DB is lost and there are no backups, point
  `ingest.yml` at the full historical CSV directory. It re-ingests in
  filename-sort order, the derived `host_state` is recomputed from scratch.
  The things lost are: ack history (acks were in the DB, nowhere else) and
  action history (same).
- **Action history is the irreplaceable bit.** If you've been auto-disabling
  hosts, the audit trail of "we disabled host X at time Y because of runs
  [a, b, c]" is in `actions` and nowhere else. Treat backups accordingly.
- **Re-bootstrapping acks.** No mechanism — operators have to re-issue them.
  The allowlist re-bootstraps trivially because the file is git-tracked.

---

## 6. Known limits

These are real, not exhaustive. None are blockers; each has a workaround.

### 6.1 Identity is `(inventory, fqdn)`, not "the host"

A renamed or re-imaged box looks like "old host went silent, new host
appeared." We capture MAC vendor and IPv4 changes as *hints* on the `returned`
report, but the identity key stays FQDN-based because that's what AAP keys on
too. Cross-FQDN host identity is out of scope.

### 6.2 We can only see what `identify-hosts` saw

A host that was permanently removed from the AAP inventory **before**
`identify-hosts` ever scanned it is invisible here. Likewise, "removed from
inventory and returned" is really "absent from scan and reappeared in scan."
The [SPEC.md §3.2 returning rule](SPEC.md#32-returning-rule) is honest about
this.

A future enhancement could ingest the AAP inventory list separately via the
REST API and correlate; for now, that's deliberately out of scope. The cost is
one more credential and one more failure mode, for marginal accuracy gain.

(Note: `add-hosts.yml` does query AAP at add-time to check for existence and
print prior lifecycle history — see [SPEC §5.8](SPEC.md#58-add-hostsyml). That's
a per-host point-in-time check, not the continuous correlation hinted at above.)

### 6.7 Inventory renames are operator-driven, not auto-detected

AAP renames are a manual action; this toolkit doesn't try to guess them from
data ("all hosts disappeared from X at the same moment they appeared in Y"
would be ambiguous with a real mass migration). The operator must run
[`rename-inventory.yml`](SPEC.md#56-rename-inventoryyml) after renaming in
AAP. If they forget: the next evaluate fires sanity gate G2 on the old name
and the new name shows up as all-new hosts. The recovery is to run the
rename playbook retroactively — the historical observations still carry the
old inventory name and get migrated wholesale.

### 6.3 `online_unmanaged` hosts have no derived "silence"

A Windows host that nmap sees every day stays at `current_state=active` even
though we never get facts from it. That's intentional — it's not a candidate
for SSH-inventory removal. If you want lifecycle for WinRM-managed hosts, this
toolkit is the right shape but needs a sibling discovery feed; the column set
isn't quite the same.

### 6.4 Calendar-day thresholds, not run-count thresholds

If discovery stops running for 40 days, every host's "days since last seen up"
goes past `silent_threshold_days`. The G2/G3 sanity gates catch this (the
percentage will blow past the cap), but the failure mode is "evaluator
correctly refuses to do anything until discovery resumes." That's the right
behavior. Operators should monitor `identify-hosts` runs as a separate concern.

### 6.5 The DB is one file on one host

If `mng_host` dies hard and there are no backups, you lose ack and action
history and have to bootstrap from CSVs (§5). This is the same single-host
risk profile as the `identify-hosts` CSV share itself; if that risk is
unacceptable, the right answer is to back up the share, not to add HA to this
toolkit.

### 6.6 Delete is single-direction

`mode=delete` removes the AAP host record. `restore.yml` cannot re-create it
because we don't archive the full host record (group memberships, host_vars,
labels, etc.). Practically: if you delete and need it back, do it from the
AAP UI or `awx.awx` with the original config. This is why disable is the
default.

---

## 7. Failure modes and what they look like

| Symptom | Likely cause | Where it surfaces | What to do |
|---|---|---|---|
| All hosts move to `down` in one run | DNS/firewall change blocked scanner, or `identify-hosts` itself broke | G1 fails: `aborted.md` with low `scanned_hosts/total_hosts` | check the `identify-hosts` job; re-run; do not act |
| `to_remove.csv` has 200 hosts when normal is 5 | percentage cap was wrong, or a real cleanup is overdue | G2 fails: `aborted.md`; investigate manually | review by hand; either temporarily raise the cap with `-e` or split into multiple runs |
| Same host shows up in `returned.csv` every week | rename loop, dynamic IP, or true flapping | `intermittent.csv` should also flag it | ack for 30 days while you investigate; add to allowlist if it's expected |
| `ingest.yml` exits 2 | a CSV had parse errors | log will name the file; ingest skipped it | inspect; re-run identify-hosts if needed |
| `act.yml` exits 3 | one or more AAP API calls failed | `actions.csv` `aap_response` column has the error per host | check AAP token scope / host IDs; re-run (idempotent) |
| `act.yml` refuses to start | the most recent `evaluate.yml` produced `aborted.md` | clear error message pointing at the abort file | run a fresh `ingest`+`evaluate`; if still aborted, fix the data, not the toolkit |
| Ack table grows large | normal use; acks aren't deleted | `summary.md` shows count of inactive acks per inventory | manual `DELETE FROM acks WHERE ack_until < date('now','-1 year')` is fine |
| DB open fails with version mismatch | someone bumped schema_version | playbook exits 4 with a clear message | run the migration script (not in scope here; lives next to the schema in `lifecycle_db.py`) |
| `restore.yml` warns about delete-host rows | operator used `mode=delete` and now wants them back | log/warning listing FQDNs | re-add manually (SPEC §6.4) |

---

## 8. Why this shape, not the alternatives

A short ledger of choices and the rejected alternatives, so future-you knows
which arguments to re-litigate and which are settled.

| Choice | Rejected alternative | Reason |
|---|---|---|
| Ansible + Python core | Pure Python CLI scheduled by cron | AAP-native scheduling, RBAC, surveys, notifications come "for free"; matches `identify-hosts` |
| SQLite on `mng_host` | JSONL files, CSV append, Postgres | SQLite hits the sweet spot for one-writer-per-inventory + real queries + zero infra |
| Disable via AAP API as default, delete as opt-in | Editing static inventory files; defaulting to delete | Disable is reversible; delete is not. Static files often aren't where AAP actually reads from. |
| Calendar-day thresholds | Run-count thresholds | Survives cron schedule changes; tied to the data, not the harness |
| `online_unmanaged` is its own state | Treat as `up` or as `down` | It's neither; if we treat it as `up` we miss real Windows-inventory drift, if we treat it as `down` we recommend removing functioning hosts |
| Allowlist as git-tracked CSV | YAML, or DB-table-only | CSV is spreadsheet-editable, simple to diff, simple to peer-review. DB caches but doesn't own. |
| Acks as DB table with operator playbook | Acks as a file under git | Acks are dynamic, short-lived, audit-rich; PRs are the wrong UX. Allowlist's PR review IS the right UX for permanent exemptions; acks need to be a single command. |
| `fqdn` as allowlist primary key; `inventory` optional | Composite `(fqdn, inventory)` required | The same fqdn often exists in multiple inventories with the same operational meaning; forcing a row per inventory was friction without clear benefit. |
| Sanity gates as hard aborts, not warnings | Render reports + emit warnings | A bad input run poisons every category, not just removals; partial signal is worse than none |
| stdlib only | `pandas`, `sqlalchemy`, `requests` | Air-gap safe; no EE rebuild needed; the workloads here don't justify the dependencies |
| Disable via direct REST `urllib` | `awx.awx.host` module | No collection dependency on the EE; trivially testable with a mock HTTP server; module call documented as an alternative |
| Operator-driven rename/retire | Auto-detect from data | A "rename" indistinguishable from a real mass migration; auto-detection would be too eager. Manual playbook with `apply=false` preview is safer. |
| `add-hosts.yml` prints history but proceeds anyway | Block adds with prior history | The whole point is to surface "we removed this before, why is it back?" *to the operator*; blocking would make the script harder to use for the legitimate "yes I know, re-add it" case. The audit row is in `actions` regardless. |
| Per-inventory CSV is primary, cross-inventory rollup is secondary | One global CSV with `inventories` column | Action target in AAP is per-`(inventory, host_id)`; `act.yml` consuming per-inventory files is the natural shape. The rollup at `_all/<date>/` adds the "one row per unique fqdn" view for operators searching across inventories. |
| All report CSVs sorted by fqdn | Sorted by recency / severity | Deterministic diffs across runs; faster human lookup. Severity is in the file name already (`to_remove` vs `intermittent`). |
| `already_removed_from` rendered as semicolon-joined `inv:kind:date` | Full JSON in the cell | CSV compatibility; full detail is one JOIN to `actions` away. |

---

## 9. Future work (not in v1)

Captured here so v1 doesn't grow them by accident.

- **AAP inventory state ingestion.** Pull the host list from AAP's REST API to
  distinguish "absent from scan" from "removed from inventory." Cleanest if
  added as a separate `vars/inventories.yml` knob (`fetch_aap_inventory: true`).
  Today's partial answer: `add-hosts.yml` does a point-in-time AAP lookup
  per fqdn at add-time; continuous correlation across the whole inventory
  would catch things this doesn't.
- **External-source inventory feed.** A `playbooks/sync-from-source.yml` that
  pulls hosts from a CMDB / ServiceNow / vendor spreadsheet, diffs against the
  current AAP inventory, and uses `add_hosts.py` to reconcile. This is the
  natural extension of `add-hosts.yml`; deliberately split off so v1 doesn't
  grow a CMDB integration.
- **Per-team report routing.** Today there's one summary per inventory; you
  could split by owner using the allowlist's `owner` field as a tagging
  surface. Probably better solved with whatever downstream tool already routes
  by team.
- **Webhook/email notifier.** Today the playbook writes files. Email is
  trivial to add (`smtplib` is stdlib); webhooks need a config schema. Both
  are better added once we know what consumers exist. For now the
  recommendation is to use AAP's own notification templates against the
  `evaluate` job result.
- **Multi-day rollups.** A `weekly-summary.md` aggregating the last 7 runs.
  Useful for stakeholder communication; trivial once the per-run summary works.
- **Delete-with-archive.** If `mode=delete` archived the full AAP host record
  to JSON before deleting, `restore.yml` could re-create it. Adds complexity
  for a path the SPEC discourages in the first place.
