# Fleet discovery toolkit (AAP-ready)

Walks the inventory and writes ONE consolidated CSV to
`<report_host>:/myshare/data/<inventory>_<date>_<jobid>_servers_info.csv`:
reachability, SSH, OS + version, physical/VM, plus supporting metadata — and,
for hosts that answered SSH but whose fact gather failed, a classified one-line
error. The filename is prefixed at write time with the AAP inventory name, the
UTC date, and the job id (e.g. `prod-linux_2026-06-22_10432_servers_info.csv`);
on a local CLI run with no AAP context it falls back to
`inventory_<date>_local_servers_info.csv`.

Self-contained: **no `ansible.cfg` and no static inventory file.** The host list
comes from the AAP inventory on the job template; the tunable knobs (`mng_host`,
scan ports, `nmap_opts`, paths, report destination) live once in
[`vars/main.yml`](vars/main.yml), and connection tuning is set as play vars.
Override any knob per run with `-e` (CLI) or an AAP extra var / survey field.

## Flow (nmap-gated)

1. **Phase 1 - one nmap sweep, run on the tools host `mng_host`** (not the EE).
   A pre-step writes the inventory (`groups['all']`) to a file on mng_host; nmap
   reads it with `-iL` (avoids a 5000-host command line / ARG_MAX) and does
   liveness + port-22 state + OS guess in a single pass. The XML is fetched back
   and parsed locally. Only up-and-22-open hosts advance to Phase 2. nmap runs
   once (it parallelises internally; it is not gated by Ansible forks).
2. **Phase 2 - facts, only for the SSH-reachable subset.** Facts are gathered
   with an **explicit `setup:` task** (not the implicit `gather_facts`) so the
   result can be registered: a per-host failure is caught (`ignore_errors` +
   `ignore_unreachable`) and **classified** into a short `error_class` plus a
   trimmed `error_msg`, instead of collapsing to a bare yes/no. Successful hosts
   yield authoritative OS, version, physical/VM, kernel, CPU, and memory.
3. **Phase 3 - merge (facts win) and write one CSV** to the report host. SSH
   facts are authoritative; nmap fills in everything that never answered.

### `error_class` values (Phase 2)

For an SSH-open host whose fact gather failed, the message is classified (in
priority order) as one of: `auth-failed`, `timeout`, `python-version`,
`python-deps`, `python-missing`, `locale`, `privilege-escalation`,
`network-unreachable`, `unreachable`, `module-failure`, or a generic `error`.
Hosts that never had port 22 open are left blank here (they are *expected* to
have no SSH facts — they are not "errored").

## Where the old ansible.cfg settings went

Everything except `forks` is now a play var or task arg in `discover.yml`:

| old cfg setting | now set as |
|---|---|
| `timeout` | var `ansible_timeout` (Phase 2) |
| `gather_timeout` | arg `gather_timeout:` on the explicit `setup:` task (Phase 2) |
| `gathering` | gathered explicitly as a registered `setup:` task (Phase 2) |
| `host_key_checking` | var `ansible_host_key_checking` + `ansible_ssh_common_args` |
| `interpreter_python` | var `ansible_python_interpreter` |
| `pipelining` | var `ansible_pipelining` |
| `ssh_args` (ConnectTimeout/keys) | var `ansible_ssh_common_args` |
| `retry_files`, `stdout_callback`, `display_skipped_hosts` | dropped (defaults / AAP owns output) |
| `inventory` | the AAP job template inventory |
| **`forks`** | **AAP job template "Forks" field** (no playbook equivalent) |

## Running in AAP

- **Inventory**: attach your inventory to the job template (read as `groups['all']`).
  Avoid a Limit unless you want a subset - `groups['all']` is the full inventory.
- **Forks**: set on the job template. Note this mainly affects Phase 2 (the small
  SSH subset); the 5000-host parallelism is nmap's job in Phase 1.
