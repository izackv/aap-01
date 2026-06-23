# Inventory lifecycle — functional specification

This is the **contract** the code must satisfy. The README describes intent
and operator-facing behavior; this document is for the person writing or
reviewing the implementation. If README and SPEC disagree, SPEC wins; please
fix the README in the same PR.

Companion documents:

- [README.md](README.md) — purpose, install, tunables, usage examples
- [ARCHITECTURE.md](ARCHITECTURE.md) — components, runtime topology, DB schema, failure modes

---

## 1. State model

Every host observed in a single `identify-hosts` CSV row is classified into
**exactly one** of these *observation states*. The classifier is in
`files/classify_state.py`. Order matters — the first rule that matches wins.

| State | Condition (on the CSV row) | Meaning |
|---|---|---|
| `up` | `reachable=true` AND `ssh_open=true` AND `error_class` is empty AND `detection_method == "ssh-facts"` | host answered SSH and gave us facts |
| `auth_failed` | `ssh_open=true` AND `error_class == "auth-failed"` | host is alive, SSH is open, but our credential didn't work |
| `ssh_blocked` | `ssh_open=true` AND `error_class` in {`timeout`, `network-unreachable`, `unreachable`, `module-failure`, `python-version`, `python-deps`, `python-missing`, `locale`, `privilege-escalation`, `error`} | SSH is open from nmap's view but Phase 2 couldn't complete; *don't* treat as dead |
| `online_unmanaged` | `reachable=true` AND `ssh_open=false` | host is up but not SSH-manageable (Windows, switch, NAS); never counts toward "silent" |
| `down` | `reachable=false` | host did not respond to the scan |
| `absent` | host is in the previous `host_state` table but does **not** appear in the current CSV at all | scan didn't see it; treated the same as `down` for lifecycle math |
| `excluded` | `(inventory, fqdn)` is in the allowlist | observed and counted but exempted from all flagging |

A **derived per-host state** (`host_state.current_state`) is recomputed at the
end of each ingest from the observation history:

| Derived state | Condition |
|---|---|
| `active` | last observation was `up` AND `consecutive_misses < min_successful_runs_before_action` |
| `silent` | `last_seen_up` is older than `silent_threshold_days` AND host is not `excluded` |
| `returning` | previous derived state was `silent` (or row had been `absent` ≥ `returned_threshold_days`) AND current observation is `up` |
| `intermittent` | within `intermittent_window_days`, the host's observations show ≥ `intermittent_min_flips` transitions between `up` and {`down`, `absent`} |
| `pending_remove` | `silent` for at least `min_successful_runs_before_action` *successful* runs in a row |
| `protected` | host is in allowlist (`excluded` observation state) |

Multiple derived states can apply in principle, but for output purposes the
**priority order** is: `protected` > `pending_remove` > `silent` > `returning` >
`intermittent` > `active`. Each host appears in at most one of the categorised
reports.

### Why `online_unmanaged` matters

If your inventory is SSH-managed (the default assumption for AAP-on-Linux), a
Windows host that nmap sees with port 5985 open is **not** a candidate for
removal — it's a candidate for "this host belongs in a WinRM inventory, not
this one." The `to_remove.csv` report deliberately **excludes**
`online_unmanaged` hosts. They are still tracked and counted in the summary,
just not removed.

### Multi-inventory: same fqdn, different inventories

State is keyed by `(inventory, fqdn)`. The same fqdn appearing in two
inventories produces **two independent `host_state` rows**; they age and flag
independently. A host can be `active` in `prod-linux` and `silent` in
`dr-linux` simultaneously — that's a real signal ("DR copy isn't being
exercised") and the reports treat it as such.

Operator visibility for this case is handled in the report schemas: both
`to_remove.csv` and `returned.csv` include an `also_active_in` column listing
any *other* inventories where the same fqdn is currently `active`. So when
`prod-linux/to_remove.csv` flags `host-x`, the operator sees in the same row
"also active in: `dr-linux`" — no cross-grepping required.

How specific scenarios surface:

