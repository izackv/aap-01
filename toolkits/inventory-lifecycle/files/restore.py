"""Re-enable hosts disabled by an earlier act.yml run.

Reads a prior actions.csv. For each row with action_kind=disable-host and
executed=1, PATCHes the host to enabled=true in AAP. Delete-host rows are
flagged with a warning — not auto-restored (we don't archive the host record).

CLI:
    python3 restore.py --db ... --from-actions-csv ... \
        --aap-url ... --aap-token ... [--apply]
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lifecycle_db import connect, now_iso
from aap_client import AAPError, inventory_id_by_name, host_lookup, enable_host


def restore(args):
    if not os.path.exists(args.from_actions_csv):
        sys.stderr.write(f"file not found: {args.from_actions_csv}\n")
        return 3
    conn = connect(args.db)

    inv_id_cache = {}
    any_error = False
    warnings_delete = []

    with open(args.from_actions_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            kind = row.get("action_kind", "")
            executed = row.get("executed", "0") == "1"
            inventory = row.get("inventory", "")
            fqdn = row.get("fqdn", "")
            if kind == "delete-host" and executed:
                warnings_delete.append((inventory, fqdn))
                continue
            if kind != "disable-host" or not executed:
                continue
            if not args.apply:
                print(f"DRYRUN re-enable {inventory}/{fqdn}")
                continue

            if inventory not in inv_id_cache:
                try:
                    inv_id_cache[inventory] = inventory_id_by_name(args.aap_url, args.aap_token, inventory)
                except (AAPError, LookupError) as e:
                    sys.stderr.write(f"ERROR  resolve inv {inventory!r}: {e}\n")
                    any_error = True
                    continue
            inv_id = inv_id_cache[inventory]
            try:
                hi = host_lookup(args.aap_url, args.aap_token, inv_id, fqdn)
                if hi is None:
                    print(f"SKIP   {inventory}/{fqdn}: host not present (was the delete one?)")
                    continue
                resp = enable_host(args.aap_url, args.aap_token, hi["id"])
                conn.execute(
                    """INSERT INTO actions
                         (run_id, inventory, fqdn, action_kind, mode, dry_run,
                          executed, executed_at, reason, triggering_runs,
                          aap_host_id, aap_response)
                       VALUES (0, ?, ?, 'reenable-host', NULL, 0, 1, ?, ?, ?, ?, ?)""",
                    (inventory, fqdn, now_iso(), "manual-restore",
                     json.dumps([row.get("action_id", "")]),
                     hi["id"], f"HTTP {resp.get('_status')}"),
                )
                conn.commit()
                print(f"OK     {inventory}/{fqdn}: re-enabled (id={hi['id']})")
            except AAPError as e:
                sys.stderr.write(f"ERROR  {inventory}/{fqdn}: {e}\n")
                any_error = True

    if warnings_delete:
        print("\nWARNING: these were action_kind=delete-host; restore.py cannot re-create them.")
        print("         Re-add manually via add-hosts.yml or the AAP UI:")
        for inv, f in warnings_delete:
            print(f"  - {inv}/{f}")

    conn.close()
    return 3 if any_error else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--from-actions-csv", required=True)
    p.add_argument("--aap-url", default="")
    p.add_argument("--aap-token", default="")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    if args.apply and not (args.aap_url and args.aap_token):
        sys.stderr.write("--apply requires --aap-url and --aap-token\n")
        return 3
    return restore(args)


if __name__ == "__main__":
    sys.exit(main())