- **Timeout field** on the job template is the *job* timeout, NOT the SSH connect
  timeout (that's `ansible_timeout`, already set in the playbook). Leave it 0 or
  generous.
- **Where nmap runs**: on `mng_host`, not the EE. That host needs `nmap`
  installed and `become` rights (the `-sS`/`-O` flags need root/CAP_NET_RAW),
  and must have network reachability to the fleet. This deliberately keeps the
  privileged/raw-socket work off the ephemeral EE, which often lacks nmap, root,
  or line-of-sight. If mng_host can't run privileged, set `become: false` on the
  nmap task and `nmap_opts: "-sT -sV --version-light"` - you keep liveness,
  ports, and the SSH banner but lose the OS guess for no-SSH hosts (SSH-able
  hosts still get authoritative facts). The EE only needs `python3` (stdlib) to
  parse the fetched XML, plus SSH reachability for Phase 2 fact gathering.
- **mng_host**: set `mng_host` in [`vars/main.yml`](vars/main.yml) (default
  `mng01`), or override with `-e mng_host=...`; it runs nmap AND stores the
  CSV. Defined via `add_host` (with `ansible_connection: ssh`), so it needn't be
  in the inventory - pass `mng_host_address` only if its short name won't resolve from the EE (usually you can leave it empty) and
  `mng_host_user` if it needs a different SSH user than the fleet. The explicit
  `ansible_connection: ssh` matters: these plays run `connection: local`, so
  without it the delegated tasks (nmap, fetch, CSV write) would run on the EE
  instead of on mng_host. Needs SSH reachability from the EE.

## Scope and limitations

- **IPv4 only.** The parser reads nmap `address` entries of type `ipv4`; an
  IPv6-only host comes back with an empty `ipv4` and is reported from whatever
  liveness/port data nmap returns. There is no IPv6 column.
- **Windows (and other non-SSH) hosts get nmap data only.** Phase 2 gathers
  facts over SSH, so WinRM/RDP hosts never advance to fact gathering even though
  `scan_ports` probes 3389/5985/5986. They appear in the CSV with their nmap
  liveness, open ports, and OS *guess* (`detection_method: nmap-guess`/`none`),
  but never `ssh-facts`. Inventory data for Windows belongs to a separate WinRM
  flow, not this toolkit.

## Security note

This is a discovery tool pointed at hosts you may not yet trust, so it
deliberately **disables SSH host-key verification** for the Phase 2 fact gather:
`ansible_host_key_checking: false` plus `-o StrictHostKeyChecking=no
-o UserKnownHostsFile=/dev/null`. That keeps a first-contact sweep from stalling
on unknown-host prompts, but it removes MITM protection for those connections —
an attacker positioned on the path could impersonate a target during the scan.
Accept it for throwaway discovery runs; do **not** copy these connection
settings into playbooks that push configuration or secrets to those hosts.

## Running locally (no AAP)

```bash
ansible-playbook -i 'host1,host2,10.0.0.9,' playbooks/discover.yml \
    -f 100 -e ansible_user=svc_discovery --private-key ~/.ssh/id -K
```
`-f` is the local stand-in for the AAP Forks field; `-i` supplies the inventory.

## Files

| Path | Purpose |
|------|---------|
| `playbooks/discover.yml` | the 3-phase playbook |
| `vars/main.yml` | tunable knobs (mng_host, ports, nmap_opts, paths) — single source, `-e`-overridable |
| `files/parse_nmap.py` | nmap XML -> JSON, stdlib only (air-gap safe) |
| `templates/report.csv.j2` | merge + CSV rendering |

The playbook reaches its siblings via `{{ playbook_dir }}/../files/` and
`{{ playbook_dir }}/../templates/`, keeping `files/` and `templates/` at the
project root where Ansible conventionally expects them.

## CSV columns

`fqdn, ipv4, reachable, ssh_open, os, os_version, os_accuracy, detection_method,
machine_type, virt_type, arch, kernel, uptime_days, cpu_cores, memory_mb,
open_ports, mac, mac_vendor, ssh_banner, last_checked, error_class, error_msg`.

`detection_method` is one of:

- `ssh-facts` — authoritative; we logged in and asked.
- `nmap-guess` — no SSH facts, but nmap fingerprinted an OS (trust it as far as `os_accuracy`).
- `nmap-partial` — host is reachable (up) but nmap couldn't fingerprint the OS; you still get liveness, open ports, and any banner.
- `none` — not reachable (down, filtered, or absent from the scan entirely).

`error_class` / `error_msg` are populated only for hosts that were SSH-open but
whose fact gather failed (see the `error_class` list above).
