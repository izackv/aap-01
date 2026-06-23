"""Evaluate hygiene rules and write per-inventory + cross-inventory reports.

Read-only against the DB. See SPEC §3 (rules), §4 (output formats), §5.2
(playbook contract). All CSVs sorted by fqdn for deterministic diffs.

CLI:
    python3 evaluate_rules.py --db ... --report-dir ... [--inventory X]
"""

import argparse
import csv
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lifecycle_db import (
    connect, days_between, get_active_acks_set, today_iso, parse_iso_date,
    DEFAULT_TUNABLES,
)


def _active_inventories(conn):
    rows = conn.execute(
        "SELECT inventory FROM inventories WHERE status = 'active' ORDER BY inventory"
    ).fetchall()
    return [r[0] for r in rows]


def _latest_run(conn, inventory):
    return conn.execute(
        """SELECT run_id, scan_date, csv_path, total_hosts, scanned_hosts, is_successful_run
             FROM runs
            WHERE inventory = ?
         ORDER BY run_id DESC LIMIT 1""",
        (inventory,),
    ).fetchone()


def _count_active_hosts(conn, inventory):
    return conn.execute(
        """SELECT COUNT(*) FROM host_state
            WHERE inventory = ? AND current_state IN ('active', 'returning')""",
        (inventory,),
    ).fetchone()[0]


def _ack_active_for(active_acks, inventory, fqdn, signal):
    return (inventory, fqdn, signal) in active_acks or (None, fqdn, signal) in active_acks


def _also_active_in(conn, fqdn, except_inventory):
    """Other inventories where this fqdn is currently active/returning."""
    rows = conn.execute(
        """SELECT inventory FROM host_state
            WHERE fqdn = ? AND inventory != ?
              AND current_state IN ('active', 'returning')
         ORDER BY inventory""",
        (fqdn, except_inventory),
    ).fetchall()
    return [r[0] for r in rows]


def _already_removed_from(conn, fqdn, except_inventory):
    """List of {inventory, kind, at, run_id} for prior executed removals in OTHER invs."""
    rows = conn.execute(
        """SELECT inventory, action_kind, executed_at, run_id
             FROM actions
            WHERE fqdn = ? AND inventory != ?
              AND executed = 1
              AND action_kind IN ('disable-host', 'delete-host')
         ORDER BY executed_at""",
        (fqdn, except_inventory),
    ).fetchall()
    result = []
    for inv, kind, at, rid in rows:
        short_kind = "disable" if kind == "disable-host" else "delete"
        short_at = (at or "")[:10]
        result.append({"inventory": inv, "kind": short_kind, "at": short_at, "run_id": rid})
    return result


def _format_already_removed(items):
    """Render the already_removed_from list as semicolon-joined inv:kind:date."""
    if not items:
        return ""
    return ";".join(f"{i['inventory']}:{i['kind']}:{i['at']}" for i in items)


def _silent_streak_run_ids(conn, inventory, fqdn, min_runs):
    """Return up to last N run_ids where this host's state was non-up in this inventory."""
    rows = conn.execute(
        """SELECT run_id FROM observations
            WHERE inventory = ? AND fqdn = ? AND state != 'up'
         ORDER BY run_id DESC LIMIT ?""",
        (inventory, fqdn, min_runs),
    ).fetchall()
    return [r[0] for r in rows]


def _compute_intermittent_flips(conn, inventory, fqdn, window_days):
    """Count up↔down transitions in the trailing window."""
    cutoff = None
    today = date.today()
    rows = conn.execute(
        """SELECT o.state, r.scan_date
             FROM observations o JOIN runs r ON r.run_id = o.run_id
            WHERE o.inventory = ? AND o.fqdn = ?
         ORDER BY r.scan_date""",
        (inventory, fqdn),
    ).fetchall()
    flips = 0
    last_relevant = None
    for state, scan_date in rows:
        d = parse_iso_date(scan_date)
        if d is None or (today - d).days > window_days:
            continue
        if state == "up":
            this = "up"
        elif state in ("down", "absent"):
            this = "down"
        else:
            continue  # auth_failed / ssh_blocked / online_unmanaged / excluded don't count
        if last_relevant and last_relevant != this:
            flips += 1
        last_relevant = this
    last_up = conn.execute(
        """SELECT MAX(r.scan_date)
             FROM observations o JOIN runs r ON r.run_id = o.run_id
            WHERE o.inventory = ? AND o.fqdn = ? AND o.state = 'up'""",
        (inventory, fqdn),
    ).fetchone()[0]
    last_down = conn.execute(
        """SELECT MAX(r.scan_date)
             FROM observations o JOIN runs r ON r.run_id = o.run_id
            WHERE o.inventory = ? AND o.fqdn = ? AND o.state IN ('down', 'absent')""",
        (inventory, fqdn),
    ).fetchone()[0]
    return flips, last_up, last_down, rows


