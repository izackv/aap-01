#!/usr/bin/env python3
"""nmap -oX -> JSON list on stdout. Stdlib only.  Usage: parse_nmap.py scan.xml"""
import sys, json, xml.etree.ElementTree as ET

def host(h):
    a = {e.get("addrtype"): e for e in h.findall("address")}
    ip = a["ipv4"].get("addr", "") if "ipv4" in a else ""
    vendor = a["mac"].get("vendor", "") if "mac" in a else ""
    # Prefer the name WE asked nmap to scan (type="user") so it matches the
    # inventory entry exactly; fall back to reverse-DNS, then the IP.
    hns = h.findall("hostnames/hostname")
    user = next((x.get("name") for x in hns if x.get("type") == "user"), "")
    ptr = next((x.get("name") for x in hns if x.get("type") == "PTR"), "")
    st = h.find("status")
    # nmap --reason puts WHY the host got its state here: echo-reply / syn-ack
    # on up hosts, no-response / host-timeout / admin-prohibited on down ones.
    # Empty when the scan was run without --reason.
    status_reason = st.get("reason", "") if st is not None else ""
    ports, ssh, banner = [], False, ""
    for p in h.findall("ports/port"):
        state = p.find("state")
        if state is None or state.get("state") != "open":
            continue
        ports.append(p.get("portid"))
        if p.get("portid") == "22":
            ssh = True
            s = p.find("service")
            if s is not None:
                banner = (s.get("product", "") + " " + s.get("version", "")).strip()
    m = h.find("os/osmatch")
    cls = m.find("osclass") if m is not None else None
    vl = vendor.lower()
    vm = next((v for k, v in (("vmware", "VMware"), ("qemu", "KVM/QEMU"),
               ("virtualbox", "VirtualBox"), ("xen", "Xen")) if k in vl), "")
    return {
        "ipv4": ip,
        "fqdn": user or ptr or ip,
        "reachable": st is not None and st.get("state") == "up",
        "status_reason": status_reason,
        "ssh_open": ssh, "ssh_banner": banner, "open_ports": ports,
        "mac": a["mac"].get("addr", "") if "mac" in a else "", "mac_vendor": vendor,
        "nmap_os": m.get("name", "") if m is not None else "",
        "nmap_os_accuracy": m.get("accuracy", "") if m is not None else "",
        "nmap_os_family": cls.get("osfamily", "") if cls is not None else "",
        "nmap_os_gen": cls.get("osgen", "") if cls is not None else "",
        "vm_hint": vm,
    }

def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: parse_nmap.py <nmap-xml-file>\n")
        return 2
    try:
        root = ET.parse(argv[1]).getroot()
    except (ET.ParseError, OSError) as e:
        # Unreadable or malformed XML (e.g. nmap produced nothing): emit an empty
        # record set so the playbook degrades to "no hosts" instead of crashing on
        # from_json. The nmap return-code check in the playbook surfaces the real cause.
        sys.stderr.write("parse_nmap: could not parse %r: %s\n" % (argv[1], e))
        print("[]")
        return 0
    print(json.dumps([host(h) for h in root.findall("host")], indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
