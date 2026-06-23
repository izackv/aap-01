"""Add one or more fqdns to an AAP inventory, with prior-history audit.

The audit hook: for each fqdn, query the lifecycle DB for past
disable/delete actions across ALL inventories and print them before adding.
That's how an operator catches "we removed this two months ago, why is it
coming back?" — see SPEC §5.8.

CLI:
    python3 add_hosts.py --aap-url ... --aap-token ... --inventory NAME \
        ( --fqdn FQDN | --from-file PATH ) \
        [--db-path ...] [--no-reenable] [--dry-run]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lifecycle_db import connect, now_iso
from aap_client import (
    AAPError, inventory_id_by_name, host_lookup, add_host, enable_host,
)


def _read_fqdns(args):
    if args.fqdn and args.from_file:
        sys.stderr.write("pass exactly one of --fqdn or --from-file\n")
        return None
    if args.fqdn:
        return [args.fqdn.strip().lower()]
    if not args.from_file:
        sys.stderr.write("--fqdn or --from-file required\n")
        return None
    if not os.path.exists(args.from_file):
        sys.stderr.write(f"file not found: {args.from_file}\n")
        return None
    fqdns = []
    with open(args.from_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fqdns.append(line.lower())
    return fqdns


def _prior_history(conn, fqdn):
    """Return a list of audit-worthy actions for this fqdn across all inventories."""
    rows = conn.execute(
        """SELECT inventory, action_kind, executed_at, run_id
             FROM actions
            WHERE fqdn = ? AND executed = 1
              AND action_kind IN ('disable-host', 'delete-host')
         ORDER BY executed_at""",
        (fqdn,),
    ).fetchall()
    return [{"inventory": r[0], "kind": r[1], "at": (r[2] or "")[:10], "run_id": r[3]}
            for r in rows]


def _format_history(history):
    if not history:
        return "no prior history"
    return "; ".join(
        f"{h['kind'].replace('-host', '')} {h['at']} in {h['inventory']} (run {h['run_id']})"
        for h in history
    )


def _record(conn, inventory, fqdn, kind, executed, reason, aap_host_id, aap_response):
    conn.execute(
        """INSERT INTO actions
             (run_id, inventory, fqdn, action_kind, mode, dry_run, executed,
              executed_at, reason, triggering_runs, aap_host_id, aap_response)
           VALUES (0, ?, ?, ?, NULL, 0, ?, ?, ?, '[]', ?, ?)""",
        (inventory, fqdn, kind, 1 if executed else 0,
         now_iso() if executed else None, reason,
         aap_host_id, aap_response),
    )
    conn.commit()


def run(args):
    fqdns = _read_fqdns(args)
    if fqdns is None:
        return 3
    if not fqdns:
        print("no fqdns to process")
        return 0

    conn = connect(args.db_path) if args.db_path else None

    inv_id = None
    if not args.dry_run:
        try:
            inv_id = inventory_id_by_name(args.aap_url, args.aap_token, args.inventory)
        except (AAPError, LookupError) as e:
            sys.stderr.write(f"resolve inventory: {e}\n")
            return 3

    any_error = False
    for fqdn in fqdns:
        history = _prior_history(conn, fqdn) if conn else []
        prior = _format_history(history)
        marker = "⚠ " if history else ""

        if args.dry_run:
            print(f"DRYRUN     {fqdn} → {args.inventory}: {marker}{prior}; would POST")
            continue

        # Lookup current state in AAP
        try:
            existing = host_lookup(args.aap_url, args.aap_token, inv_id, fqdn)
        except (AAPError, LookupError) as e:
            print(f"ERROR      {fqdn} → {args.inventory}: lookup failed: {e}")
            if conn:
                _record(conn, args.inventory, fqdn, "add-host", False,
                         f"lookup-failed; history: {prior}", None, str(e)[:300])
            any_error = True
            continue

        if existing is None:
            try:
                resp = add_host(args.aap_url, args.aap_token, inv_id, fqdn)
                new_id = resp.get("id")
                print(f"ADD        {fqdn} → {args.inventory}: {marker}{prior}; created (id={new_id})")
                if conn:
                    _record(conn, args.inventory, fqdn, "add-host", True,
                             f"added; history: {prior}", new_id, f"HTTP {resp.get('_status')}")
            except AAPError as e:
                print(f"ERROR      {fqdn} → {args.inventory}: AAP {e.status}: {e.body[:120]}")
                if conn:
                    _record(conn, args.inventory, fqdn, "add-host", False,
                             f"api-error; history: {prior}", None, str(e)[:300])
                any_error = True
        else:
            host_id = existing["id"]
            enabled = existing.get("enabled", True)
            if enabled:
                print(f"SKIP       {fqdn} → {args.inventory}: already exists (id={host_id}, enabled=true)")
            elif args.no_reenable:
                print(f"SKIP       {fqdn} → {args.inventory}: exists, disabled, --no-reenable")
            else:
                try:
                    resp = enable_host(args.aap_url, args.aap_token, host_id)
                    print(f"REENABLE   {fqdn} → {args.inventory}: was disabled; "
                          f"re-enabled (id={host_id})")
                    if conn:
                        _record(conn, args.inventory, fqdn, "reenable-host", True,
                                 f"re-enabled via add-hosts; history: {prior}",
                                 host_id, f"HTTP {resp.get('_status')}")
                except AAPError as e:
                    print(f"ERROR      {fqdn} → {args.inventory}: re-enable failed: {e}")
                    any_error = True

    if conn:
        conn.close()
    return 3 if any_error else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aap-url", required=True)
    p.add_argument("--aap-token", required=True)
    p.add_argument("--inventory", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--fqdn")
    g.add_argument("--from-file")
    p.add_argument("--db-path", default="",
                   help="lifecycle DB; required for prior-history audit (strongly recommended)")
    p.add_argument("--no-reenable", action="store_true",
                   help="if host exists but is disabled, leave it disabled")
    p.add_argument("--dry-run", action="store_true",
                   help="print decisions and history; do not call AAP")
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