# ---------------------------------------------------------------------------
# Candidate computation
# ---------------------------------------------------------------------------

def candidates_to_remove(conn, inventory, tunables, active_acks):
    """List of dicts for the to_remove.csv report."""
    rows = conn.execute(
        """SELECT fqdn, last_seen_up, last_ipv4, last_mac, last_mac_vendor,
                  last_os, last_error_class, consecutive_silent_runs
             FROM host_state
            WHERE inventory = ? AND current_state = 'pending_remove'""",
        (inventory,),
    ).fetchall()
    today = date.today().isoformat()
    out = []
    for r in rows:
        fqdn = r[0]
        if _ack_active_for(active_acks, inventory, fqdn, "pending_remove"):
            continue
        if _ack_active_for(active_acks, inventory, fqdn, "silent"):
            continue
        days_silent = days_between(r[1], today) or 0
        also = _also_active_in(conn, fqdn, inventory)
        removed = _already_removed_from(conn, fqdn, inventory)
        run_ids = _silent_streak_run_ids(conn, inventory, fqdn,
                                          tunables["min_successful_runs_before_action"])
        out.append({
            "fqdn": fqdn,
            "inventory": inventory,
            "last_seen_up": r[1] or "",
            "days_silent": days_silent,
            "last_ipv4": r[2] or "",
            "last_mac": r[3] or "",
            "last_mac_vendor": r[4] or "",
            "last_os": r[5] or "",
            "last_error_class": r[6] or "",
            "also_active_in": json.dumps(also),
            "already_removed_from": _format_already_removed(removed),
            "last_run_ids": json.dumps(run_ids),
        })
    out.sort(key=lambda x: (x["fqdn"], x["inventory"]))
    return out


def candidates_returned(conn, inventory, tunables, active_acks):
    """Hosts whose current_state is 'returning' this round."""
    rows = conn.execute(
        """SELECT fqdn, last_observed, last_ipv4, last_mac, last_mac_vendor, last_os
             FROM host_state
            WHERE inventory = ? AND current_state = 'returning'""",
        (inventory,),
    ).fetchall()
    out = []
    for r in rows:
        fqdn = r[0]
        if _ack_active_for(active_acks, inventory, fqdn, "returned"):
            continue
        # Find prior "up" observation for the diff.
        prior = conn.execute(
            """SELECT r.scan_date, o.ipv4, o.mac, o.mac_vendor, o.os
                 FROM observations o JOIN runs r ON r.run_id = o.run_id
                WHERE o.inventory = ? AND o.fqdn = ? AND o.state = 'up'
             ORDER BY r.scan_date DESC LIMIT 1 OFFSET 1""",
            (inventory, fqdn),
        ).fetchone()
        prior_seen, p_ipv4, p_mac, p_vendor, p_os = (prior if prior else (None, None, None, None, None))
        cur_seen = r[1]
        cur_ipv4, cur_mac, cur_vendor, cur_os = r[2], r[3], r[4], r[5]
        days_absent = days_between(prior_seen, cur_seen) if prior_seen else None
        also = _also_active_in(conn, fqdn, inventory)
        removed = _already_removed_from(conn, fqdn, inventory)
        out.append({
            "fqdn": fqdn,
            "inventory": inventory,
            "days_absent": days_absent if days_absent is not None else "",
            "prior_last_seen_up": prior_seen or "",
            "current_seen": cur_seen or "",
            "prior_ipv4": p_ipv4 or "",
            "current_ipv4": cur_ipv4 or "",
            "ipv4_changed": str(bool(p_ipv4 and cur_ipv4 and p_ipv4 != cur_ipv4)).lower(),
            "prior_mac": p_mac or "",
            "current_mac": cur_mac or "",
            "mac_changed": str(bool(p_mac and cur_mac and p_mac != cur_mac)).lower(),
            "prior_mac_vendor": p_vendor or "",
            "current_mac_vendor": cur_vendor or "",
            "mac_vendor_changed": str(bool(p_vendor and cur_vendor and p_vendor != cur_vendor)).lower(),
            "prior_os": p_os or "",
            "current_os": cur_os or "",
            "os_changed": str(bool(p_os and cur_os and p_os != cur_os)).lower(),
            "also_active_in": json.dumps(also),
            "already_removed_from": _format_already_removed(removed),
        })
    out.sort(key=lambda x: (x["fqdn"], x["inventory"]))
    return out


