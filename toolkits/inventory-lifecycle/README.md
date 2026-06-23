# Inventory lifecycle toolkit (AAP-ready)

Consumes the CSV output of [`identify-hosts`](../identify-hosts/), keeps a small
state DB on `mng_host`, and produces *time-aware* signals about your inventory:
which hosts have been silent long enough to remove, which ones disappeared and
came back, and which ones flap so much they need a human decision. It can also
**disable** stale hosts in AAP (via the AAP API), with a one-command reversal.
True delete is supported but gated harder — see
[SPEC §6 Removal mechanics](SPEC.md#6-removal-mechanics).

This toolkit is a **sibling** to `identify-hosts`. It does not run scans. It
reads the CSVs that `identify-hosts` writes, and writes its own per-run report
tree to the same share.

> [!IMPORTANT]
> Read [SPEC.md](SPEC.md) before changing rule thresholds or the state model.
> Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing the DB schema, where
> the playbooks run, or anything to do with concurrency.

## What it does, in one paragraph

Every time `identify-hosts` finishes, this toolkit's `ingest` playbook picks up
the new CSV(s), classifies each row into a small set of states (`up`,
`auth_failed`, `ssh_blocked`, `online_unmanaged`, `down`, `absent`), and appends
the result to a SQLite DB on `mng_host`. A separate `evaluate` playbook then
applies time-window rules to the DB and writes three CSV reports plus a Markdown
summary: hosts recommended for removal, hosts that returned after a long
absence, and hosts that flap. Destructive actions (disabling in AAP, pruning old
input CSVs) are **off by default**, gated by sanity checks, and capped per run.
An `ack` playbook lets operators mute a noisy signal for a single host for N
days; an `allowlist` (CSV under git) permanently exempts hosts that *should*
look stale (DR boxes, seasonal QA, monthly-batch hardware).

## Phased adoption (file → alert → act)

You don't have to turn this on all at once. The same playbooks support a graduated rollout:

| Phase | What runs | Side effects on inventory | Best for |
|---|---|---|---|
| **A. File-only** | `ingest.yml` + `evaluate.yml` | none — only writes reports to the share | first weeks; build trust in the rule outputs |
| **B. File + alert** | adds AAP **notification template** firing when `to_remove.csv` is non-empty | none on inventory; an email/Slack/webhook fires | once the reports look right; gets a human eye on candidates before any action |
| **C. File + alert + act (disable)** | adds `act.yml` with `apply=true, mode=disable` (behind an approval node) | hosts set to `enabled: false` in AAP — fully reversible | normal steady-state once you trust the reports |
| **D. File + alert + act (delete)** | `act.yml` with `mode=delete` (further-gated; see SPEC §6) | hosts removed from AAP inventory entirely — **not reversible without re-add** | rare housekeeping; most shops live at Phase C indefinitely |

A is the default. B and C are configuration choices, not extra playbooks. D
requires explicit `-e mode=delete` and refuses to run more than
`max_remove_per_run_abs` hosts even with the flag.

## Flow

```
identify-hosts                  inventory-lifecycle
─────────────────────           ──────────────────────────────────────
                                                ┌─→ to_remove.csv
discover.yml ──CSV──→ ingest.yml ──→ evaluate.yml ┼─→ returned.csv
                       (SQLite)                   ├─→ intermittent.csv
                                                  └─→ summary.md
                                                          │
                                                          ↓ (opt-in: Phase C/D)
                                                    act.yml
                                                 (disable | delete via AAP API
                                                  + prune old CSVs)

                  ─────────── manual ops ──────────
                  ack.yml         → mute one signal for one host for N days
                  restore.yml     → re-enable hosts from a prior actions.csv
```

1. **Ingest** — locate matching CSVs on the report share, hash them (skip if
   already ingested), classify each row, write `runs` + `observations` rows,
   update the `host_state` view. Decides whether the run was "successful" (≥
   `min_run_success_pct` of inventory was actually scanned) and stamps it.
2. **Evaluate** — apply the rule set defined in [SPEC.md](SPEC.md) over the
   trailing window. Produces the three CSVs and `summary.md`. Runs the sanity
   gates: if more than `max_remove_per_run_pct` of inventory crossed the
   threshold in one run, write `aborted.md` instead and refuse to produce
   category reports.
3. **Act** — *opt-in*. Disable (or, with explicit opt-in, delete) flagged
   hosts in AAP. Prune `identify-hosts` CSVs older than `keep_csv_days`,
   **except** any CSV that contributed to a recorded action (those are
   evidence — kept indefinitely until you decide otherwise).
4. **Ack** — *as needed*. Mute a single signal for a single host for N days
   when you've decided "yes, I know, leave it alone for now."
5. **Restore** — *as needed*. Re-enable hosts that were previously disabled
   (or `enabled: true` only — true-delete restoration is manual, see SPEC §6).

## Where things run

Same model as `identify-hosts`:

- **EE (the AAP execution environment)** runs the Ansible orchestration: glob
  for CSVs, render Jinja templates, call the AAP API if `act` is enabled.
- **`mng_host`** holds the SQLite DB and runs the Python core. All DB reads
  and writes are delegated there. The DB lives at
  `/myshare/data/lifecycle/state.sqlite` by default (override in `vars/main.yml`).
- The EE only needs `python3` (stdlib) for Jinja and the API call. The Python
  core on `mng_host` also uses only stdlib (`sqlite3`, `csv`, `hashlib`, `json`)
  so this stays air-gap-safe.

## Running in AAP

Job templates (three core, the rest operator-driven):

| JT name | Playbook | Schedule | What it does |
|---|---|---|---|
| `inventory-lifecycle-ingest` | `playbooks/ingest.yml` | post-discover (workflow node) | absorb new CSVs into the DB |
| `inventory-lifecycle-evaluate` | `playbooks/evaluate.yml` | daily | run rules, write reports, no side effects |
| `inventory-lifecycle-act` | `playbooks/act.yml` | manual w/ approval node | disable (or delete) flagged hosts, prune old CSVs |
| `inventory-lifecycle-ack` | `playbooks/ack.yml` | manual, ad-hoc | mute a signal for a host for N days |
| `inventory-lifecycle-restore` | `playbooks/restore.yml` | manual, ad-hoc | re-enable hosts disabled by an earlier `act.yml` |
| `inventory-lifecycle-add-hosts` | `playbooks/add-hosts.yml` | manual, ad-hoc | add fqdns to an inventory, with prior-history warning |
| `inventory-lifecycle-rename-inventory` | `playbooks/rename-inventory.yml` | manual, after AAP rename | migrate DB rows to the new inventory name |
| `inventory-lifecycle-retire-inventory` | `playbooks/retire-inventory.yml` | manual, when an AAP inventory is decommissioned | stop evaluating an inventory; preserve history |

The `ingest` and `evaluate` templates need no special privileges — they only
read CSVs and read/write the SQLite file. The `act` and `restore` templates
need an AAP credential (token) with permission to update hosts in the target
inventories, and `act` should be gated behind a workflow **approval node** so a
human accepts the batch before disables happen.

> [!WARNING]
> Don't put `act` on a cron schedule unless you've watched it produce sane
> output for at least one threshold window of real data. The sanity gates catch
> the common failure modes (network blip → mass false-positives), but the only
> real test is "did this run actually recommend hosts you agree should go?"

## Running locally (no AAP)

```bash
# Ingest a directory of CSVs into the DB
ansible-playbook -i 'localhost,' playbooks/ingest.yml -c local \
    -e "csv_glob=/path/to/csvs/*_servers_info.csv" \
    -e "db_path=./state.sqlite"

# Run the rules, write reports to ./reports/
ansible-playbook -i 'localhost,' playbooks/evaluate.yml -c local \
    -e "db_path=./state.sqlite" \
    -e "report_dir=./reports/"

# Act (dry-run by default; pass apply=true to actually call AAP/prune)
ansible-playbook -i 'localhost,' playbooks/act.yml -c local \
    -e "db_path=./state.sqlite" -e "apply=false"

# Mute "returned" alerts for a single host for 30 days
ansible-playbook -i 'localhost,' playbooks/ack.yml -c local \
    -e "fqdn=host-x.example.com" -e "signal=returned" -e "days=30" \
    -e "reason='known DR drill on the 12th'"

# Re-enable hosts disabled in a prior act run
ansible-playbook -i 'localhost,' playbooks/restore.yml -c local \
    -e "from_actions_csv=./reports/prod-linux/2026-06-15/actions.csv"
```

The local path is mostly for testing the rule engine on captured CSVs. Real use
is in AAP.

## Tunable knobs

Everything lives in [`vars/main.yml`](vars/main.yml). The values below are the
**defaults** — override per-inventory in `vars/inventories.yml` or per-run with
`-e`. The full semantics are in [SPEC.md §3](SPEC.md#3-rules-and-thresholds).

| Knob | Default | Meaning |
|---|---|---|
| `silent_threshold_days` | `30` | days a host can be non-`up` before it's flagged for removal |
| `returned_threshold_days` | `14` | minimum absent days for a "returned" alert |
| `intermittent_min_flips` | `4` | up↔down transitions in window to flag as intermittent |
| `intermittent_window_days` | `30` | window for counting flips |
| `max_remove_per_run_pct` | `5` | abort the run if more than this % is flagged at once |
| `max_remove_per_run_abs` | `50` | hard cap on flagged hosts per run |
| `min_run_success_pct` | `85` | a run must scan at least this fraction of inventory to "count" |
| `min_successful_runs_before_action` | `5` | host needs N *successful* runs of evidence |
| `keep_csv_days` | `90` | prune `identify-hosts` CSVs older than this (evidence CSVs are kept) |
| `db_path` | `/myshare/data/lifecycle/state.sqlite` | location of the SQLite DB on `mng_host` |
| `report_dir` | `/myshare/data/lifecycle/reports/` | base directory for output reports |

## Allowlist (permanent exemption) vs Ack (temporary suppression)

These are two different mechanisms; don't conflate them.

- **Allowlist** — "this host is *supposed* to look stale; never flag it." Lives
  in [`vars/allowlist.csv`](vars/allowlist.csv) under git. Peer-reviewable.
  Permanent (until removed). For: cold DR boxes, monthly batch runners,
  hardware whose normal state is "off." Operator changes are pull requests.
- **Ack** — "I saw this signal once, mute it for N days." Lives in the DB.
  Set via `playbooks/ack.yml`. Temporary (auto-expires). For: "yes I know this
  DR drill is today; stop bugging me until Monday."

### Allowlist file format (CSV)

```csv
fqdn,inventory,reason,owner,ack_until
dr-db-01.example.com,,DR cold standby; quarterly DR drill,dba-team,2027-01-01
load-test-04.example.com,qa-linux,Spun up only during release windows,qe,
batch-runner-03.example.com,,Wakes monthly for close-of-books,,
host-x.example.com,prod-linux,Owned by app team,app-team,
host-x.example.com,dr-linux,Owned by app team (DR copy),app-team,
```

Schema (only `fqdn` is required):

| Column | Required | Meaning |
|---|---|---|
| `fqdn` | **yes** | the host's FQDN (lowercased on load) |
| `inventory` | no | if empty, applies to **every** inventory where this fqdn appears; if set, only that inventory |
| `reason` | no (recommended) | shown verbatim in reports and PR review |
| `owner` | no | team or person to ping with questions |
| `ack_until` | no | YYYY-MM-DD; past this date the entry still applies but ingest emits a warning so it gets re-reviewed |

The same fqdn may appear in multiple rows (one with empty `inventory` plus one
per specific inventory, or multiple specific inventories). Two rows with the
*same* `(fqdn, inventory)` is a hard error — the parser refuses to load and
ingest fails so the operator fixes the conflict.

Lines starting with `#` are treated as comments and skipped by the parser.

An allowlist match makes the host's `current_state = protected`. It's still
observed and counted, but it never appears in `to_remove.csv` and its return
after an absence does not raise a `returned` alert (we expect those returns).

### Ack semantics

`playbooks/ack.yml` is a small operator command that inserts a row into the
`acks` table:

```bash
ansible-playbook playbooks/ack.yml \
    -e "fqdn=host-x.example.com" \
    -e "signal=returned" \
    -e "days=30" \
    -e "reason='DR drill on the 12th; known transient return'"
# optional: -e inventory=prod-linux  (omit to apply to every inventory)
# optional: -e added_by=alice         (defaults to $USER / $ANSIBLE_USER)
```

`signal` is one of `returned`, `intermittent`, `silent`, `pending_remove`. The
ack auto-expires after `days` and the alert resumes if the underlying condition
still holds. Active acks are listed in `summary.md`. Full semantics are in
[SPEC.md §7](SPEC.md#7-acks-per-host-signal-suppression).

## Outputs

`evaluate.yml` writes two trees under `report_dir` on `mng_host`. All CSVs are
sorted by fqdn for deterministic diffs.

**Per-inventory** at `{{ report_dir }}/<inventory>/<UTC-date>/`:

| File | Contents |
|---|---|
| `summary.md` | human-readable rollup: counts per state, active acks, links to the other files |
| `to_remove.csv` | hosts recommended for removal in this inventory (one row per `(inventory, fqdn)`); columns include `also_active_in` and `already_removed_from` |
| `returned.csv` | hosts that returned to `up` after ≥ `returned_threshold_days` absent — with "what changed" hints (MAC, IPv4, OS) |
| `intermittent.csv` | hosts with ≥ `intermittent_min_flips` flips in window — candidates for allowlist review |
| `actions.csv` | what `act.yml` actually did this run; only written by `act.yml`, never by `evaluate.yml` |
| `aborted.md` | only written when sanity gates fail; explains *why* the run refused to act, lists the offending counts |

**Cross-inventory rollup** at `{{ report_dir }}/_all/<UTC-date>/`:

| File | Contents |
|---|---|
| `summary.md` | global rollup across all inventories: unique flagged fqdns, cross-inventory hotspots |
| `to_remove.csv` | one row per *unique* fqdn flagged for removal in any inventory; `inventories_to_remove` column lists which |
| `returned.csv` | one row per unique returned fqdn across all inventories |
| `intermittent.csv` | one row per unique intermittent fqdn across all inventories |

The per-inventory files are what `act.yml` consumes (action is per-inventory).
The rollup is informational: grep by fqdn to see the whole picture for one
host without cross-grepping per-inventory files.

CSV column lists are in [SPEC.md §4](SPEC.md#4-output-formats).

## Multi-inventory behavior

State is keyed by `(inventory, fqdn)`. The same fqdn in two inventories
produces two independent `host_state` rows — they age and flag independently.
That's the right behavior because the action target in AAP is also keyed by
`(inventory, host_id)`.

Operator visibility comes from three things:

1. **`also_active_in` column** on `to_remove.csv` and `returned.csv` — other
   inventories where this fqdn is *currently* `active`.
2. **`already_removed_from` column** on the same files — other inventories
   where this fqdn was previously disabled or deleted by `act.yml` (audit:
   "we already removed this from B months ago"). Format:
   `inventoryB:delete:2026-04-22;qa-linux:disable:2026-05-10`.
3. **Cross-inventory rollup** at `report_dir/_all/<date>/` — one line per
   *unique* fqdn flagged in any inventory this run, with the full multi-inventory
   picture in one row (full schema in [SPEC §4.7](SPEC.md#47-cross-inventory-rollup-_alldate)).
   The rollup is the file to grep when you want "everything about host-x in
   one place."

All CSVs are sorted by fqdn (then inventory) so diffs across runs are stable
and a human scanning a single file finds hosts predictably.

Common scenarios and what the operator sees:

| Scenario | What appears in reports |
|---|---|
| fqdn moved test → prod | `test/to_remove.csv` flags it with `also_active_in=[prod]` — operator confirms the move was intentional |
| fqdn added to test (still in prod) | both inventories see it as active; nothing to do |
| fqdn removed from test only (still in prod) | `test/to_remove.csv` flags with `also_active_in=[prod]` — confirms partial removal |
| fqdn removed from B months ago, A scanned rarely, A's run finally flags it | `A/to_remove.csv` row carries `already_removed_from=B:delete:2026-04-22` — the operator sees prior history immediately, confirms rather than re-litigates |
| fqdn flagged silent in both prod and dr at the same time | two rows (one per inventory file), each with `also_active_in=[]`. The `_all/<date>/to_remove.csv` rollup carries one line with `inventories_to_remove=[prod, dr]` |

The two harder scenarios — **renaming** and **retiring** an AAP inventory —
need explicit toolkit-side commands. Without them, every host in the
affected inventory looks like a mass removal event and the sanity gates abort
the next run. See [Inventory rename / retire](#inventory-rename--retire).

## Inventory rename / retire

When AAP-side inventory names change, the lifecycle DB needs to know.

### Rename

```bash
# Preview what will move (default — apply=false)
ansible-playbook playbooks/rename-inventory.yml \
    -e "from=prod-linux" -e "to=prod-rhel"

# Actually migrate the DB rows
ansible-playbook playbooks/rename-inventory.yml \
    -e "from=prod-linux" -e "to=prod-rhel" -e "apply=true"
```

Updates `inventory` in every row of `host_state`, `observations`, `runs`,
`actions`, `acks`, and `allowlist` cache from `from` to `to`. Marks the old
name `status='renamed'` for audit (rows aren't deleted). Run this **after**
renaming in AAP; the next CSV will arrive with the new name and append
cleanly to the migrated history. Full contract:
[SPEC §5.6](SPEC.md#56-rename-inventoryyml).

### Retire

```bash
ansible-playbook playbooks/retire-inventory.yml \
    -e "inventory=old-vendor-pool" -e "apply=true"
```

Marks the inventory `status='retired'`. `evaluate.yml` skips it from then on,
no more reports, no rule processing. History is preserved for audit. If a
CSV later arrives bearing a retired inventory's name, ingest treats it as an
error (usually means a name was re-used or a rename was missed).

## Adding hosts back (re-add script)

Two needs are served by the same tool:

1. **Restoring a host deleted by an earlier `act.yml -e mode=delete`** —
   `restore.yml` only re-enables disabled hosts; it can't re-create deleted
   ones (we don't archive enough; see [SPEC §6.4](SPEC.md#64-delete--worked-example)).
2. **Adding hosts from an external source** — a CMDB export, a peer team's
   spreadsheet, a vendor onboarding list.

The side-effect that justifies the script's existence: **it prints any prior
lifecycle history for each fqdn before adding it.** When a team requests
creation of a host that this toolkit removed two months ago, the operator
sees it — that's the cue to ask "why is this back?" before silently
re-adding it.

### Usage

```bash
# Single host
ansible-playbook playbooks/add-hosts.yml \
    -e "inventory=prod-linux" \
    -e "fqdn=new-host.example.com" \
    -e "aap_url=$AAP_URL" -e "aap_token=$AAP_TOKEN" \
    -e "apply=true"

# Batch from a text file (one fqdn per line; blank lines & '#' comments OK)
ansible-playbook playbooks/add-hosts.yml \
    -e "inventory=prod-linux" \
    -e "from_file=/tmp/new-hosts.txt" \
    -e "aap_url=$AAP_URL" -e "aap_token=$AAP_TOKEN" \
    -e "apply=true"

# Dry-run: print decisions and prior history, do NOT call AAP
ansible-playbook playbooks/add-hosts.yml \
    -e "inventory=prod-linux" -e "from_file=/tmp/new-hosts.txt" \
    -e "apply=false"
```

Standalone Python form (for ad-hoc use outside AAP, on `mng_host`):

```bash
python3 files/add_hosts.py \
    --aap-url "$AAP_URL" --aap-token "$AAP_TOKEN" \
    --inventory prod-linux \
    --from-file /tmp/new-hosts.txt \
    [--db-path /myshare/data/lifecycle/state.sqlite] \
    [--no-reenable]   # leave existing-but-disabled hosts as-is
    [--dry-run]
```

### Sample output

```
ADD       host-a.example.com → prod-linux: no prior history; created (AAP id=10421)
ADD       host-b.example.com → prod-linux: ⚠ prior history in prod-linux —
                                            disabled 2026-03-15 (run 47),
                                            deleted 2026-04-22 (run 63); created (id=10422)
SKIP      host-c.example.com → prod-linux: already exists, enabled=true
REENABLE  host-d.example.com → prod-linux: existed but disabled 2026-04-12; re-enabled (id=9981)
ERROR     host-e.example.com → prod-linux: AAP 400 — name conflict in shared group
```

Every action also writes an `actions.csv` row (`action_kind=add-host` or
`reenable-host`). The history check covers **all** inventories — if `host-b`
was previously removed from `dr-linux` and you're now adding it to
`prod-linux`, the warning still fires.

### Hand-rolled equivalent (no toolkit needed)

```bash
# Resolve inventory ID
INV_ID=$(curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=prod-linux" \
     "$AAP_URL/api/v2/inventories/" | jq -r '.results[0].id')

# Check existence first to avoid duplicate-name errors
EXISTS=$(curl -sG -H "Authorization: Bearer $AAP_TOKEN" \
     --data-urlencode "name=new-host.example.com" \
     --data-urlencode "inventory=$INV_ID" \
     "$AAP_URL/api/v2/hosts/" | jq '.count')

if [ "$EXISTS" = "0" ]; then
  curl -s -X POST -H "Authorization: Bearer $AAP_TOKEN" \
       -H "Content-Type: application/json" \
       -d '{"name": "new-host.example.com", "variables": "---\n"}' \
       "$AAP_URL/api/v2/inventories/$INV_ID/hosts/"
fi
```

This is what `add-hosts.yml` does internally, minus the lifecycle history
check (the value-add of the toolkit version).

## Identity caveats

This toolkit tracks **`(inventory, fqdn)` pairs**, not "hosts" in any deeper
sense. A renamed or re-imaged box looks identical to "old host went away, new
host appeared." MAC address is captured when nmap provides it and surfaces as a
"what changed" hint in the `returned` report, but it's not part of the identity
key. See [ARCHITECTURE.md §6](ARCHITECTURE.md#6-known-limits) for the full list
of things this tool cannot tell apart.

## Relationship to `identify-hosts`

- **One-way dependency.** This toolkit reads CSVs that `identify-hosts` writes.
  `identify-hosts` knows nothing about this toolkit. You can keep running
  `identify-hosts` standalone forever; nothing breaks.
- **Stable contract.** The CSV column set documented in
  [`identify-hosts/README.md`](../identify-hosts/README.md#csv-columns) is what
  the ingest parser depends on. If that contract changes, this toolkit needs a
  matching update — covered in [SPEC.md §2](SPEC.md#2-input-format-the-contract).
- **No coupling on scheduling.** This toolkit doesn't care if discovery runs
  daily, weekly, or ad-hoc. All thresholds are in calendar days against
  `last_checked`, not run counts.

## Files

| Path | Purpose |
|---|---|
| `playbooks/ingest.yml` | absorb new `identify-hosts` CSVs into the SQLite DB |
| `playbooks/evaluate.yml` | apply rules, write reports, no side effects |
| `playbooks/act.yml` | disable or delete hosts in AAP + prune old CSVs; gated and dry-run by default |
| `playbooks/ack.yml` | insert a signal-suppression row into the `acks` table |
| `playbooks/restore.yml` | re-enable hosts disabled by a prior `act.yml` run |
| `playbooks/add-hosts.yml` | add fqdns to an AAP inventory; print prior lifecycle history |
| `playbooks/rename-inventory.yml` | migrate DB state when an AAP inventory is renamed |
| `playbooks/retire-inventory.yml` | mark an inventory retired; stop evaluating it |
| `playbooks/full-cycle.yml` | local-dev convenience: ingest → evaluate (no act) |
| `files/lifecycle_db.py` | SQLite schema, ingest, host_state update, queries — stdlib only |
| `files/classify_state.py` | CSV row → one of the seven states (see SPEC §1) |
| `files/evaluate_rules.py` | apply rules → set of action recommendations |
| `files/aap_client.py` | thin stdlib `urllib` wrapper around the AAP REST API (host lookup, PATCH, DELETE, POST) |
| `files/act.py` | act CLI: disable/delete in AAP per the most recent `to_remove.csv` + prune evidence-safe |
| `files/restore.py` | restore CLI: re-enable hosts from a prior `actions.csv` |
| `files/add_hosts.py` | standalone re-add / new-add CLI with prior-history lookup |
| `files/tests/test_classify_state.py` | unit tests for the classifier (run with `python3 -m unittest`) |
| `vars/main.yml` | tunable knobs (single source, `-e`-overridable) |
| `vars/allowlist.csv` | hosts that should never be flagged for removal — see schema above |
| `vars/inventories.yml` | per-inventory threshold overrides (optional) |

## Further reading inside this toolkit

- [SPEC.md](SPEC.md) — the state model, the rules, the sanity gates, the
  output schemas, the removal mechanics with worked examples, exit codes.
- [ARCHITECTURE.md](ARCHITECTURE.md) — components, where they run, the SQLite
  schema (including `acks`), concurrency story, failure modes, known limits.
