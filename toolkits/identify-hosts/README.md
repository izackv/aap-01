# Fleet discovery toolkit (AAP-ready)

Walks the inventory and writes ONE consolidated CSV to
`<report_host>:/myshare/data/servers_info.csv`: reachability, SSH, OS + version,
physical/VM, plus supporting metadata.

Self-contained: **no `ansible.cfg` and no static inventory file.** The host list
comes from the AAP inventory on the job template; all connection tuning lives in
the playbook as play vars.

## Flow (nmap-gated)

1. **Phase 1 - one nmap sweep, run on the tools host `mng_host`** (not the EE).
   A pre-step writes the inventory (`groups['all']`) to a file on mng_host; nmap
   reads it with `-iL` (avoids a 5000-host command line / ARG_MAX) and does
   liveness + port-22 state + OS guess in a single pass. The XML is fetched back
   and parsed locally. Only up-and-22-open hosts advance to Phase 2. nmap runs
   once (it parallelises internally; it is not gated by Ansible forks).
2. **Phase 2 - facts, only for the SSH-reachable subset.** Authoritative OS,
   version, physical/VM. Small set, finishes fast.
3. **Phase 3 - merge (facts win) and write one CSV** to the report host.

## Where the old ansible.cfg settings went

Everything except `forks` is now a play var or play keyword in `discover.yml`:

| old cfg setting | now set as |
|---|---|
| `timeout` | var `ansible_timeout` (Phase 2) |
| `gather_timeout` | play keyword `gather_timeout` (Phase 2) |
| `host_key_checking` | var `ansible_host_key_checking` + `ansible_ssh_common_args` |
| `interpreter_python` | var `ansible_python_interpreter` |
| `pipelining` | var `ansible_pipelining` |
| `ssh_args` (ConnectTimeout/keys) | var `ansible_ssh_common_args` |
| `gathering` | `gather_facts:` per play (implicit) |
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
- **mng_host**: set `mng_host` (default `mng01`); it runs nmap AND stores the
  CSV. Defined via `add_host` (with `ansible_connection: ssh`), so it needn't be
  in the inventory - pass `mng_host_address` only if its short name won't resolve from the EE (usually you can leave it empty) and
  `mng_host_user` if it needs a different SSH user than the fleet. The explicit
  `ansible_connection: ssh` matters: these plays run `connection: local`, so
  without it the delegated tasks (nmap, fetch, CSV write) would run on the EE
  instead of on mng_host. Needs SSH reachability from the EE.

## Running locally (no AAP)

```bash
ansible-playbook -i 'host1,host2,10.0.0.9,' playbooks/discover.yml \
    -f 100 -e ansible_user=svc_discovery --private-key ~/.ssh/id -K
```
`-f` is the local stand-in for the AAP Forks field; `-i` supplies the inventory.

## Files

| Path | Purpose |
|------|---------|
| `playbooks/discover.yml` | the 3-phase playbook (all tuning inlined) |
| `files/parse_nmap.py` | nmap XML -> JSON, stdlib only (air-gap safe) |
| `templates/report.csv.j2` | merge + CSV rendering |

The playbook reaches its siblings via `{{ playbook_dir }}/../files/` and
`{{ playbook_dir }}/../templates/`, keeping `files/` and `templates/` at the
project root where Ansible conventionally expects them.

## CSV columns

`fqdn, ipv4, reachable, ssh_open, os, os_version, os_accuracy, detection_method,
machine_type, virt_type, arch, kernel, uptime_days, cpu_cores, memory_mb,
open_ports, mac, mac_vendor, ssh_banner, last_checked`.

`detection_method` is `ssh-facts` (authoritative), `nmap-guess` (trust
`os_accuracy`), or `none` (unreachable).
