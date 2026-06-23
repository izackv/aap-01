"""SQLite-backed lifecycle DB + CLI for ingest, acks, rename, retire.

Stdlib only. See ARCHITECTURE §3 for the schema, SPEC §5 for the playbook
contracts that drive each CLI subcommand.

CLI subcommands:
    init                — create schema (idempotent)
    ingest              — absorb CSVs into the DB
    ack                 — insert/extend a signal mute
    ack-list            — list active acks
    ack-remove          — expire an active ack
    rename-inventory    — migrate state to a new inventory name
    retire-inventory    — mark an inventory as no longer evaluated
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from glob import glob

# Allow importing classify_state from the same directory when run as a script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_state import classify, safe_bool, BoolParseError


SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventories (
    inventory     TEXT    PRIMARY KEY,
    status        TEXT    NOT NULL,
    first_seen    TEXT    NOT NULL,
    last_ingest   TEXT,
    retired_at    TEXT,
    renamed_from  TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory         TEXT    NOT NULL,
    scan_date         TEXT    NOT NULL,
    aap_job_id        TEXT,
    csv_path          TEXT    NOT NULL,
    csv_sha256        TEXT    NOT NULL,
    total_hosts       INTEGER NOT NULL,
    scanned_hosts     INTEGER NOT NULL,
    dropped_rows      INTEGER NOT NULL DEFAULT 0,
    ingest_ts         TEXT    NOT NULL,
    is_successful_run INTEGER NOT NULL,
    UNIQUE(csv_sha256)
);

CREATE TABLE IF NOT EXISTS observations (
    run_id            INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    inventory         TEXT    NOT NULL,
    fqdn              TEXT    NOT NULL,
    ipv4              TEXT,
    mac               TEXT,
    mac_vendor        TEXT,
    os                TEXT,
    state             TEXT    NOT NULL,
    detection_method  TEXT,
    error_class       TEXT,
    raw_reachable     INTEGER,
    raw_ssh_open      INTEGER,
    last_checked      TEXT,
    PRIMARY KEY (run_id, inventory, fqdn)
);
CREATE INDEX IF NOT EXISTS idx_obs_inv_fqdn ON observations(inventory, fqdn);
CREATE INDEX IF NOT EXISTS idx_obs_state    ON observations(state);

CREATE TABLE IF NOT EXISTS host_state (
    inventory               TEXT    NOT NULL,
    fqdn                    TEXT    NOT NULL,
    first_seen              TEXT    NOT NULL,
    last_observed           TEXT    NOT NULL,
    last_seen_up            TEXT,
    consecutive_misses      INTEGER NOT NULL DEFAULT 0,
    consecutive_silent_runs INTEGER NOT NULL DEFAULT 0,
    current_state           TEXT    NOT NULL,
    last_ipv4               TEXT,
    last_mac                TEXT,
    last_mac_vendor         TEXT,
    last_os                 TEXT,
    last_error_class        TEXT,
    PRIMARY KEY (inventory, fqdn)
);

CREATE TABLE IF NOT EXISTS allowlist (
    inventory  TEXT,
    fqdn       TEXT    NOT NULL,
    reason     TEXT,
    owner      TEXT,
    added_at   TEXT    NOT NULL,
    ack_until  TEXT,
    PRIMARY KEY (inventory, fqdn)
);

CREATE TABLE IF NOT EXISTS acks (
    ack_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory     TEXT,
    fqdn          TEXT    NOT NULL,
    signal_kind   TEXT    NOT NULL,
    ack_until     TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    added_at      TEXT    NOT NULL,
    added_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_acks_active ON acks(fqdn, signal_kind, ack_until);

CREATE TABLE IF NOT EXISTS actions (
    action_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    inventory        TEXT    NOT NULL,
    fqdn             TEXT,
    action_kind      TEXT    NOT NULL,
    mode             TEXT,
    dry_run          INTEGER NOT NULL,
    executed         INTEGER NOT NULL DEFAULT 0,
    executed_at      TEXT,
    reason           TEXT    NOT NULL,
    triggering_runs  TEXT,
    aap_host_id      INTEGER,
    aap_response     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_kind ON actions(action_kind, executed);
"""

