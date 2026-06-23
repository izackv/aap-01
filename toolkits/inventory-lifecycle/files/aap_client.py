"""Thin stdlib-urllib wrapper around the AAP REST API.

Stdlib only — no `requests`, no `awx.awx` collection dependency. See
SPEC §6 for the worked examples this implements.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT = 30


class AAPError(Exception):
    def __init__(self, status, body, message=""):
        self.status = status
        self.body = body
        super().__init__(message or f"AAP {status}: {body[:200]}")


def _request(method, url, token, body=None, timeout=DEFAULT_TIMEOUT):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {"_status": r.status}
            try:
                doc = json.loads(raw)
                if isinstance(doc, dict):
                    doc["_status"] = r.status
                return doc
            except json.JSONDecodeError:
                return {"_status": r.status, "_raw": raw.decode("utf-8", "replace")[:500]}
    except HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        raise AAPError(e.code, body_text)
    except URLError as e:
        raise AAPError(0, str(e.reason))


def _get(url, token, params=None, timeout=DEFAULT_TIMEOUT):
    if params:
        url = f"{url}?{urlencode(params)}"
    return _request("GET", url, token, timeout=timeout)


def inventory_id_by_name(aap_url, token, name):
    data = _get(f"{aap_url}/api/v2/inventories/", token, {"name": name})
    count = data.get("count", 0)
    if count != 1:
        raise LookupError(f"inventory {name!r}: found {count}")
    return data["results"][0]["id"]


def host_lookup(aap_url, token, inventory_id, fqdn):
    """Return the host dict (with id, enabled, ...) or None if not present."""
    data = _get(
        f"{aap_url}/api/v2/hosts/",
        token,
        {"name": fqdn, "inventory": inventory_id},
    )
    count = data.get("count", 0)
    if count == 0:
        return None
    if count > 1:
        raise LookupError(f"host {fqdn!r} in inv {inventory_id}: found {count}")
    return data["results"][0]


def disable_host(aap_url, token, host_id):
    return _request(
        "PATCH", f"{aap_url}/api/v2/hosts/{host_id}/", token, {"enabled": False}
    )


def enable_host(aap_url, token, host_id):
    return _request(
        "PATCH", f"{aap_url}/api/v2/hosts/{host_id}/", token, {"enabled": True}
    )


def delete_host(aap_url, token, host_id):
    return _request("DELETE", f"{aap_url}/api/v2/hosts/{host_id}/", token)


def add_host(aap_url, token, inventory_id, fqdn, variables="---\n"):
    return _request(
        "POST",
        f"{aap_url}/api/v2/inventories/{inventory_id}/hosts/",
        token,
        {"name": fqdn, "variables": variables},
    )
