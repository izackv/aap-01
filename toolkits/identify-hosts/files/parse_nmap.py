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
    ports, ssh, banner = [], False, ""
    for p in h.findall("ports/port"):
        if p.find("state").get("state") != "open":
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
        "ssh_open": ssh, "ssh_banner": banner, "open_ports": ports,
        "mac": a["mac"].get("addr", "") if "mac" in a else "", "mac_vendor": vendor,
        "nmap_os": m.get("name", "") if m is not None else "",
        "nmap_os_accuracy": m.get("accuracy", "") if m is not None else "",
        "nmap_os_family": cls.get("osfamily", "") if cls is not None else "",
        "nmap_os_gen": cls.get("osgen", "") if cls is not None else "",
        "vm_hint": vm,
    }

print(json.dumps([host(h) for h in ET.parse(sys.argv[1]).getroot().findall("host")], indent=2))