VALID_SIGNAL_KINDS = frozenset({"returned", "intermittent", "silent", "pending_remove"})
FILENAME_RE = re.compile(
    r"^(?P<inventory>[^_]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<jobid>[^_]+)_servers_info\.csv$"
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso():
    return date.today().isoformat()


def parse_iso_date(s):
    """Accept either a 10-char ISO date or a full ISO-8601 timestamp; return date."""
    if not s:
        return None
    s = s.strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def days_between(start_iso, end_iso):
    """Calendar-day difference (end - start). Returns None if either is None."""
    a = parse_iso_date(start_iso)
    b = parse_iso_date(end_iso)
    if a is None or b is None:
        return None
    return (b - a).days


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def connect(db_path):
    """Open the DB (creating it if missing); verify schema version."""
    new_db = not os.path.exists(db_path)
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if new_db or version == 0:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    elif version != SCHEMA_VERSION:
        conn.close()
        sys.stderr.write(
            f"ERROR: DB schema version mismatch: file={version} code={SCHEMA_VERSION}. "
            f"Refusing to open {db_path}. Run the migration script (none yet for v1).\n"
        )
        sys.exit(4)

    # Always re-run CREATE IF NOT EXISTS to recover from a partial earlier create.
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

ALLOWLIST_COLUMNS = frozenset({"fqdn", "inventory", "reason", "owner", "ack_until"})


def load_allowlist_csv(path):
    """Parse vars/allowlist.csv into a list of dicts. Raises on validation errors."""
    if not path or not os.path.exists(path):
        return []
    entries = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        filtered = (line for line in f if not line.lstrip().startswith("#"))
        reader = csv.DictReader(filtered)
        if not reader.fieldnames:
            return []
        unknown = set(reader.fieldnames) - ALLOWLIST_COLUMNS
        if unknown:
            raise ValueError(f"unknown allowlist columns: {sorted(unknown)}")
        for i, row in enumerate(reader, start=2):
            fqdn = (row.get("fqdn") or "").strip().lower()
            if not fqdn:
                raise ValueError(f"allowlist row {i}: fqdn is required")
            inventory = (row.get("inventory") or "").strip() or None
            key = (inventory, fqdn)
            if key in seen:
                raise ValueError(f"allowlist row {i}: duplicate ({inventory!r}, {fqdn!r})")
            seen.add(key)
            ack_until = (row.get("ack_until") or "").strip() or None
            if ack_until and parse_iso_date(ack_until) is None:
                raise ValueError(f"allowlist row {i}: bad ack_until {ack_until!r}")
            entries.append({
                "inventory": inventory,
                "fqdn": fqdn,
                "reason": (row.get("reason") or "").strip() or None,
                "owner": (row.get("owner") or "").strip() or None,
                "ack_until": ack_until,
            })
    return entries


def cache_allowlist(conn, entries):
    """Delete-then-insert the allowlist table from parsed entries."""
    with conn:
        conn.execute("DELETE FROM allowlist")
        now = now_iso()
        for e in entries:
            conn.execute(
                """INSERT INTO allowlist (inventory, fqdn, reason, owner, added_at, ack_until)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (e["inventory"], e["fqdn"], e["reason"], e["owner"], now, e["ack_until"]),
            )


def allowlist_lookup_set(entries):
    """Return a set of (inventory_or_None, fqdn) for fast in-set lookup."""
    return {(e["inventory"], e["fqdn"]) for e in entries}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_csv_filename(path):
    m = FILENAME_RE.match(os.path.basename(path))
    if not m:
        return None
    return {
        "inventory": m.group("inventory"),
        "scan_date": m.group("date"),
        "aap_job_id": m.group("jobid"),
    }


def _is_silent(last_seen_up, first_seen, today, silent_days):
    """Has the host been non-`up` for ≥ silent_days?

    Uses last_seen_up if present; otherwise falls back to first_seen so that
    a brand-new always-down host gets the same grace period as a host whose
    `up` observations stopped.
    """
    anchor = last_seen_up or first_seen
    if not anchor:
        return False
    diff = days_between(anchor, today)
    if diff is None:
        return False
    return diff >= silent_days


def _recompute_for_observed(conn, inventory, scan_date, obs, is_successful, tunables):
    silent_days = tunables["silent_threshold_days"]
    min_silent_runs = tunables["min_successful_runs_before_action"]
    fqdn = obs["fqdn"]

    existing = conn.execute(
        """SELECT first_seen, last_seen_up, consecutive_misses,
                  consecutive_silent_runs, current_state
           FROM host_state WHERE inventory = ? AND fqdn = ?""",
        (inventory, fqdn),
    ).fetchone()
    if existing:
        first_seen, prior_last_up, prior_misses, prior_silent, prior_state = existing
    else:
        first_seen, prior_last_up, prior_misses, prior_silent, prior_state = (
            scan_date, None, 0, 0, None,
        )

    state = obs["state"]
    if state == "up":
        new_last_up = scan_date
        new_misses = 0
        new_silent_streak = 0
    elif state == "online_unmanaged":
        # Observed alive but not SSH-manageable (Windows, switch, NAS).
        # SPEC §1: never counts toward silent. Reset miss counter; don't
        # touch the silent streak (so we don't accidentally bake transient
        # online_unmanaged observations into the streak).
        new_last_up = prior_last_up
        new_misses = 0
        new_silent_streak = 0
    else:
        new_last_up = prior_last_up
        new_misses = prior_misses + 1
        if state == "excluded":
            new_silent_streak = 0
        elif _is_silent(new_last_up, first_seen, scan_date, silent_days) and is_successful:
            new_silent_streak = prior_silent + 1
        else:
            new_silent_streak = prior_silent

    # Derived current_state per priority: protected > pending_remove > silent
    #                                     > returning > active.
    # (intermittent is window-derived in evaluate; not stored here.)
    if state == "excluded":
        current_state = "protected"
    elif state == "up":
        current_state = "returning" if prior_state in ("silent", "pending_remove") else "active"
    elif state == "online_unmanaged":
        # Observed; not an SSH inventory removal candidate.
        current_state = "active"
    elif _is_silent(new_last_up, first_seen, scan_date, silent_days):
        if new_silent_streak >= min_silent_runs and is_successful:
            current_state = "pending_remove"
        else:
            current_state = "silent"
    else:
        current_state = "active"

    conn.execute(
        """INSERT INTO host_state
            (inventory, fqdn, first_seen, last_observed, last_seen_up,
             consecutive_misses, consecutive_silent_runs, current_state,
             last_ipv4, last_mac, last_mac_vendor, last_os, last_error_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(inventory, fqdn) DO UPDATE SET
             last_observed = excluded.last_observed,
             last_seen_up = excluded.last_seen_up,
             consecutive_misses = excluded.consecutive_misses,
             consecutive_silent_runs = excluded.consecutive_silent_runs,
             current_state = excluded.current_state,
             last_ipv4 = COALESCE(excluded.last_ipv4, host_state.last_ipv4),
             last_mac  = COALESCE(excluded.last_mac, host_state.last_mac),
             last_mac_vendor = COALESCE(excluded.last_mac_vendor, host_state.last_mac_vendor),
             last_os = COALESCE(excluded.last_os, host_state.last_os),
             last_error_class = excluded.last_error_class""",
        (inventory, fqdn, first_seen, scan_date, new_last_up,
         new_misses, new_silent_streak, current_state,
         obs.get("ipv4"), obs.get("mac"), obs.get("mac_vendor"), obs.get("os"),
         obs.get("error_class")),
    )


def _recompute_for_absent(conn, inventory, scan_date, fqdn, is_successful, tunables):
    silent_days = tunables["silent_threshold_days"]
    min_silent_runs = tunables["min_successful_runs_before_action"]
    existing = conn.execute(
        """SELECT first_seen, last_seen_up, consecutive_misses,
                  consecutive_silent_runs, current_state
           FROM host_state WHERE inventory = ? AND fqdn = ?""",
        (inventory, fqdn),
    ).fetchone()
    if not existing:
        return
    first_seen, prior_last_up, prior_misses, prior_silent, prior_state = existing

    new_misses = prior_misses + 1
    silent_now = _is_silent(prior_last_up, first_seen, scan_date, silent_days)
    if prior_state == "protected":
        new_silent_streak = 0
        current_state = "protected"
    elif silent_now and is_successful:
        new_silent_streak = prior_silent + 1
        current_state = "pending_remove" if new_silent_streak >= min_silent_runs else "silent"
    elif silent_now:
        new_silent_streak = prior_silent
        current_state = "silent" if prior_state != "pending_remove" else "pending_remove"
    else:
        new_silent_streak = prior_silent
        current_state = prior_state if prior_state else "active"

    conn.execute(
        """UPDATE host_state
              SET consecutive_misses = ?, consecutive_silent_runs = ?, current_state = ?
            WHERE inventory = ? AND fqdn = ?""",
        (new_misses, new_silent_streak, current_state, inventory, fqdn),
    )


def ingest_csv(conn, csv_path, allowlist_set, tunables, inventory_override=None):
    """Ingest one identify-hosts CSV. Returns a result dict.

    The whole CSV is one transaction: either everything sticks or nothing does.
    Re-running on the same SHA-256 is a no-op (returns skipped=True).
    """
    sha = file_sha256(csv_path)
    existing = conn.execute(
        "SELECT run_id FROM runs WHERE csv_sha256 = ?", (sha,)
    ).fetchone()
    if existing:
        return {"skipped": True, "run_id": existing[0], "csv_path": csv_path}

    meta = parse_csv_filename(csv_path)
    if meta is None and not inventory_override:
        raise ValueError(
            f"filename does not match <inventory>_<date>_<jobid>_servers_info.csv "
            f"and no inventory_override given: {csv_path}"
        )
    if inventory_override:
        inventory = inventory_override
        scan_date = (meta and meta["scan_date"]) or today_iso()
        aap_job_id = (meta and meta["aap_job_id"]) or "manual"
    else:
        inventory = meta["inventory"]
        scan_date = meta["scan_date"]
        aap_job_id = meta["aap_job_id"]

    # Refuse to ingest into a retired inventory.
    status_row = conn.execute(
        "SELECT status FROM inventories WHERE inventory = ?", (inventory,)
    ).fetchone()
    if status_row and status_row[0] == "retired":
        raise ValueError(
            f"inventory {inventory!r} is retired; refusing to ingest {csv_path}. "
            f"Un-retire (manual SQL) or rename in AAP first."
        )

    observations = []
    dropped = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fqdn = (row.get("fqdn") or "").strip().lower()
            if not fqdn:
                dropped += 1
                continue
            lc = (row.get("last_checked") or "").strip()
            if lc and parse_iso_date(lc) is None:
                dropped += 1
                continue
            row["inventory"] = inventory
            try:
                state = classify(row, allowlist=None)
            except BoolParseError:
                dropped += 1
                continue
            if (inventory, fqdn) in allowlist_set or (None, fqdn) in allowlist_set:
                state = "excluded"
            observations.append({
                "fqdn": fqdn,
                "ipv4": (row.get("ipv4") or "").strip() or None,
                "mac": (row.get("mac") or "").strip() or None,
                "mac_vendor": (row.get("mac_vendor") or "").strip() or None,
                "os": (row.get("os") or "").strip() or None,
                "state": state,
                "detection_method": (row.get("detection_method") or "").strip() or None,
                "error_class": (row.get("error_class") or "").strip() or None,
                "raw_reachable": 1 if safe_bool(row.get("reachable")) else 0,
                "raw_ssh_open": 1 if safe_bool(row.get("ssh_open")) else 0,
                "last_checked": lc or None,
            })

    scanned = len(observations)
    prior = conn.execute(
        "SELECT fqdn FROM host_state WHERE inventory = ?", (inventory,)
    ).fetchall()
    prior_fqdns = {r[0] for r in prior}
    seen_fqdns = {o["fqdn"] for o in observations}
    absent_fqdns = prior_fqdns - seen_fqdns
    total = scanned + len(absent_fqdns)

    # is_successful_run: scanned / total ≥ min_run_success_pct
    if total == 0:
        is_successful = True  # empty inventory, nothing to score
    else:
        is_successful = scanned * 100 >= total * tunables["min_run_success_pct"]
    is_successful_int = 1 if is_successful else 0

    try:
        conn.execute("BEGIN")

        cur = conn.execute(
            """INSERT INTO runs
                 (inventory, scan_date, aap_job_id, csv_path, csv_sha256,
                  total_hosts, scanned_hosts, dropped_rows, ingest_ts, is_successful_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inventory, scan_date, aap_job_id, csv_path, sha,
             total, scanned, dropped, now_iso(), is_successful_int),
        )
        run_id = cur.lastrowid

        # Upsert the inventories row.
        conn.execute(
            """INSERT INTO inventories (inventory, status, first_seen, last_ingest)
                 VALUES (?, 'active', ?, ?)
               ON CONFLICT(inventory) DO UPDATE SET last_ingest=excluded.last_ingest""",
            (inventory, now_iso(), now_iso()),
        )

        for o in observations:
            conn.execute(
                """INSERT INTO observations
                     (run_id, inventory, fqdn, ipv4, mac, mac_vendor, os, state,
                      detection_method, error_class, raw_reachable, raw_ssh_open, last_checked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, inventory, o["fqdn"], o["ipv4"], o["mac"], o["mac_vendor"],
                 o["os"], o["state"], o["detection_method"], o["error_class"],
                 o["raw_reachable"], o["raw_ssh_open"], o["last_checked"]),
            )

        for o in observations:
            _recompute_for_observed(conn, inventory, scan_date, o, is_successful, tunables)
        for fqdn in absent_fqdns:
            _recompute_for_absent(conn, inventory, scan_date, fqdn, is_successful, tunables)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "skipped": False,
        "run_id": run_id,
        "rows": scanned,
        "dropped": dropped,
        "success": is_successful,
        "inventory": inventory,
        "csv_path": csv_path,
    }


# ---------------------------------------------------------------------------
# Acks
# ---------------------------------------------------------------------------

def ack_insert_or_extend(conn, inventory, fqdn, signal, days, reason, added_by):
    """Insert an ack, or extend an existing active one. Returns ack_until."""
    if signal not in VALID_SIGNAL_KINDS:
        raise ValueError(f"signal must be one of {sorted(VALID_SIGNAL_KINDS)}, got {signal!r}")
    if days <= 0:
        raise ValueError(f"days must be > 0, got {days}")
    fqdn = fqdn.strip().lower()
    new_until = (date.today() + timedelta(days=days)).isoformat()

    existing = conn.execute(
        """SELECT ack_id, ack_until, reason FROM acks
            WHERE fqdn = ? AND signal_kind = ?
              AND ((inventory IS NULL AND ? IS NULL) OR inventory = ?)
              AND ack_until >= ?
            ORDER BY ack_until DESC LIMIT 1""",
        (fqdn, signal, inventory, inventory, today_iso()),
    ).fetchone()

    with conn:
        if existing:
            ack_id, prior_until, prior_reason = existing
            best_until = max(prior_until, new_until)
            merged_reason = f"{prior_reason}\n— extended: {reason}"
            conn.execute(
                "UPDATE acks SET ack_until = ?, reason = ? WHERE ack_id = ?",
                (best_until, merged_reason, ack_id),
            )
            return best_until
        else:
            conn.execute(
                """INSERT INTO acks (inventory, fqdn, signal_kind, ack_until,
                                     reason, added_at, added_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (inventory, fqdn, signal, new_until, reason, now_iso(), added_by),
            )
            return new_until


def ack_list_active(conn, inventory=None):
    """Return a list of dicts for active acks (ack_until >= today)."""
    if inventory:
        rows = conn.execute(
            """SELECT ack_id, inventory, fqdn, signal_kind, ack_until, reason, added_at, added_by
                 FROM acks
                WHERE (inventory IS NULL OR inventory = ?) AND ack_until >= ?
             ORDER BY inventory, fqdn, signal_kind""",
            (inventory, today_iso()),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT ack_id, inventory, fqdn, signal_kind, ack_until, reason, added_at, added_by
                 FROM acks
                WHERE ack_until >= ?
             ORDER BY inventory IS NULL, inventory, fqdn, signal_kind""",
            (today_iso(),),
        ).fetchall()
    cols = ["ack_id", "inventory", "fqdn", "signal_kind", "ack_until", "reason", "added_at", "added_by"]
    return [dict(zip(cols, r)) for r in rows]


def ack_remove(conn, inventory, fqdn, signal):
    """Mark the matching active ack inactive (set ack_until to yesterday)."""
    fqdn = fqdn.strip().lower()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with conn:
        cur = conn.execute(
            """UPDATE acks
                  SET ack_until = ?, reason = reason || char(10) || ?
                WHERE fqdn = ? AND signal_kind = ?
                  AND ((inventory IS NULL AND ? IS NULL) OR inventory = ?)
                  AND ack_until >= ?""",
            (yesterday, "— removed via ack-remove", fqdn, signal,
             inventory, inventory, today_iso()),
        )
    return cur.rowcount


def get_active_acks_set(conn):
    """Return a set of (inventory_or_None, fqdn, signal_kind) for active acks."""
    rows = conn.execute(
        "SELECT inventory, fqdn, signal_kind FROM acks WHERE ack_until >= ?",
        (today_iso(),),
    ).fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


# ---------------------------------------------------------------------------
# Rename / Retire
# ---------------------------------------------------------------------------

def rename_inventory(conn, from_name, to_name, apply=False):
    """Migrate every row from inventory=from_name to inventory=to_name.

    Returns a dict with the counts that would (or did) move.
    """
    if from_name == to_name:
        raise ValueError("from and to are identical")
    from_row = conn.execute(
        "SELECT status FROM inventories WHERE inventory = ?", (from_name,)
    ).fetchone()
    if not from_row:
        raise ValueError(f"inventory {from_name!r} not found")
    to_row = conn.execute(
        "SELECT status FROM inventories WHERE inventory = ?", (to_name,)
    ).fetchone()
    if to_row and to_row[0] == "active":
        raise ValueError(f"target inventory {to_name!r} already exists and is active")

    counts = {
        "host_state": conn.execute(
            "SELECT COUNT(*) FROM host_state WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
        "observations": conn.execute(
            "SELECT COUNT(*) FROM observations WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
        "runs": conn.execute(
            "SELECT COUNT(*) FROM runs WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
        "actions": conn.execute(
            "SELECT COUNT(*) FROM actions WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
        "acks": conn.execute(
            "SELECT COUNT(*) FROM acks WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
        "allowlist": conn.execute(
            "SELECT COUNT(*) FROM allowlist WHERE inventory = ?", (from_name,)
        ).fetchone()[0],
    }

    if not apply:
        return {"applied": False, "counts": counts}

    try:
        conn.execute("BEGIN")
        for table in ("host_state", "observations", "runs", "actions", "acks", "allowlist"):
            conn.execute(f"UPDATE {table} SET inventory = ? WHERE inventory = ?",
                         (to_name, from_name))
        conn.execute(
            """INSERT INTO inventories (inventory, status, first_seen, last_ingest, renamed_from)
                 VALUES (?, 'active', ?, ?, ?)
               ON CONFLICT(inventory) DO UPDATE SET
                 status='active', renamed_from=excluded.renamed_from""",
            (to_name, now_iso(), now_iso(), from_name),
        )
        conn.execute(
            "UPDATE inventories SET status='renamed' WHERE inventory = ?",
            (from_name,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"applied": True, "counts": counts}


def retire_inventory(conn, inventory, apply=False):
    """Mark an inventory retired. evaluate.yml will skip it from now on."""
    row = conn.execute(
        "SELECT status FROM inventories WHERE inventory = ?", (inventory,)
    ).fetchone()
    if not row:
        raise ValueError(f"inventory {inventory!r} not found")
    if row[0] == "retired":
        raise ValueError(f"inventory {inventory!r} is already retired")
    summary = {}
    for state in ("active", "silent", "pending_remove", "returning", "intermittent", "protected"):
        summary[state] = conn.execute(
            "SELECT COUNT(*) FROM host_state WHERE inventory = ? AND current_state = ?",
            (inventory, state),
        ).fetchone()[0]
    if not apply:
        return {"applied": False, "host_states": summary}
    with conn:
        conn.execute(
            "UPDATE inventories SET status='retired', retired_at=? WHERE inventory = ?",
            (now_iso(), inventory),
        )
    return {"applied": True, "host_states": summary}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_TUNABLES = {
    "silent_threshold_days": 30,
    "min_successful_runs_before_action": 5,
    "min_run_success_pct": 85,
}


def _load_tunables_from_args(args):
    t = dict(DEFAULT_TUNABLES)
    for k in DEFAULT_TUNABLES:
        v = getattr(args, k, None)
        if v is not None:
            t[k] = v
    return t


def cmd_init(args):
    conn = connect(args.db)
    conn.close()
    print(f"initialized {args.db} (schema v{SCHEMA_VERSION})")
    return 0


def cmd_ingest(args):
    conn = connect(args.db)
    tunables = _load_tunables_from_args(args)
    try:
        entries = load_allowlist_csv(args.allowlist_csv)
    except ValueError as e:
        sys.stderr.write(f"allowlist parse error: {e}\n")
        return 2
    cache_allowlist(conn, entries)
    al_set = allowlist_lookup_set(entries)

    paths = sorted(glob(args.csv_glob))
    if not paths:
        print(f"no CSVs matched {args.csv_glob}")
        return 0

    any_error = False
    for path in paths:
        try:
            result = ingest_csv(conn, path, al_set, tunables, args.inventory)
            if result["skipped"]:
                print(f"skipped (already ingested) {path}")
            else:
                print(f"ingested {path} rows={result['rows']} dropped={result['dropped']} "
                      f"success={'yes' if result['success'] else 'no'} run_id={result['run_id']}")
        except Exception as e:
            sys.stderr.write(f"ERROR ingesting {path}: {e}\n")
            any_error = True
    conn.close()
    return 2 if any_error else 0


def cmd_ack(args):
    conn = connect(args.db)
    inv = args.inventory or None
    added_by = args.added_by or os.environ.get("USER") or os.environ.get("ANSIBLE_USER") or "unknown"
    try:
        until = ack_insert_or_extend(conn, inv, args.fqdn, args.signal,
                                      args.days, args.reason, added_by)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 5
    inv_label = inv if inv else "*"
    print(f"acked {inv_label}/{args.fqdn} signal={args.signal} until={until}")
    conn.close()
    return 0


def cmd_ack_list(args):
    conn = connect(args.db)
    acks = ack_list_active(conn, args.inventory or None)
    if not acks:
        print("no active acks")
    else:
        print(f"{'INV':<20} {'FQDN':<40} {'SIGNAL':<16} {'UNTIL':<12} {'BY':<12} REASON")
        for a in acks:
            inv = a["inventory"] or "*"
            print(f"{inv:<20.20} {a['fqdn']:<40.40} {a['signal_kind']:<16} "
                  f"{a['ack_until']:<12} {(a['added_by'] or ''):<12.12} {a['reason']}")
    conn.close()
    return 0


def cmd_ack_remove(args):
    conn = connect(args.db)
    inv = args.inventory or None
    n = ack_remove(conn, inv, args.fqdn, args.signal)
    print(f"removed {n} ack(s) for {(inv or '*')}/{args.fqdn} signal={args.signal}")
    conn.close()
    return 0 if n else 5


def cmd_rename(args):
    conn = connect(args.db)
    try:
        r = rename_inventory(conn, args.from_name, args.to_name, args.apply)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 4
    print("preview" if not r["applied"] else "applied")
    for tbl, n in r["counts"].items():
        print(f"  {tbl}: {n} rows")
    conn.close()
    return 0


def cmd_retire(args):
    conn = connect(args.db)
    try:
        r = retire_inventory(conn, args.inventory, args.apply)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 4
    print("preview" if not r["applied"] else "applied")
    for st, n in r["host_states"].items():
        print(f"  {st}: {n}")
    conn.close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True, help="path to SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create schema (idempotent)")
    p_init.set_defaults(func=cmd_init)

    p_ing = sub.add_parser("ingest", help="absorb identify-hosts CSVs into the DB")
    p_ing.add_argument("--csv-glob", required=True)
    p_ing.add_argument("--allowlist-csv")
    p_ing.add_argument("--inventory", help="override inventory name (use when filename doesn't match)")
    p_ing.add_argument("--silent-threshold-days", type=int)
    p_ing.add_argument("--min-successful-runs-before-action", type=int)
    p_ing.add_argument("--min-run-success-pct", type=int)
    p_ing.set_defaults(func=cmd_ingest)

    p_ack = sub.add_parser("ack", help="insert or extend a signal mute")
    p_ack.add_argument("--fqdn", required=True)
    p_ack.add_argument("--signal", required=True, choices=sorted(VALID_SIGNAL_KINDS))
    p_ack.add_argument("--days", type=int, required=True)
    p_ack.add_argument("--reason", required=True)
    p_ack.add_argument("--inventory", default="", help="optional; blank = all inventories")
    p_ack.add_argument("--added-by")
    p_ack.set_defaults(func=cmd_ack)

    p_al = sub.add_parser("ack-list", help="list active acks")
    p_al.add_argument("--inventory", default="")
    p_al.set_defaults(func=cmd_ack_list)

    p_ar = sub.add_parser("ack-remove", help="expire an active ack")
    p_ar.add_argument("--fqdn", required=True)
    p_ar.add_argument("--signal", required=True, choices=sorted(VALID_SIGNAL_KINDS))
    p_ar.add_argument("--inventory", default="")
    p_ar.set_defaults(func=cmd_ack_remove)

    p_rn = sub.add_parser("rename-inventory", help="migrate state to a new inventory name")
    p_rn.add_argument("--from", dest="from_name", required=True)
    p_rn.add_argument("--to", dest="to_name", required=True)
    p_rn.add_argument("--apply", action="store_true")
    p_rn.set_defaults(func=cmd_rename)

    p_rt = sub.add_parser("retire-inventory", help="mark an inventory retired")
    p_rt.add_argument("--inventory", required=True)
    p_rt.add_argument("--apply", action="store_true")
    p_rt.set_defaults(func=cmd_retire)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
