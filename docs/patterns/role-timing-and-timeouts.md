---
title: Ansible Role Timing with Global Timeout and CSV Reporting
tags: [ansible, timeout, async, csv, aap]
created: 2026-06-08
---

# Ansible Role Timing with Global Timeout + CSV Report

This setup runs roles `r1`–`r4`, applies a **global 15-minute per-task timeout**, records
per-role duration and success/failure, and writes a single CSV to a shared log folder on a
remote host. Roles do **not** need to be modified for this to work — the wrapper lives in the
playbook. A sample role is included at the end for the case where you *do* want to fine-tune
timeouts per task using `async`.

---

## 1. `ansible.cfg` (global 15-minute timeout)

Place this at the **root of the project repository**. Automation controller runs the playbook
with the project directory as the working directory, so a root-level `ansible.cfg` is picked up
automatically (it is second in Ansible's config resolution order, right after `ANSIBLE_CONFIG`).

`task_timeout` is **per task, applied globally** to every task in the run. 15 minutes = **900 seconds**.

```ini
# ansible.cfg
[defaults]
# Global per-task wall-clock limit. Any single task exceeding this fails with a timeout error.
# 15 minutes = 900 seconds.
task_timeout = 900
```

> [!NOTE]
> **Precedence**
> `ansible.cfg` is **below** environment variables. If `ANSIBLE_TASK_TIMEOUT` is set anywhere
> (shell, EE image, or — on AAP — a Job Template variable), it overrides this file.
> If `ANSIBLE_CONFIG` points at another file, this one is ignored entirely.
> Ansible prints the loaded config at the top of output: `Using /runner/project/ansible.cfg as config file`.

---

## 2. Playbook — runs `r1`–`r4` with timing

The `roles:` keyword gives no per-role hooks, so roles are driven from `tasks:` via a loop.
The roles themselves are untouched.

```yaml
# site.yml
---
- name: Run roles with per-role timing + CSV report
  hosts: all
  gather_facts: true            # only if your roles need facts
  vars:
    roles_to_run:
      - r1
      - r2
      - r3
      - r4
  tasks:
    - name: Execute each role with timing + failure capture
      ansible.builtin.include_tasks: _timed_role.yml
      loop: "{{ roles_to_run }}"
      loop_control:
        loop_var: role_name

    - name: Write timing report to shared log folder
      run_once: true
      delegate_to: myserver.com
      ansible.builtin.copy:
        dest: "//sharefolder/logs/role_timings.csv"
        content: |
          host,role,duration_ms,status
          {{ ansible_play_hosts_all
             | map('extract', hostvars, 'csv_lines')
             | map('default', [])
             | flatten
             | join('\n') }}
```

---

## 3. Included task — times and runs each role

`block` / `rescue` / `always` captures duration and status for **every** role, whether it
succeeds, fails, or is killed by `task_timeout`. A hung task does not "fail" on its own — the
global `task_timeout` is what converts a freeze into a failure that `rescue` can catch.

```yaml
# _timed_role.yml
---
- block:
    - name: "[{{ role_name }}] mark start"
      ansible.builtin.set_fact:
        role_start_ms: "{{ (now().timestamp() * 1000) | int }}"

    - name: "[{{ role_name }}] run role"
      ansible.builtin.include_role:
        name: "{{ role_name }}"

    - name: "[{{ role_name }}] mark success"
      ansible.builtin.set_fact:
        role_status: success

  rescue:
    - name: "[{{ role_name }}] mark failed"
      ansible.builtin.set_fact:
        role_status: failed

  always:
    - name: "[{{ role_name }}] record timing"
      ansible.builtin.set_fact:
        csv_lines: >-
          {{ csv_lines | default([]) +
             [ inventory_hostname ~ ',' ~ role_name ~ ',' ~
               (((now().timestamp() * 1000) | int) - (role_start_ms | int)) ~ ',' ~
               role_status ] }}
```

Behaviour notes:

- **Continues after failure.** `rescue` swallows the error, so `r2` still runs after `r1` fails,
  and every role lands in the CSV. To abort the host on first failure instead, add a
  `ansible.builtin.fail:` at the end of `rescue`.
- **Per-host timing is correct.** With the linear strategy each host records its own
  start/end around the role span; durations are real per-host wall-clock milliseconds.
- **"Abort" duration** ≈ the `task_timeout` value (900000 ms here), since that is when a hung
  task is killed.

---

## 4. The CSV step (delegated to `myserver.com`)

This is the second task in the playbook above. It is shown separately here because the delegation
target and path are the parts most likely to change:

```yaml
- name: Write timing report to shared log folder
  run_once: true                       # write the combined file once, not per host
  delegate_to: myserver.com            # the file is written on myserver.com, not the controller
  ansible.builtin.copy:
    dest: "//sharefolder/logs/role_timings.csv"
    content: |
      host,role,duration_ms,status
      {{ ansible_play_hosts_all
         | map('extract', hostvars, 'csv_lines')
         | map('default', [])
         | flatten
         | join('\n') }}
```

- `run_once: true` + aggregating from `hostvars` produces **one** clean file containing every
  host's rows, instead of concurrent per-host appends racing on the same file.
- `delegate_to: myserver.com` means the write happens on that host. Ensure `//sharefolder/logs`
  is mounted/reachable there and the connection user can write to it.
- On **AAP**, delegating to a real persisted host (as here) is the correct pattern — writing to
  `localhost` would land inside the ephemeral execution-environment container and be lost on
  teardown.

Example output:

```csv
host,role,duration_ms,status
node01,r1,4821,success
node01,r2,60003,failed
node01,r3,1187,success
node01,r4,902,success
node02,r1,4790,success
node02,r2,60001,failed
node02,r3,1203,success
node02,r4,915,success
```

---

## 5. Sample role with per-task `async` timeouts (`t1`–`t4`)

Use this only for roles where you want **finer-grained** control than the global 15-minute
backstop. Each task gets its own bounded runtime via `async` (max runtime) + `poll` (check
interval). With `poll > 0`, Ansible enforces the limit and kills the task on overrun — this is
required for the timeout to actually bite (`poll: 0` would disable enforcement).

```yaml
# roles/r1/tasks/main.yml
---
- name: t1 - quick check (30s budget)
  ansible.builtin.command: /usr/local/bin/t1-check
  async: 30
  poll: 5

- name: t2 - medium task (2m budget)
  ansible.builtin.command: /usr/local/bin/t2-run
  async: 120
  poll: 10

- name: t3 - bounded time sync (90s budget)
  ansible.builtin.command: chronyc waitsync 10
  async: 90
  poll: 5

- name: t4 - long task (5m budget)
  ansible.builtin.command: /usr/local/bin/t4-process
  async: 300
  poll: 15
```

> [!TIP]
> **How the two timeouts interact**
> The global `task_timeout = 900` is the **outer backstop** applied to every task. A task that
> also sets `async` is bounded by **whichever limit is shorter** — so `t1`'s 30-second `async`
> fires long before the 900-second global limit. This lets you keep the global safety net while
> tightening specific tasks in specific roles. `async` requires `command`/`shell`-style modules
> (not every module supports it).

---

## 6. Limiting a shell command at the command level (no task parameters)

If you want the limit baked into the **command itself** — independent of `async`, `task_timeout`,
or any task keyword — wrap it with the GNU coreutils `timeout` command. The bound travels with the
command string, so it works the same whether the task runs via Ansible, cron, or a shell, and needs
no specific ansible-core version.

```yaml
- name: t-x - bounded entirely by the shell timeout command
  ansible.builtin.shell: timeout 30 /usr/local/bin/long-thing
```

`timeout DURATION COMMAND` runs `COMMAND` and terminates it if it is still running after `DURATION`.
Duration accepts suffixes: `s` seconds (default), `m` minutes, `h` hours, `d` days — e.g. `timeout 5m ...`.

**Exit-code behaviour (matters for `rescue`).** On timeout, `timeout` exits **124**, which makes the
Ansible task fail naturally — so it is caught by the `rescue` block and recorded as `failed` in the
CSV, exactly like a `task_timeout` kill. A clean run passes the underlying command's own exit code
through.

**Force-kill a process that ignores SIGTERM.** By default `timeout` sends `SIGTERM`. Use
`-k`/`--kill-after` to follow up with `SIGKILL` if the process refuses to die, and `-s`/`--signal`
to choose the initial signal:

```yaml
- name: t-y - SIGTERM at 60s, hard SIGKILL 10s later if still alive
  ansible.builtin.shell: timeout -k 10 60 /usr/local/bin/stubborn-thing

- name: t-z - send a specific signal on timeout
  ansible.builtin.shell: timeout -s SIGINT 45 /usr/local/bin/needs-sigint
```

Bounded `chronyc` example for the original NTP-freeze case, with no task-level parameters at all:

```yaml
- name: Force time sync, hard-bounded by the shell
  ansible.builtin.shell: timeout 90 chronyc waitsync 10
```

> [!NOTE]
> **Caveats**
> - `timeout` is **GNU coreutils**, present on RHEL/Fedora and most Linux. It is not on stock
>   macOS (there it is `gtimeout` via Homebrew coreutils) or minimal BusyBox images.
> - This bounds the **command**, not the Ansible task overhead. The global `task_timeout` still
>   applies on top as the outer backstop — whichever fires first wins.
> - Use full paths or be mindful of `PATH` when running under `shell`.

---

## 7. Using the environment variable instead (plain Ansible, not AAP)

If you would rather not rely on the project `ansible.cfg` — for example to vary the timeout per
invocation without editing the repo — set `ANSIBLE_TASK_TIMEOUT` at run time. It maps to the same
`task_timeout` setting and **overrides** any value in `ansible.cfg`, because environment variables
sit above config files in Ansible's resolution order:

```bash
# 15 minutes = 900 seconds, just for this run
ANSIBLE_TASK_TIMEOUT=900 ansible-playbook -i inventory site.yml
```

Or export it for the whole shell session:

```bash
export ANSIBLE_TASK_TIMEOUT=900
ansible-playbook -i inventory site.yml
```

This is handy for one-off tightening/loosening (e.g. a quick smoke run with
`ANSIBLE_TASK_TIMEOUT=120`) without touching `ansible.cfg`. Just remember the override direction:
**env var > project `ansible.cfg` > defaults**, and `ANSIBLE_CONFIG` (if set) decides *which*
config file is read in the first place.
