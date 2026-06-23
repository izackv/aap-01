"""Disable or delete hosts in AAP per the most recent to_remove.csv.

Fail-closed: refuses to run if the most recent evaluate produced aborted.md.
Per-host idempotent: re-runs skip hosts already in the target state. Logs
every attempt to the `actions` table and to actions.csv in the report dir.

CLI:
    python3 act.py --db ... --report-dir ... --inventory ... \
        --aap-url ... --aap-token ... \
        [--apply] [--mode disable|delete] [--keep-csv-days N] [--csv-glob ...]
"""

import argparse
import csv
import glob as globmod
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lifecycle_db import connect, now_iso, today_iso
from aap_client import (
    AAPError, inventory_id_by_name, host_lookup,
    disable_host, delete_host,
)


ACTIONS_FIELDS = [
    "action_id", "action_kind", "fqdn", "inventory",
    "executed", "executed_at", "dry_run", "mode",
    "reason", "aap_host_id", "aap_response",
]


def _latest_inv_report_dir(report_dir, inventory):
    base = os.path.join(report_dir, inventory)
    if not os.path.isdir(base):
        return None
    dates = sorted(os.listdir(base))
    return os.path.join(base, dates[-1]) if dates else None


def _read_to_remove(report_dir, inventory):
    latest = _latest_inv_report_dir(report_dir, inventory)
    if not latest:
        return None, None
    path = os.path.join(latest, "to_remove.csv")
    if not os.path.exists(path):
        return latest, None
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return latest, rows


def _latest_run_id(conn, inventory):
    row = conn.execute(
        "SELECT run_id FROM runs WHERE inventory = ? ORDER BY run_id DESC LIMIT 1",
        (inventory,),
    ).fetchone()
    return row[0] if row else None