def candidates_intermittent(conn, inventory, tunables, active_acks):
    """Hosts with ≥ min_flips up↔down transitions in trailing window."""
    window = tunables["intermittent_window_days"]
    min_flips = tunables["intermittent_min_flips"]
    fqdns = conn.execute(
        "SELECT DISTINCT fqdn FROM observations WHERE inventory = ?",
        (inventory,),
    ).fetchall()
    out = []
    for (fqdn,) in fqdns:
        if _ack_active_for(active_acks, inventory, fqdn, "intermittent"):
            continue
        flips, last_up, last_down, rows = _compute_intermittent_flips(
            conn, inventory, fqdn, window
        )
        if flips < min_flips:
            continue
        state = conn.execute(
            "SELECT current_state FROM host_state WHERE inventory = ? AND fqdn = ?",
            (inventory, fqdn),
        ).fetchone()
        # Compact observation sequence: U/D within window only.
        today = date.today()
        compact = []
        for state_, scan_date in rows:
            d = parse_iso_date(scan_date)
            if d is None or (today - d).days > window:
                continue
            if state_ == "up":
                compact.append("U")
            elif state_ in ("down", "absent"):
                compact.append("D")
        out.append({
            "fqdn": fqdn,
            "inventory": inventory,
            "window_days": window,
            "flips": flips,
            "last_seen_up": last_up or "",
            "last_seen_down": last_down or "",
            "current_state": (state[0] if state else ""),
            "observation_sequence": " ".join(compact),
        })
    out.sort(key=lambda x: (x["fqdn"], x["inventory"]))
    return out


# ---------------------------------------------------------------------------
# Sanity gates
# ---------------------------------------------------------------------------