| Scenario | What this toolkit observes | What the operator sees |
|---|---|---|
| fqdn moved test → prod | new `host_state(prod, fqdn)` created; existing `host_state(test, fqdn)` ages to absent → silent | `test/to_remove.csv` eventually flags it with `also_active_in=[prod]` — operator confirms "yes, removed from test on purpose" |
| fqdn added to test (still in prod) | new `host_state(test, fqdn)`; `host_state(prod, fqdn)` unchanged | Both inventories see it as active; nothing to do |
| fqdn removed from test only (still in prod) | `host_state(test, fqdn)` ages out | `test/to_remove.csv` flags with `also_active_in=[prod]`; operator confirms partial removal |
| inventory renamed in AAP | every host appears "new" under the new name; every host in the old name ages out | sanity gate G2 typically fires on the old name — the *correct* signal. Use [§5.6 `rename-inventory.yml`](#56-rename-inventoryyml) to migrate state cleanly. |
| inventory retired in AAP | scans stop arriving; existing `host_state` rows all age to silent in lockstep | sanity gate G2 fires. Use [§5.7 `retire-inventory.yml`](#57-retire-inventoryyml) to stop evaluating that inventory while keeping its history for audit. |

---

## 2. Input format (the contract)

Source: any file matching `csv_glob` (default
`/myshare/data/<inventory>_*_servers_info.csv`).

**Required CSV columns** (subset of [`identify-hosts` CSV columns](../identify-hosts/README.md#csv-columns)):
`fqdn, ipv4, reachable, ssh_open, detection_method, last_checked, error_class`.

Optional but used when present: `mac, mac_vendor, os, os_version, open_ports`.

The ingest parser:

- Treats `reachable` and `ssh_open` as case-insensitive booleans
  (`true`/`false`/`1`/`0`/`yes`/`no`); anything else raises an error.
- Parses `last_checked` as ISO-8601 (with or without `Z`). If unparseable, the
  row is dropped and counted in `runs.dropped_rows`.
- Strips whitespace and lowercases `fqdn` for storage.
- Reads `inventory` from the **filename prefix** (the leading token before the
  first `_`). If the filename does not match `<inventory>_<date>_<jobid>_servers_info.csv`,
  the ingest of that file fails fast with a clear error (the operator can pass
  `-e inventory=<name>` to override).
- Computes `csv_sha256` of the file and skips ingest entirely if a row in
  `runs` already has that hash — re-running ingest is a no-op.

**The contract is the column set, not the full schema.** Adding columns to
`identify-hosts` CSVs is safe; removing or renaming any required column is a
breaking change requiring a coordinated update here.

---

## 3. Rules and thresholds

All thresholds are calendar days against `last_checked`, never run counts. Run
counts are used only for the "successful runs" gate (§3.4).

### 3.1 `silent` rule

A host enters `silent` when:

- `last_seen_up < now() - silent_threshold_days`, AND
- the host is not in the allowlist.

`last_seen_up` is the most recent observation timestamp where the observation
state was `up`. `auth_failed`, `ssh_blocked`, and `online_unmanaged` **do
not** count as "seen up" — the host might be alive but it isn't proven
inventory-healthy.

### 3.2 `returning` rule

A host raises a `returning` alert when **all** of:

- previous `host_state.current_state` was `silent` OR `pending_remove`, OR the
  host had no `up` observation in the prior `returned_threshold_days`;
- the current observation is `up`;
- the host is not in the allowlist (allowlisted returns are expected);
- there is no active ack with `signal_kind=returned` for `(inventory, fqdn)` —
  see [§7](#7-acks-per-host-signal-suppression).

The alert row records "what changed" by diffing against the most recent prior
`up` observation:

- `mac_changed` — boolean
- `ipv4_changed` — boolean
- `mac_vendor_changed` — boolean
- `os_changed` — boolean

A return where everything matches the prior `up` snapshot is *less* interesting
than one where the MAC vendor changed — operators should treat the latter as
"possible host replacement, name reused."

### 3.3 `intermittent` rule

A host is flagged `intermittent` when, over the trailing `intermittent_window_days`,
its sequence of observation states contains ≥ `intermittent_min_flips`
transitions between `up` and any of {`down`, `absent`}. Transitions in and out
of `auth_failed`/`ssh_blocked`/`online_unmanaged` do **not** count — those are
"unclassified," not flapping. An active ack with `signal_kind=intermittent`
also filters the row out of the report.

Intermittent hosts are surfaced for human review (probably belong in the
allowlist). They are explicitly **not** included in `to_remove.csv`.

### 3.4 `pending_remove` rule and sanity gates

A host is recommended for removal when:

1. it is `silent` (rule 3.1), AND
2. the most recent `min_successful_runs_before_action` runs were all
   *successful* (see §3.5), AND
3. the host's `current_state` was `silent` in all of those successful runs
   (no transient `up` in between resets the counter), AND
4. the host is not in the allowlist, AND
5. there is no active ack with `signal_kind in {silent, pending_remove}` for
   the host.

Before producing `to_remove.csv`, evaluate these **sanity gates** in order:

| Gate | Condition | Action if violated |
|---|---|---|
| G1: latest run is successful | the run just ingested has `is_successful_run=1` | write `aborted.md`, stop |
| G2: % cap | `len(candidates) <= max_remove_per_run_pct% of total_active_hosts` | write `aborted.md`, stop |
| G3: absolute cap | `len(candidates) <= max_remove_per_run_abs` | write `aborted.md`, stop |

`aborted.md` records which gate failed and the counts, so the operator can tell
"DNS resolver was down" from "we genuinely have 200 stale hosts to clean up."
No further reports are written when a gate fails — neither `to_remove.csv` nor
`returned.csv`, because the underlying data is suspect.

> Rationale: if the latest run was bad (G1 failed), the rule outputs for
> *every* category are unreliable, not just removals. The operator's path is
> "investigate the bad run, re-ingest if needed, re-evaluate" — not "act on
> partial signal."

### 3.5 What makes a run "successful"

A run is `is_successful_run=1` iff:

- the CSV was parseable (no fatal errors during ingest), AND
- `scanned_hosts / total_hosts >= min_run_success_pct / 100`.

`scanned_hosts` counts observations where the host had **any** non-`absent`
state. `total_hosts` is the count of distinct `(inventory, fqdn)` known from
this run *plus* any host in `host_state` for the same inventory not present in
this CSV (those count as `absent`, so they reduce `scanned_hosts`).

A run with `is_successful_run=0` is still recorded in the DB and its
observations are still ingested — they just don't count toward the
"successful runs in a row" requirement for `pending_remove`.

---

## 4. Output formats

All written under `{{ report_dir }}/<inventory>/<UTC-date>/`. UTF-8, LF line
endings, header row in all CSVs. `evaluate.yml` writes everything except
`actions.csv`; `act.yml` writes `actions.csv` and updates the DB.

### 4.1 `summary.md`

```markdown
# Inventory lifecycle — <inventory> — <UTC-date>

Run ID: <run_id>          (CSV: <csv_path>)
Total hosts known:        <N>
Observed this run:        <N>
Success threshold:        <pct>%  → run is_successful: <yes|no>

## Counts by derived state

| State          | Count |
|----------------|-------|
| active         | …     |
| silent         | …     |
| pending_remove | …     |
| returning      | …     |
| intermittent   | …     |
| protected      | …     |

## Sanity gates

- G1 latest run successful: <pass|FAIL>
- G2 percent cap (<pct>%):  <pass|FAIL>  (<n>/<total>)
- G3 absolute cap (<abs>):  <pass|FAIL>  (<n>)

## Active acks (this inventory)

| fqdn | signal | ack_until | reason | added_by |
|------|--------|-----------|--------|----------|
| …    | …      | …         | …      | …        |

## Files this run

- [to_remove.csv](to_remove.csv) — <n> hosts
- [returned.csv](returned.csv)   — <n> hosts
- [intermittent.csv](intermittent.csv) — <n> hosts
- [actions.csv](actions.csv)     — present only after act.yml runs
- [aborted.md](aborted.md)       — present only when any gate failed
```

### 4.2 `to_remove.csv`

`fqdn, inventory, last_seen_up, days_silent, last_ipv4, last_mac, last_mac_vendor, last_os, last_error_class, also_active_in, already_removed_from, last_run_ids`

**Sort order**: ascending by `fqdn`, then by `inventory`. Deterministic so
diffs across runs are stable.

`also_active_in` is a JSON list of *other* inventories where this fqdn's
current `host_state.current_state` is `active`. Empty list `[]` means the host
is unique to this inventory (the common case). A non-empty list means the
operator should check whether this is an intentional partial removal or a
mistake (see [§1 multi-inventory](#multi-inventory-same-fqdn-different-inventories)).

`already_removed_from` is a JSON list of objects describing prior executed
removals of the *same fqdn* in *other* inventories — one object per executed
`disable-host` or `delete-host` action in the `actions` table:

```json
[{"inventory":"inventoryB","kind":"delete","at":"2026-04-22","run_id":63}]
```

For CSV legibility the list is rendered compactly:
`inventoryB:delete:2026-04-22;qa-linux:disable:2026-05-10`. Empty `[]` is the
common case.

This is the audit hook for the scenario "A scanned rarely, host removed from B
months ago, now A's run finally flags it" — the row shows `already_removed_from
= inventoryB:delete:2026-04-22` so the operator can confirm the prior decision
rather than re-litigate it. Full detail is always available by joining to the
`actions` table on `fqdn`.

`last_run_ids` is a JSON list of the `run_id` values for the runs that
contributed to the silent streak — kept so the operator can pull the original
evidence CSVs.

### 4.3 `returned.csv`

`fqdn, inventory, days_absent, prior_last_seen_up, current_seen, prior_ipv4, current_ipv4, ipv4_changed, prior_mac, current_mac, mac_changed, prior_mac_vendor, current_mac_vendor, mac_vendor_changed, prior_os, current_os, os_changed, also_active_in, already_removed_from`

**Sort order**: ascending by `fqdn`, then by `inventory`.

`also_active_in` and `already_removed_from` have the same semantics as in
`to_remove.csv`. They are particularly informative on a `returned` row —
"this host returned here but is currently active over there" often means a
temporary cross-inventory move, not a real return; "returned here but we
deleted it from another inventory last month" is a much stronger signal that
something is being re-created against expectations.

### 4.4 `intermittent.csv`

`fqdn, inventory, window_days, flips, last_seen_up, last_seen_down, current_state, observation_sequence`

**Sort order**: ascending by `fqdn`, then by `inventory`.

`observation_sequence` is a compact string like `U D U D U D U D` (timestamps
elided) so an operator can eyeball the pattern.

### 4.5 `actions.csv`

`action_id, action_kind, fqdn, inventory, executed, executed_at, dry_run, mode, reason, aap_host_id, aap_response`

`action_kind` values: `recommend-remove`, `disable-host`, `delete-host`,
`reenable-host`, `add-host`, `prune-csv`.

`mode` values: `disable` (default), `delete` (only when act.yml is invoked with
`mode=delete`). Empty for non-act actions (`add-host`, `reenable-host`,
`prune-csv`).

### 4.6 `aborted.md`

```markdown
# Run aborted — <inventory> — <UTC-date>

Run ID: <run_id>

## Failed gate

<G1 | G2 | G3>: <why>

## Counts

Candidates this run: <n>
Active hosts: <n>
Threshold: <max_remove_per_run_pct>% / <max_remove_per_run_abs> abs

## Next step

Investigate the source CSV: <path>
Re-run identify-hosts if the data is suspect.
Re-run evaluate.yml after the next successful ingest.

No reports were written for this run.
```

### 4.7 Cross-inventory rollup (`_all/<date>/`)

In addition to the per-inventory report tree, `evaluate.yml` writes a
**cross-inventory rollup** at `{{ report_dir }}/_all/<UTC-date>/`. The leading
underscore keeps it from colliding with any real inventory name and sorts it
to the top of listings.

The rollup answers: "across all inventories, which unique hosts are flagged
right now, and what's the full multi-inventory picture for each one?" Per-inventory
files remain the primary product because `act.yml` consumes them one
inventory at a time (action is per-`(inventory, host_id)`); the rollup is
informational — single line per unique fqdn.

| File | Contents |
|---|---|
| `_all/<date>/to_remove.csv` | one line per unique fqdn flagged in *any* inventory this run |
| `_all/<date>/returned.csv` | one line per unique fqdn returning in any inventory |
| `_all/<date>/intermittent.csv` | one line per unique fqdn flagged intermittent in any inventory |
| `_all/<date>/summary.md` | global rollup: total unique flagged fqdns, cross-inventory hotspots |

**Schema of `_all/<date>/to_remove.csv`:**

`fqdn, inventories_to_remove, also_active_in, already_removed_from, oldest_last_seen_up, max_days_silent`

| Column | Meaning |
|---|---|
| `fqdn` | the host (sort key) |
| `inventories_to_remove` | JSON list of inventories where this fqdn is in `to_remove` this run |
| `also_active_in` | JSON list of inventories where this fqdn is currently `active` |
| `already_removed_from` | semicolon-joined `inventory:kind:date` records of executed prior removals |
| `oldest_last_seen_up` | earliest `last_seen_up` across all inventories where this fqdn is flagged |
| `max_days_silent` | longest silent streak across those inventories |

**Sort order**: ascending by `fqdn`. The rollup is the file to grep when you
want everything about one host on one line.

The rollup is written **only when at least one per-inventory `to_remove.csv`
was written this run** (a fully gated/aborted run produces no per-inventory
removal CSV and therefore no rollup row). If every inventory aborted, the
rollup directory has only `summary.md` noting that.

---

## 5. Playbooks — required behavior

### 5.1 `ingest.yml`

Variables consumed: `csv_glob`, `db_path`, `inventory` (optional override),
`allowlist_path` (default `vars/allowlist.csv`).

Steps:
1. Read `vars/allowlist.csv` (see §8), normalise to a set of allowlist entries.
   Cache into the `allowlist` table (delete-then-insert; the file is authoritative).
2. Glob `csv_glob` on `mng_host`. For each file:
   1. Compute SHA-256. Skip if `runs.csv_sha256` already present.
   2. Parse filename for `(inventory, scan_date, aap_job_id)`. If `inventory`
      is passed as an override, use that.
   3. Stream-parse the CSV (don't load all rows into memory; some inventories
      are 50k+ rows).
   4. Classify each row → observation state.
   5. Compute `is_successful_run` per §3.5.
   6. In a single transaction: insert into `runs`, insert observations,
      recompute the affected `host_state` rows.
3. Expire any rows in `acks` whose `ack_until < today` (mark inactive; do not
   delete — keep audit history).
4. Print a one-line summary per CSV: `ingested <path> rows=<n> success=<yes|no>`.

Exit code: 0 on success, 2 if any file failed to parse (others may still have
been ingested — the log will tell the operator which).

### 5.2 `evaluate.yml`

Variables consumed: `db_path`, `report_dir`, `inventory` (optional, filter to
one), all thresholds from `vars/main.yml`.

Steps:
1. Read tunables. Compute the trailing windows.
2. For each `active` inventory (skip `retired` / `renamed` rows in `inventories`):
   1. Compute `pending_remove` candidates (rule 3.4), filtered by active acks.
   2. Compute `returning` candidates (rule 3.2), filtered by active acks.
   3. Compute `intermittent` candidates (rule 3.3), filtered by active acks.
   4. For each candidate, compute `also_active_in` (other inventories where
      `host_state.current_state='active'` for this fqdn) and
      `already_removed_from` (executed `disable-host`/`delete-host` `actions`
      rows for this fqdn across other inventories), per [§4.2](#42-to_removecsv).
   5. Apply sanity gates (3.4). If any fail, render only `summary.md` and
      `aborted.md`. Do not render the three category CSVs.
   6. Otherwise render all four files, **sorted by fqdn**.
3. Render the cross-inventory rollup at `_all/<date>/` per [§4.7](#47-cross-inventory-rollup-_alldate),
   merging across all inventories that produced category CSVs this run.
4. Never mutate the DB. `evaluate.yml` is **read-only.**

Exit code: 0 always (a gated abort is not an evaluator failure; the operator
discovers it via the report tree).

### 5.3 `act.yml`

Variables consumed: `db_path`, `report_dir`, `apply` (bool, default `false`),
`mode` (`disable`|`delete`, default `disable`), `aap_url`, `aap_token`,
`keep_csv_days`, `csv_glob`.

Steps:
1. Refuse to run if the most recent `evaluate.yml` for the target inventory
   produced an `aborted.md` (fail-closed).
2. **Extra gate for `mode=delete`**: refuse unless `apply=true` AND
   `len(candidates) <= max_remove_per_run_abs`. The percentage cap (G2) still
   applies. Delete is single-direction — guard it harder.
3. Read the most recent `to_remove.csv`. For each row:
   - Resolve the AAP host ID (see §6.2).
   - If `apply=false`: write a `dry-run` action row, do not call AAP.
   - If `apply=true, mode=disable`: PATCH `/api/v2/hosts/<id>/` with
     `{"enabled": false}`. Record the response.
   - If `apply=true, mode=delete`: DELETE `/api/v2/hosts/<id>/`. Record the response.
   - Insert into `actions` table; record in `actions.csv`.
4. Prune `identify-hosts` CSVs older than `keep_csv_days`, **except** any CSV
   whose `run_id` is referenced by an action row (executed or recommended).
   Those are evidence and kept indefinitely. Pruning is recorded as
   `action_kind=prune-csv` rows.

Exit code: 0 on success, 3 if AAP API returned errors for any host (rest were
still attempted; see `actions.csv` for details).

### 5.4 `ack.yml`

Variables consumed: `db_path`, `fqdn` (required), `signal` (required: one of
`returned`, `intermittent`, `silent`, `pending_remove`), `days` (required, int),
`reason` (required), `inventory` (optional — empty means all inventories),
`added_by` (optional — defaults to `$USER` or `$ANSIBLE_USER`),
`list` (bool, default `false`), `remove` (bool, default `false`).

Steps:
1. If `list=true`: print the active acks (optionally filtered by `inventory`)
   and exit. No DB mutation.
2. If `remove=true`: locate the matching ack row (by `inventory_or_null`, `fqdn`,
   `signal`) and set `ack_until = today - 1 day` plus an audit note. Exit.
3. Else (insert/extend mode): validate inputs (`signal` in allowed set, `days >
   0`). Compute `ack_until = today + days`.
4. Insert into `acks`. If an active ack with the same
   `(inventory_or_null, fqdn, signal_kind)` already exists, **extend** its
   `ack_until` (whichever is later wins); append the new reason to the existing
   row's `reason`.
5. Print one line: `acked <inv|*>/<fqdn> signal=<signal> until=<date>`.

Exit code: 0 on success, 5 on invalid inputs.

### 5.5 `restore.yml`

Variables consumed: `db_path`, `aap_url`, `aap_token`, `from_actions_csv`
(path), `apply` (bool, default `false`).

Steps:
1. Read `from_actions_csv`. For each row with `action_kind=disable-host` and
   `executed=1`:
   - Resolve the AAP host ID (look up by fqdn within `inventory`).
   - If `apply=false`: print "would re-enable <fqdn>".
   - If `apply=true`: PATCH `/api/v2/hosts/<id>/` with `{"enabled": true}`.
     Insert a `reenable-host` action row referencing the original action_id.
2. Rows with `action_kind=delete-host` are **not** auto-restored. Print a
   warning listing them — the operator must re-add via `awx.awx.host` or the
   UI (we don't keep the full host record needed to reconstitute it).

Exit code: 0 on success, 3 if any AAP call failed.

### 5.6 `rename-inventory.yml`

Variables consumed: `db_path`, `from` (required), `to` (required), `apply`
(bool, default `false`).

Reflects an AAP inventory rename in the lifecycle DB so historical state
follows the new name. Without this, a rename causes every host in the old
inventory to age into `silent` and `aborted.md` to fire on the next evaluate.

Steps:
1. Refuse if `from == to`, or if `from` is not present in the `inventories`
   table, or if `to` already has an `active` row.
2. Print the counts that will move: `host_state`, `observations`, `runs`,
   `actions`, `acks`, `allowlist` rows where `inventory = <from>`.
3. If `apply=false`: stop. (Default — confirm before mutating.)
4. If `apply=true`: in one transaction, UPDATE every table to set
   `inventory = <to>` where it was `<from>`. Insert/update the `inventories`
   row to `status='active', renamed_from=<from>` (the `<from>` row is marked
   `status='renamed'` for audit; not deleted).

Exit code: 0 on success, 4 on DB errors.

> Note: this changes only the lifecycle DB. The actual AAP rename is a
> separate operator action in the AAP UI or via the AAP API.

### 5.7 `retire-inventory.yml`

Variables consumed: `db_path`, `inventory` (required), `apply` (bool, default
`false`).

Marks an inventory as no longer evaluated. Historical data is kept for audit
but `evaluate.yml` skips it (no reports, no rule processing). Future ingests
that name this inventory are an error — once retired, an inventory that
reappears in a CSV usually means a rename was missed or a name was re-used.

Steps:
1. Refuse if `inventory` is not in the `inventories` table, or already
   `status='retired'`.
2. Summarize what will be skipped from now on: host counts by `current_state`.
3. If `apply=false`: stop.
4. If `apply=true`: set `inventories.status='retired', retired_at=now()`. No
   row deletions.

Exit code: 0 on success, 4 on DB errors.

### 5.8 `add-hosts.yml`

Variables consumed: `db_path`, `inventory` (required), `aap_url`, `aap_token`,
one of `fqdn` (single) or `from_file` (path), `apply` (bool, default `false`),
`reenable` (bool, default `true`).

Adds one or more hosts to an AAP inventory. **Always prints any prior
lifecycle history for the fqdn before adding** — that's the audit hook the
operator uses to spot "this host was removed last month, why are we recreating
it?"

Steps:
1. Validate exactly one of `fqdn` / `from_file` is provided. Parse `from_file`
   (one fqdn per line; blank lines and `#` comments skipped).
2. Resolve the inventory ID once (`aap_client.inventory_id_by_name`).
3. For each fqdn (lowercased on read):
   1. Query `host_state` for prior history of `(inventory, fqdn)` AND
      `(any inventory, fqdn)`. Collect `actions` rows referencing it.
   2. Check whether the host already exists in the target AAP inventory.
   3. Decide an outcome:
      - **exists, enabled=true** → `SKIP` (idempotent no-op).
      - **exists, enabled=false** and `reenable=true` → `REENABLE`
        (PATCH `enabled: true`).
      - **exists, enabled=false** and `reenable=false` → `SKIP` with warning.
      - **does not exist** → `ADD` (POST to
        `/api/v2/inventories/<inv_id>/hosts/` with `{"name": "<fqdn>", "variables": "---\n"}`).
   4. If `apply=false`: print the decision and the prior-history summary, no
      AAP call.
   5. If `apply=true`: execute, insert `actions` row (`action_kind=add-host`
      or `reenable-host`), record AAP response.
4. Print a per-host status line (format in [README §"Adding hosts back"](README.md#adding-hosts-back-re-add-script)).

Exit code: 0 on success, 3 if any AAP call failed.

> Note: prior history is **informational, not blocking**. The script always
> attempts the add (unless skipping). This matches the documented use case —
> creating a host requested by another team while surfacing "but we removed it
> in March" so the operator can ask why.

---

## 6. Removal mechanics

This section is the worked example the operator/implementer needs to confirm
how removal *actually* hits AAP. The `act.yml` and `restore.yml` playbooks
implement these calls; `files/aap_client.py` is the thin stdlib wrapper.

### 6.1 The two modes

| Mode | HTTP | What it does in AAP | Reversal |
|---|---|---|---|
| `disable` (default) | `PATCH /api/v2/hosts/<id>/` body `{"enabled": false}` | host stays in inventory; AAP excludes it from job runs | `PATCH … {"enabled": true}` — one call, fully reversible |
| `delete` (opt-in) | `DELETE /api/v2/hosts/<id>/` | host removed from inventory entirely | manual re-add (`POST /api/v2/inventories/<inv_id>/hosts/` with name + variables); we do **not** archive enough to do this for you |

Disable is the default for the reason the README states: reversible, leaves the
host record (and its variables, group memberships, fact cache) intact. The
operator can flip it back the moment they realise the removal was wrong.

### 6.2 Resolving an AAP host ID from FQDN

AAP's API keys hosts by integer ID. We have `(inventory_name, fqdn)`. Two-step
lookup:

```python
# files/aap_client.py — sketch, stdlib urllib only

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

def _get(path, params, url, token):
    qs = urlencode(params)
    req = Request(f"{url}{path}?{qs}",
                  headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def inventory_id_by_name(url, token, name):
    data = _get("/api/v2/inventories/", {"name": name}, url, token)
    if data["count"] != 1:
        raise LookupError(f"inventory {name!r}: found {data['count']}")
    return data["results"][0]["id"]

def host_id_by_fqdn(url, token, inv_id, fqdn):
    data = _get("/api/v2/hosts/",
                {"name": fqdn, "inventory": inv_id}, url, token)
    if data["count"] != 1:
        raise LookupError(f"host {fqdn!r} in inv {inv_id}: found {data['count']}")
    return data["results"][0]["id"]
```

### 6.3 Disable — worked example

What the playbook produces, in plain `curl` so you can run it by hand to
verify against your AAP:

```bash
# 1) Resolve the inventory ID
curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=prod-linux" \
     "$AAP_URL/api/v2/inventories/" | jq '.results[0].id'
# → 42

# 2) Resolve the host ID inside that inventory
curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=stale-box-17.example.com" \
     --data-urlencode "inventory=42" \
     "$AAP_URL/api/v2/hosts/" | jq '.results[0].id'
# → 9173

# 3) Disable
curl -s -X PATCH -H "Authorization: Bearer $AAP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": false}' \
     "$AAP_URL/api/v2/hosts/9173/" | jq '.enabled'
# → false
```

And the reversal — the entire content of `restore.yml`'s per-host loop, in
`curl`:

```bash
curl -s -X PATCH -H "Authorization: Bearer $AAP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": true}' \
     "$AAP_URL/api/v2/hosts/9173/" | jq '.enabled'
# → true
```

### 6.4 Delete — worked example

Use only when you've decided the host record itself is garbage (not just the
host being temporarily offline). Re-adding requires you to recreate the host
record, including any group memberships and `host_vars` that AAP held for it.

```bash
curl -s -X DELETE -H "Authorization: Bearer $AAP_TOKEN" \
     "$AAP_URL/api/v2/hosts/9173/" -w "%{http_code}\n"
# → 204
```

To re-add (manual; `restore.yml` does **not** do this):

```bash
curl -s -X POST -H "Authorization: Bearer $AAP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name": "stale-box-17.example.com", "variables": "---\n"}' \
     "$AAP_URL/api/v2/inventories/42/hosts/"
```

### 6.5 Alternative: `awx.awx` collection (if available in your EE)

If the EE has the `awx.awx` collection, both modes can also be expressed as
Ansible module calls. This is fine for ad-hoc use but the toolkit core uses
the REST path above (no collection dependency, easier to test).

```yaml
- name: Disable host
  awx.awx.host:
    name: "{{ fqdn }}"
    inventory: "{{ inventory_name }}"
    enabled: false
    controller_host: "{{ aap_url }}"
    controller_oauthtoken: "{{ aap_token }}"

- name: Delete host
  awx.awx.host:
    name: "{{ fqdn }}"
    inventory: "{{ inventory_name }}"
    state: absent
    controller_host: "{{ aap_url }}"
    controller_oauthtoken: "{{ aap_token }}"
```

### 6.6 Adding a host — worked example (re-add or new add)

The inverse of delete. Used by `add-hosts.yml` and documented for hand-run
recovery:

```bash
# 1) Resolve the inventory ID (same as 6.3)
INV_ID=$(curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=prod-linux" \
     "$AAP_URL/api/v2/inventories/" | jq -r '.results[0].id')

# 2) Check if the host already exists (avoid duplicates / re-create errors)
curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=new-host.example.com" \
     --data-urlencode "inventory=$INV_ID" \
     "$AAP_URL/api/v2/hosts/" | jq '.count'
# → 0 = does not exist, proceed to POST.
# → 1 = exists; check .results[0].enabled and PATCH instead of POST.

# 3) Add (only if step 2 returned 0)
curl -s -X POST -H "Authorization: Bearer $AAP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name": "new-host.example.com", "variables": "---\n"}' \
     "$AAP_URL/api/v2/inventories/$INV_ID/hosts/" | jq '.id'
# → new host integer ID
```

The toolkit's `add-hosts.yml` wraps this with the prior-history lookup against
the lifecycle DB — that's the value-add over plain `curl`. Standalone Python
form: `python3 files/add_hosts.py --help`.

### 6.7 The audit trail

Every API call writes a row to `actions` and a line to `actions.csv` with:

- `action_kind`: `disable-host` | `delete-host` | `reenable-host`
- `mode`: `disable` | `delete`
- `aap_host_id`: the resolved integer ID
- `aap_response`: HTTP status + truncated body, or the error
- `triggering_runs`: JSON list of `run_id`s that justified the action
- `executed_at`: ISO timestamp

This is the audit story the README promises: for any host you removed, you can
answer "when, why (which runs proved it silent), and what AAP said about it."

---

## 7. Acks (per-host signal suppression)

Distinct from the allowlist (§8): allowlist = "permanent exemption"; ack =
"temporary mute on one signal."

### 7.1 Ack semantics

An ack matches an observation row for the purpose of filtering it out of the
category reports iff:

- `acks.fqdn == observation.fqdn`, AND
- (`acks.inventory IS NULL` OR `acks.inventory == observation.inventory`), AND
- `acks.signal_kind` matches the report being rendered, AND
- `acks.ack_until >= today`.

There is no automatic deletion of expired acks. They remain in the table for
audit; `ingest.yml` simply marks them inactive (by virtue of `ack_until <
today`). Operators can list acks (`ack.yml -e list=true`) and manually drop
them if the table grows uncomfortable.

### 7.2 Ack vs allowlist interaction

If a host is in the allowlist (`current_state=protected`), it is filtered out
of all category reports already — acks on it are inert but legal. Useful when
a host moves between protected and unprotected states (e.g., you removed it
from the allowlist temporarily and want to keep a returned-alert mute in place
for one more cycle).

### 7.3 Listing and removing acks

`ack.yml` defaults to insert/extend. Two operator modes via flags:

```bash
# List active acks for an inventory
ansible-playbook playbooks/ack.yml -e "list=true" -e "inventory=prod-linux"

# Remove (mark inactive) a specific ack
ansible-playbook playbooks/ack.yml -e "remove=true" \
    -e "fqdn=host-x.example.com" -e "signal=returned" \
    -e "inventory=prod-linux"
```

Removal sets `ack_until = today - 1 day` and writes an audit note; the row
stays in the table.

---

## 8. Allowlist file format

`vars/allowlist.csv`. CSV with header. Lines starting with `#` are skipped.

| Column | Required | Notes |
|---|---|---|
| `fqdn` | **yes** | lowercased on load |
| `inventory` | no | empty → matches every inventory containing this fqdn; non-empty → only that inventory |
| `reason` | no (strongly recommended) | shown in reports and PRs |
| `owner` | no | team or person to ping |
| `ack_until` | no | `YYYY-MM-DD`; past this date the entry still applies but a warning is logged so it gets re-reviewed |

Validation on load:

- `fqdn` empty → hard error, ingest fails.
- Same `(fqdn, inventory)` twice (with both `inventory` non-empty and equal, or
  both empty) → hard error, ingest fails. Same fqdn with one empty-inventory
  row and one specific-inventory row is allowed; if both apply, the
  specific-inventory row's metadata (reason/owner/ack_until) wins for that
  inventory.
- Unknown columns → hard error (typo-catching).
- `ack_until` unparseable → hard error.

Matching rule applied per observation `(inventory, fqdn)`:
1. Look for a row with this exact `(fqdn, inventory)`. If found, host is `excluded`; use that row's metadata.
2. Else look for a row with this fqdn and empty `inventory`. If found, host is `excluded`; use that row's metadata.
3. Else: not allowlisted.

The DB caches the parsed file into the `allowlist` table on every ingest
(full delete-then-insert). The CSV file is the source of truth.

---

## 9. Exit codes (summary)

| Code | Meaning |
|---|---|
| 0 | success (including an "aborted by sanity gate" outcome from `evaluate.yml`) |
| 2 | `ingest.yml`: one or more input files failed to parse |
| 3 | `act.yml` / `restore.yml` / `add-hosts.yml`: at least one AAP API call failed |
| 4 | DB unreachable, schema mismatch, or invalid rename/retire target (any playbook) |
| 5 | `ack.yml`: invalid input (unknown signal, non-positive days, etc.) |

Other non-zero exits come from Ansible itself (connectivity, syntax, etc.) and
have their own meanings.

---

## 10. Test plan

The Python core is plain stdlib; everything important is unit-testable without
Ansible.

Minimum test cases (these live in `files/tests/` and run under `pytest` or
plain `python -m unittest`):

- `classify_state.py`: one test per row in the §1 table, plus boundary cases
  (empty `error_class`, missing columns, unparseable `last_checked`).
- `lifecycle_db.py`: idempotent ingest (same file twice → no new rows),
  schema-version mismatch handling, concurrent writers (SQLite WAL), allowlist
  parse errors, ack insert/extend/list/remove.
- `evaluate_rules.py`: each rule (silent/returning/intermittent/pending_remove)
  with synthetic histories — including the off-by-one cases at the threshold
  boundary and the "transient up resets the silent streak" case. Ack
  filtering: same fixture with and without an active ack, asserting the
  candidate set differs only in the acked host.
- Sanity gates: each gate failing in isolation, two gates failing at once.
- `aap_client.py`: mock HTTP (stdlib `http.server` in a thread) for inventory
  lookup, host lookup, PATCH enabled, DELETE, error responses.
- End-to-end: ingest 14 days of synthetic CSVs at varying success rates,
  evaluate, assert the expected `to_remove.csv` / `returned.csv` content;
  run `act.yml -e apply=false` and assert `actions.csv` has the right
  dry-run rows; run `restore.yml` against a prior `actions.csv` (against
  the mock AAP) and assert re-enable calls land.

The Ansible playbooks are thin enough to need only smoke tests: "does
`ingest.yml -c local` against a fixture directory exit 0 and produce a populated
DB."
