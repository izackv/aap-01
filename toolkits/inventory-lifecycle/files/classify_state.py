"""Classify an identify-hosts CSV row into one of the observation states.

Pure function. See SPEC §1 for the rules; the order below matters (first
match wins). No I/O, no DB, no logging — easy to unit-test.
"""

_SSH_BLOCKED_ERRORS = frozenset({
    "timeout",
    "network-unreachable",
    "unreachable",
    "module-failure",
    "python-version",
    "python-deps",
    "python-missing",
    "locale",
    "privilege-escalation",
    "error",
})

_TRUE_LITERALS = frozenset({"true", "1", "yes"})
_FALSE_LITERALS = frozenset({"false", "0", "no", ""})


class BoolParseError(ValueError):
    pass


def to_bool(value):
    """Parse identify-hosts' boolean columns. Raises BoolParseError on garbage."""
    if value is None:
        return False
    s = str(value).strip().lower()
    if s in _TRUE_LITERALS:
        return True
    if s in _FALSE_LITERALS:
        return False
    raise BoolParseError(f"unparseable boolean: {value!r}")


def safe_bool(value):
    """Like to_bool but returns False on garbage instead of raising."""
    try:
        return to_bool(value)
    except BoolParseError:
        return False


def classify(row, allowlist=None):
    """Classify one identify-hosts CSV row → observation state string.

    row: dict with the columns from identify-hosts (SPEC §2).
    allowlist: optional set of (inventory_or_None, fqdn) tuples. If provided
               and the row matches, returns 'excluded'.

    Returns one of: up | auth_failed | ssh_blocked | online_unmanaged | down |
                    excluded. The 'absent' state is determined outside this
                    function (a host is absent when no row exists at all).
    """
    reachable = to_bool(row.get("reachable"))
    ssh_open = to_bool(row.get("ssh_open"))
    error_class = (row.get("error_class") or "").strip()
    detection_method = (row.get("detection_method") or "").strip()

    if allowlist is not None:
        inv = (row.get("inventory") or "").strip() or None
        fqdn = (row.get("fqdn") or "").strip().lower()
        if (inv, fqdn) in allowlist or (None, fqdn) in allowlist:
            return "excluded"

    if reachable and ssh_open and not error_class and detection_method == "ssh-facts":
        return "up"
    if ssh_open and error_class == "auth-failed":
        return "auth_failed"
    if ssh_open and error_class in _SSH_BLOCKED_ERRORS:
        return "ssh_blocked"
    if reachable and not ssh_open:
        return "online_unmanaged"
    if not reachable:
        return "down"
    # Defensive default for malformed rows.
    return "down"