def _record_action(conn, run_id, inventory, fqdn, kind, mode, dry_run, executed,
                   reason, aap_host_id, aap_response, triggering_runs):
    cur = conn.execute(
        """INSERT INTO actions
             (run_id, inventory, fqdn, action_kind, mode, dry_run,
              executed, executed_at, reason, triggering_runs,
              aap_host_id, aap_response)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, inventory, fqdn, kind, mode, 1 if dry_run else 0,
         1 if executed else 0, now_iso() if executed else None,
         reason, json.dumps(triggering_runs or []),
         aap_host_id, aap_response),
    )
    conn.commit()
    return cur.lastrowid


def _prune_evidence_safe(conn, csv_glob, keep_csv_days, dry_run):
    """Delete identify-hosts CSVs older than keep_csv_days, except evidence.

    Evidence = any CSV path referenced by a `runs` row that's referenced by
    an `actions` row.
    """
    if not csv_glob or keep_csv_days <= 0:
        return []
    evidence = {row[0] for row in conn.execute(
        """SELECT DISTINCT r.csv_path FROM runs r
              JOIN actions a ON a.run_id = r.run_id"""
    ).fetchall()}
    paths = sorted(globmod.glob(csv_glob))
    pruned = []
    import datetime as dt
    cutoff = dt.date.today() - dt.timedelta(days=keep_csv_days)
    for p in paths:
        if p in evidence:
            continue
        try:
            mtime = dt.date.fromtimestamp(os.path.getmtime(p))
        except OSError:
            continue
        if mtime < cutoff:
            if dry_run:
                pruned.append((p, "would-delete"))
            else:
                try:
                    os.remove(p)
                    pruned.append((p, "deleted"))
                except OSError as e:
                    pruned.append((p, f"error:{e}"))
    return pruned


def act(args):
    conn = connect(args.db)
    inventory = args.inventory

    # Fail-closed if the latest evaluate aborted.
    latest_dir = _latest_inv_report_dir(args.report_dir, inventory)
    if latest_dir is None:
        sys.stderr.write(f"no reports yet for inventory {inventory!r}; run evaluate first\n")
        return 0
    if os.path.exists(os.path.join(latest_dir, "aborted.md")):
        sys.stderr.write(
            f"most recent evaluate for {inventory!r} was aborted by a sanity gate; "
            f"see {latest_dir}/aborted.md. Refusing to act.\n"
        )
        return 0

    _, rows = _read_to_remove(args.report_dir, inventory)
    if not rows:
        print(f"{inventory}: no to_remove.csv rows; nothing to do")
        return 0

    if args.mode == "delete":
        if not args.apply:
            print("delete mode requires --apply (and will respect max-remove-per-run-abs)")
        if len(rows) > args.max_remove_per_run_abs:
            sys.stderr.write(
                f"REFUSE: {len(rows)} candidates exceeds --max-remove-per-run-abs="
                f"{args.max_remove_per_run_abs} for delete mode\n"
            )
            return 0

    run_id = _latest_run_id(conn, inventory) or 0

    try:
        inv_id = inventory_id_by_name(args.aap_url, args.aap_token, inventory) if args.apply else None
    except (AAPError, LookupError) as e:
        sys.stderr.write(f"could not resolve AAP inventory id for {inventory!r}: {e}\n")
        return 3

    any_error = False
    actions_csv = os.path.join(latest_dir, "actions.csv")
    actions_rows = []

    for row in rows:
        fqdn = row["fqdn"]
        triggering = []
        try:
            triggering = json.loads(row.get("last_run_ids") or "[]")
        except json.JSONDecodeError:
            pass

        host_info = None
        if args.apply:
            try:
                host_info = host_lookup(args.aap_url, args.aap_token, inv_id, fqdn)
            except (AAPError, LookupError) as e:
                _record_action(conn, run_id, inventory, fqdn,
                                "disable-host" if args.mode == "disable" else "delete-host",
                                args.mode, dry_run=False, executed=False,
                                reason="lookup-failed",
                                aap_host_id=None, aap_response=str(e),
                                triggering_runs=triggering)
                actions_rows.append({"fqdn": fqdn, "action_kind": "lookup-error",
                                     "inventory": inventory, "executed": "0",
                                     "dry_run": "0", "mode": args.mode,
                                     "reason": "lookup-failed",
                                     "aap_response": str(e)})
                any_error = True
                print(f"ERROR  {fqdn}: lookup failed: {e}")
                continue

        if not args.apply:
            kind = "disable-host" if args.mode == "disable" else "delete-host"
            aid = _record_action(conn, run_id, inventory, fqdn, kind, args.mode,
                                  dry_run=True, executed=False,
                                  reason="dry-run",
                                  aap_host_id=None, aap_response="dry-run",
                                  triggering_runs=triggering)
            actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                 "action_kind": kind, "executed": "0", "dry_run": "1",
                                 "mode": args.mode, "reason": "dry-run", "aap_host_id": "",
                                 "aap_response": "dry-run", "executed_at": ""})
            print(f"DRYRUN {fqdn}: {kind}")
            continue

        # Apply-true path
        if args.mode == "disable":
            if host_info is None:
                msg = "host not found in AAP — already gone"
                aid = _record_action(conn, run_id, inventory, fqdn, "disable-host",
                                      "disable", dry_run=False, executed=False,
                                      reason=msg, aap_host_id=None,
                                      aap_response="not-found", triggering_runs=triggering)
                actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "disable-host", "executed": "0",
                                     "dry_run": "0", "mode": "disable", "reason": msg,
                                     "aap_host_id": "", "aap_response": "not-found",
                                     "executed_at": ""})
                print(f"SKIP   {fqdn}: not-found")
                continue
            if not host_info.get("enabled", True):
                aid = _record_action(conn, run_id, inventory, fqdn, "disable-host",
                                      "disable", dry_run=False, executed=True,
                                      reason="already-disabled", aap_host_id=host_info["id"],
                                      aap_response="already-disabled", triggering_runs=triggering)
                actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "disable-host", "executed": "1",
                                     "dry_run": "0", "mode": "disable",
                                     "reason": "already-disabled",
                                     "aap_host_id": str(host_info["id"]),
                                     "aap_response": "already-disabled",
                                     "executed_at": now_iso()})
                print(f"SKIP   {fqdn}: already-disabled (id={host_info['id']})")
                continue
            try:
                resp = disable_host(args.aap_url, args.aap_token, host_info["id"])
                aid = _record_action(conn, run_id, inventory, fqdn, "disable-host",
                                      "disable", dry_run=False, executed=True,
                                      reason="silent-threshold",
                                      aap_host_id=host_info["id"],
                                      aap_response=f"HTTP {resp.get('_status')}",
                                      triggering_runs=triggering)
                actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "disable-host", "executed": "1",
                                     "dry_run": "0", "mode": "disable",
                                     "reason": "silent-threshold",
                                     "aap_host_id": str(host_info["id"]),
                                     "aap_response": f"HTTP {resp.get('_status')}",
                                     "executed_at": now_iso()})
                print(f"OK     {fqdn}: disabled (id={host_info['id']})")
            except AAPError as e:
                _record_action(conn, run_id, inventory, fqdn, "disable-host",
                                "disable", dry_run=False, executed=False,
                                reason="api-error",
                                aap_host_id=host_info["id"],
                                aap_response=str(e)[:500], triggering_runs=triggering)
                actions_rows.append({"fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "disable-host", "executed": "0",
                                     "dry_run": "0", "mode": "disable", "reason": "api-error",
                                     "aap_host_id": str(host_info["id"]),
                                     "aap_response": str(e)[:200], "executed_at": ""})
                print(f"ERROR  {fqdn}: {e}")
                any_error = True

        elif args.mode == "delete":
            if host_info is None:
                aid = _record_action(conn, run_id, inventory, fqdn, "delete-host",
                                      "delete", dry_run=False, executed=False,
                                      reason="already-gone", aap_host_id=None,
                                      aap_response="not-found", triggering_runs=triggering)
                actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "delete-host", "executed": "0",
                                     "dry_run": "0", "mode": "delete", "reason": "already-gone",
                                     "aap_host_id": "", "aap_response": "not-found",
                                     "executed_at": ""})
                print(f"SKIP   {fqdn}: already-gone")
                continue
            try:
                resp = delete_host(args.aap_url, args.aap_token, host_info["id"])
                aid = _record_action(conn, run_id, inventory, fqdn, "delete-host",
                                      "delete", dry_run=False, executed=True,
                                      reason="silent-threshold (delete mode)",
                                      aap_host_id=host_info["id"],
                                      aap_response=f"HTTP {resp.get('_status')}",
                                      triggering_runs=triggering)
                actions_rows.append({"action_id": aid, "fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "delete-host", "executed": "1",
                                     "dry_run": "0", "mode": "delete",
                                     "reason": "silent-threshold (delete mode)",
                                     "aap_host_id": str(host_info["id"]),
                                     "aap_response": f"HTTP {resp.get('_status')}",
                                     "executed_at": now_iso()})
                print(f"OK     {fqdn}: deleted (was id={host_info['id']})")
            except AAPError as e:
                _record_action(conn, run_id, inventory, fqdn, "delete-host",
                                "delete", dry_run=False, executed=False,
                                reason="api-error",
                                aap_host_id=host_info["id"],
                                aap_response=str(e)[:500], triggering_runs=triggering)
                actions_rows.append({"fqdn": fqdn, "inventory": inventory,
                                     "action_kind": "delete-host", "executed": "0",
                                     "dry_run": "0", "mode": "delete", "reason": "api-error",
                                     "aap_host_id": str(host_info["id"]),
                                     "aap_response": str(e)[:200], "executed_at": ""})
                print(f"ERROR  {fqdn}: {e}")
                any_error = True

    # Prune old CSVs (after the host actions so evidence references are current).
    pruned = _prune_evidence_safe(conn, args.csv_glob, args.keep_csv_days,
                                    dry_run=not args.apply)
    for path, status in pruned:
        aid = _record_action(conn, run_id, inventory, None, "prune-csv", None,
                              dry_run=(not args.apply),
                              executed=(status == "deleted"),
                              reason=status, aap_host_id=None, aap_response=path,
                              triggering_runs=[])
        actions_rows.append({"action_id": aid, "fqdn": "", "inventory": "",
                             "action_kind": "prune-csv", "executed":
                             "1" if status == "deleted" else "0",
                             "dry_run": "0" if args.apply else "1",
                             "mode": "", "reason": status,
                             "aap_host_id": "", "aap_response": path,
                             "executed_at": now_iso() if status == "deleted" else ""})

    # Write actions.csv
    os.makedirs(latest_dir, exist_ok=True)
    with open(actions_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ACTIONS_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in actions_rows:
            w.writerow({k: r.get(k, "") for k in ACTIONS_FIELDS})

    conn.close()
    return 3 if any_error else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--report-dir", required=True)
    p.add_argument("--inventory", required=True)
    p.add_argument("--aap-url", default="")
    p.add_argument("--aap-token", default="")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--mode", choices=("disable", "delete"), default="disable")
    p.add_argument("--keep-csv-days", type=int, default=90)
    p.add_argument("--csv-glob", default="")
    p.add_argument("--max-remove-per-run-abs", type=int, default=50)
    args = p.parse_args(argv)
    if args.apply and not (args.aap_url and args.aap_token):
        sys.stderr.write("--apply requires --aap-url and --aap-token\n")
        return 3
    return act(args)


if __name__ == "__main__":
    sys.exit(main())