def gate_decisions(conn, inventory, candidates, tunables):
    """Run gates G1/G2/G3 over the to_remove candidates. Returns dict."""
    run = _latest_run(conn, inventory)
    is_successful = bool(run and run[5])
    active = _count_active_hosts(conn, inventory)
    n = len(candidates)
    pct_cap = tunables["max_remove_per_run_pct"]
    abs_cap = tunables["max_remove_per_run_abs"]
    g1_ok = is_successful
    g2_ok = (active == 0) or (n * 100 <= active * pct_cap)
    g3_ok = n <= abs_cap
    return {
        "g1_ok": g1_ok,
        "g2_ok": g2_ok,
        "g3_ok": g3_ok,
        "any_failed": not (g1_ok and g2_ok and g3_ok),
        "n_candidates": n,
        "active_hosts": active,
        "pct_cap": pct_cap,
        "abs_cap": abs_cap,
        "run_successful": is_successful,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def _write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


TO_REMOVE_FIELDS = [
    "fqdn", "inventory", "last_seen_up", "days_silent",
    "last_ipv4", "last_mac", "last_mac_vendor", "last_os", "last_error_class",
    "also_active_in", "already_removed_from", "last_run_ids",
]

RETURNED_FIELDS = [
    "fqdn", "inventory", "days_absent", "prior_last_seen_up", "current_seen",
    "prior_ipv4", "current_ipv4", "ipv4_changed",
    "prior_mac", "current_mac", "mac_changed",
    "prior_mac_vendor", "current_mac_vendor", "mac_vendor_changed",
    "prior_os", "current_os", "os_changed",
    "also_active_in", "already_removed_from",
]

INTERMITTENT_FIELDS = [
    "fqdn", "inventory", "window_days", "flips",
    "last_seen_up", "last_seen_down", "current_state", "observation_sequence",
]


def _state_counts(conn, inventory):
    rows = conn.execute(
        """SELECT current_state, COUNT(*) FROM host_state
            WHERE inventory = ? GROUP BY current_state""",
        (inventory,),
    ).fetchall()
    return dict(rows)


def write_summary(path, inventory, run, counts, gates, acks):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run_id = run[0] if run else "—"
    scan_date = run[1] if run else "—"
    csv_path = run[2] if run else "—"
    total = run[3] if run else 0
    scanned = run[4] if run else 0
    is_succ = "yes" if (run and run[5]) else "no"
    lines = []
    lines.append(f"# Inventory lifecycle — {inventory} — {today_iso()}")
    lines.append("")
    lines.append(f"Run ID: {run_id}    (CSV: {csv_path})")
    lines.append(f"Total hosts known: {total}")
    lines.append(f"Observed this run: {scanned}")
    lines.append(f"Run is_successful: {is_succ}")
    lines.append("")
    lines.append("## Counts by derived state")
    lines.append("")
    lines.append("| State          | Count |")
    lines.append("|----------------|-------|")
    for st in ("active", "silent", "pending_remove", "returning", "intermittent", "protected"):
        lines.append(f"| {st:<14} | {counts.get(st, 0):<5} |")
    lines.append("")
    lines.append("## Sanity gates")
    lines.append("")
    lines.append(f"- G1 latest run successful: {'pass' if gates['g1_ok'] else 'FAIL'}")
    lines.append(f"- G2 percent cap ({gates['pct_cap']}%): "
                 f"{'pass' if gates['g2_ok'] else 'FAIL'}  "
                 f"({gates['n_candidates']}/{gates['active_hosts']})")
    lines.append(f"- G3 absolute cap ({gates['abs_cap']}): "
                 f"{'pass' if gates['g3_ok'] else 'FAIL'}  ({gates['n_candidates']})")
    lines.append("")
    lines.append("## Active acks (this inventory)")
    lines.append("")
    if acks:
        lines.append("| fqdn | signal | ack_until | reason | added_by |")
        lines.append("|------|--------|-----------|--------|----------|")
        for a in acks:
            reason = (a["reason"] or "").replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(f"| {a['fqdn']} | {a['signal_kind']} | {a['ack_until']} | "
                         f"{reason} | {a['added_by'] or ''} |")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Files this run")
    lines.append("")
    if gates["any_failed"]:
        lines.append("- [aborted.md](aborted.md) — sanity gate failed; no category CSVs were written")
    else:
        lines.append("- [to_remove.csv](to_remove.csv)")
        lines.append("- [returned.csv](returned.csv)")
        lines.append("- [intermittent.csv](intermittent.csv)")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_aborted(path, inventory, run, gates):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    failed = []
    if not gates["g1_ok"]:
        failed.append(f"G1: latest run was not successful")
    if not gates["g2_ok"]:
        failed.append(f"G2: {gates['n_candidates']} > {gates['pct_cap']}% of {gates['active_hosts']} active")
    if not gates["g3_ok"]:
        failed.append(f"G3: {gates['n_candidates']} > {gates['abs_cap']} absolute cap")
    run_id = run[0] if run else "—"
    csv_path = run[2] if run else "—"
    body = (
        f"# Run aborted — {inventory} — {today_iso()}\n\n"
        f"Run ID: {run_id}\n\n"
        f"## Failed gate(s)\n\n"
        + "\n".join(f"- {x}" for x in failed) + "\n\n"
        f"## Counts\n\n"
        f"Candidates this run: {gates['n_candidates']}\n"
        f"Active hosts: {gates['active_hosts']}\n"
        f"Threshold: {gates['pct_cap']}% / {gates['abs_cap']} abs\n\n"
        f"## Next step\n\n"
        f"Investigate the source CSV: {csv_path}\n"
        f"Re-run identify-hosts if the data is suspect.\n"
        f"Re-run evaluate after the next successful ingest.\n\n"
        f"No reports were written for this run.\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


# ---------------------------------------------------------------------------
# Cross-inventory rollup
# ---------------------------------------------------------------------------

def rollup_to_remove(per_inv_to_remove):
    """One row per unique fqdn flagged anywhere this run."""
    by_fqdn = {}
    for row in per_inv_to_remove:
        f = row["fqdn"]
        by_fqdn.setdefault(f, []).append(row)
    out = []
    for fqdn, rows in by_fqdn.items():
        invs = sorted({r["inventory"] for r in rows})
        also = sorted({inv for r in rows for inv in json.loads(r.get("also_active_in") or "[]")})
        removed_strs = [r.get("already_removed_from") or "" for r in rows]
        removed_strs = [s for s in removed_strs if s]
        merged_removed = ";".join(sorted(set(";".join(removed_strs).split(";")))) if removed_strs else ""
        merged_removed = ";".join(p for p in merged_removed.split(";") if p)  # remove empties
        oldest = min((r["last_seen_up"] for r in rows if r["last_seen_up"]), default="")
        max_days = max((r["days_silent"] for r in rows if isinstance(r["days_silent"], int)), default=0)
        out.append({
            "fqdn": fqdn,
            "inventories_to_remove": json.dumps(invs),
            "also_active_in": json.dumps(also),
            "already_removed_from": merged_removed,
            "oldest_last_seen_up": oldest,
            "max_days_silent": max_days,
        })
    out.sort(key=lambda x: x["fqdn"])
    return out


def rollup_returned(per_inv_returned):
    by_fqdn = {}
    for row in per_inv_returned:
        by_fqdn.setdefault(row["fqdn"], []).append(row)
    out = []
    for fqdn, rows in by_fqdn.items():
        invs = sorted({r["inventory"] for r in rows})
        also = sorted({inv for r in rows for inv in json.loads(r.get("also_active_in") or "[]")})
        out.append({
            "fqdn": fqdn,
            "inventories_returned": json.dumps(invs),
            "also_active_in": json.dumps(also),
            "max_days_absent": max((r["days_absent"] for r in rows
                                     if isinstance(r["days_absent"], int)), default=""),
        })
    out.sort(key=lambda x: x["fqdn"])
    return out


def rollup_intermittent(per_inv_intermittent):
    by_fqdn = {}
    for row in per_inv_intermittent:
        by_fqdn.setdefault(row["fqdn"], []).append(row)
    out = []
    for fqdn, rows in by_fqdn.items():
        out.append({
            "fqdn": fqdn,
            "inventories_intermittent": json.dumps(sorted({r["inventory"] for r in rows})),
            "max_flips": max(r["flips"] for r in rows),
        })
    out.sort(key=lambda x: x["fqdn"])
    return out


# ---------------------------------------------------------------------------
# Top-level evaluate
# ---------------------------------------------------------------------------

def evaluate_all(conn, report_dir, tunables, inventory_filter=None):
    inventories = _active_inventories(conn)
    if inventory_filter:
        inventories = [i for i in inventories if i == inventory_filter]
    active_acks = get_active_acks_set(conn)
    today = today_iso()

    all_to_remove, all_returned, all_intermittent = [], [], []

    for inv in inventories:
        inv_dir = os.path.join(report_dir, inv, today)
        os.makedirs(inv_dir, exist_ok=True)
        run = _latest_run(conn, inv)
        counts = _state_counts(conn, inv)
        to_remove = candidates_to_remove(conn, inv, tunables, active_acks)
        gates = gate_decisions(conn, inv, to_remove, tunables)
        inv_acks = [a for a in (
            conn.execute(
                """SELECT ack_id, inventory, fqdn, signal_kind, ack_until, reason, added_at, added_by
                     FROM acks
                    WHERE ack_until >= ?
                      AND (inventory IS NULL OR inventory = ?)""",
                (today, inv),
            ).fetchall()
        )]
        inv_acks_dicts = [
            {"ack_id": a[0], "inventory": a[1], "fqdn": a[2], "signal_kind": a[3],
             "ack_until": a[4], "reason": a[5], "added_at": a[6], "added_by": a[7]}
            for a in inv_acks
        ]
        write_summary(os.path.join(inv_dir, "summary.md"), inv, run, counts, gates, inv_acks_dicts)

        if gates["any_failed"]:
            write_aborted(os.path.join(inv_dir, "aborted.md"), inv, run, gates)
            print(f"{inv}: ABORTED (sanity gate failed); see {inv_dir}/aborted.md")
            continue

        returned = candidates_returned(conn, inv, tunables, active_acks)
        intermittent = candidates_intermittent(conn, inv, tunables, active_acks)

        _write_csv(os.path.join(inv_dir, "to_remove.csv"), TO_REMOVE_FIELDS, to_remove)
        _write_csv(os.path.join(inv_dir, "returned.csv"), RETURNED_FIELDS, returned)
        _write_csv(os.path.join(inv_dir, "intermittent.csv"), INTERMITTENT_FIELDS, intermittent)

        all_to_remove.extend(to_remove)
        all_returned.extend(returned)
        all_intermittent.extend(intermittent)
        print(f"{inv}: to_remove={len(to_remove)} returned={len(returned)} "
              f"intermittent={len(intermittent)}")

    # Cross-inventory rollup
    rollup_dir = os.path.join(report_dir, "_all", today)
    os.makedirs(rollup_dir, exist_ok=True)
    if all_to_remove or all_returned or all_intermittent:
        _write_csv(
            os.path.join(rollup_dir, "to_remove.csv"),
            ["fqdn", "inventories_to_remove", "also_active_in",
             "already_removed_from", "oldest_last_seen_up", "max_days_silent"],
            rollup_to_remove(all_to_remove),
        )
        _write_csv(
            os.path.join(rollup_dir, "returned.csv"),
            ["fqdn", "inventories_returned", "also_active_in", "max_days_absent"],
            rollup_returned(all_returned),
        )
        _write_csv(
            os.path.join(rollup_dir, "intermittent.csv"),
            ["fqdn", "inventories_intermittent", "max_flips"],
            rollup_intermittent(all_intermittent),
        )
        with open(os.path.join(rollup_dir, "summary.md"), "w", encoding="utf-8") as f:
            f.write(
                f"# Inventory lifecycle — cross-inventory rollup — {today}\n\n"
                f"Unique fqdns flagged for removal: {len({r['fqdn'] for r in all_to_remove})}\n"
                f"Unique fqdns returned:           {len({r['fqdn'] for r in all_returned})}\n"
                f"Unique fqdns intermittent:       {len({r['fqdn'] for r in all_intermittent})}\n"
            )
    else:
        with open(os.path.join(rollup_dir, "summary.md"), "w", encoding="utf-8") as f:
            f.write(f"# Cross-inventory rollup — {today}\n\nNothing flagged this run.\n")

    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--report-dir", required=True)
    p.add_argument("--inventory", help="filter to one inventory")
    for k, v in DEFAULT_TUNABLES.items():
        p.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    p.add_argument("--intermittent-window-days", type=int, default=30)
    p.add_argument("--intermittent-min-flips", type=int, default=4)
    p.add_argument("--max-remove-per-run-pct", type=int, default=5)
    p.add_argument("--max-remove-per-run-abs", type=int, default=50)
    args = p.parse_args(argv)

    tunables = {
        "silent_threshold_days": args.silent_threshold_days,
        "min_successful_runs_before_action": args.min_successful_runs_before_action,
        "min_run_success_pct": args.min_run_success_pct,
        "intermittent_window_days": args.intermittent_window_days,
        "intermittent_min_flips": args.intermittent_min_flips,
        "max_remove_per_run_pct": args.max_remove_per_run_pct,
        "max_remove_per_run_abs": args.max_remove_per_run_abs,
    }
    conn = connect(args.db)
    try:
        return evaluate_all(conn, args.report_dir, tunables, args.inventory)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
