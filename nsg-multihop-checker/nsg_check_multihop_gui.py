#!/usr/bin/env python3
"""
nsg_check_multihop_gui.py -- desktop window version of nsg_check_multihop_universal.py.
Run locally (Windows, macOS, or Linux). Requires: az login, python3 with
tkinter (bundled with Python on Windows and macOS). No pip installs needed,
standard library only.

Traces the FULL path from a source IP to a destination IP, following route
tables and VNet peering hop by hop, and shows every NSG on the way as a card
marked ALLOW or DENY, so the blocked hop is obvious at a glance.

Ports can be a single port (443), a range (8000-8100), a list (80,443,8000-8100)
or * for any. When the answer differs inside a range, the result is split, for
example "8000-8049 ALLOW, 8050-8100 DENY".

Same decision logic as the command-line script (route tables, peering, ASGs,
Load Balancer Floating IP / backend pools); only the presentation differs.

LIMITS, same as always:
- Cannot see inside a firewall's own policy (e.g. Panorama). Only checks the
  NSG at each Azure-visible hop.
- A hop only appears if an actual Azure route table points to it.
- Read-only. Nothing is created, changed, or deleted.

USAGE
    python3 nsg_check_multihop_gui.py
"""

import csv
import ipaddress
import json
import platform
import queue
import re
import subprocess
import threading
import traceback

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont
except ImportError:
    raise SystemExit(
        "tkinter is not available in this Python install.\n"
        "Windows/macOS: reinstall Python from python.org (tkinter is included).\n"
        "Debian/Ubuntu: sudo apt install python3-tk"
    )

# On Windows, `az` resolves to az.cmd, which subprocess can only find via the
# shell. shell=True with an argument list works on Windows but breaks argument
# handling on macOS/Linux, so this must be conditional.
IS_WINDOWS = platform.system() == "Windows"

QUERY = (
    'Resources '
    '| where type =~ "microsoft.network/virtualnetworks" '
    '| mvexpand subnet = properties.subnets '
    '| extend nsgId = tostring(subnet.properties.networkSecurityGroup.id) '
    "| extend nsgName = split(nsgId, '/')[-1] "
    '| project VNetName = name, ResourceGroup = resourceGroup, '
    'SubnetName = tostring(subnet.name), '
    'AddressPrefix = tostring(subnet.properties.addressPrefix), '
    'NSG_Name = iff(isnotempty(nsgName), tostring(nsgName), "NO NSG ATTACHED"), '
    'NSG_Id = nsgId, '
    'RouteTableId = tostring(subnet.properties.routeTable.id)'
)

LB_QUERY = (
    'Resources '
    '| where type =~ "microsoft.network/loadbalancers" '
    '| mvexpand fic = properties.frontendIPConfigurations '
    '| project LBName = name, ResourceGroup = resourceGroup, LBId = id, '
    'PrivateIP = tostring(fic.properties.privateIPAddress)'
)

ASG_QUERY = (
    'Resources '
    '| where type =~ "microsoft.network/networkinterfaces" '
    '| mvexpand ipconfig = properties.ipConfigurations '
    '| mvexpand asg = ipconfig.properties.applicationSecurityGroups '
    '| project PrivateIP = tostring(ipconfig.properties.privateIPAddress), '
    'ASGId = tostring(asg.id)'
)


# --------------------------------------------------------------------------
# Azure access (no GUI code in here)
# --------------------------------------------------------------------------

def run_az(args):
    """Runs an az command. Returns (parsed_json_or_None, error_text_or_None)."""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW, no console flash
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                shell=IS_WINDOWS, **kwargs)
    except FileNotFoundError:
        return None, "Azure CLI ('az') was not found. Install it and run 'az login'."
    if result.returncode != 0:
        return None, (result.stderr or "az command failed").strip()
    if not result.stdout.strip():
        return None, None
    return json.loads(result.stdout), None


_cache = {}     # az call results, cleared at the start of every trace
_notes = []     # warnings collected while tracing one check


def note(msg):
    if msg not in _notes:
        _notes.append(msg)


def az(args, quiet=False):
    """Cached az call. Returns parsed JSON, or None on failure (noted)."""
    key = tuple(args)
    if key in _cache:
        return _cache[key]
    data, err = run_az(args)
    if err:
        if not quiet:
            note("Azure call failed: " + " ".join(args[:6]) + " ... " + err[:200])
        return None
    _cache[key] = data
    return data


def clear_cache():
    _cache.clear()


def graph_all_pages(query, max_pages=25):
    """Returns (rows, error_text). Pages through Resource Graph results."""
    all_rows, skip_token = [], None
    for _ in range(max_pages):
        cmd = ["az", "graph", "query", "-q", query, "-o", "json"]
        if skip_token:
            cmd += ["--skip-token", skip_token]
        result, err = run_az(cmd)
        if err:
            return all_rows, err
        if result is None:
            break
        all_rows.extend(result.get("data", []))
        skip_token = result.get("skip_token") or result.get("skipToken") or result.get("$skipToken")
        if not skip_token:
            break
    return all_rows, None


def load_data():
    """Returns (data_or_None, error_text, warnings)."""
    run_az(["az", "extension", "add", "--name", "resource-graph", "-y"])
    rows, err = graph_all_pages(QUERY)
    if err:
        return None, err, []
    warnings = []
    lb_rows, err = graph_all_pages(LB_QUERY)
    if err:
        warnings.append("Load Balancer data unavailable")
        lb_rows = []
    asg_rows, err = graph_all_pages(ASG_QUERY)
    if err:
        warnings.append("ASG data unavailable")
        asg_rows = []
    return {"rows": rows, "lb_rows": lb_rows,
            "asg": build_asg_membership(asg_rows)}, None, warnings


# --------------------------------------------------------------------------
# Ports: single, range, list, any
# --------------------------------------------------------------------------

def parse_ports(text):
    """'443', '8000-8100', '80,443,8000-8100', '*' -> sorted merged [(lo, hi)]."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("Enter a port, a range like 8000-8100, a list like 80,443, or *")
    if t in ("*", "any"):
        return [(0, 65535)]
    t = re.sub(r"\s*-\s*", "-", t)
    out = []
    for part in re.split(r"[,\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"'{part}' is not a valid port or range")
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if lo > hi:
            raise ValueError(f"Range {part} starts higher than it ends")
        if hi > 65535:
            raise ValueError(f"Port {hi} is above 65535")
        out.append((lo, hi))
    if not out:
        raise ValueError("Enter at least one port")
    return ports_union(out)


def ports_union(ivs):
    out = []
    for lo, hi in sorted(ivs):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def ports_subtract(a, b):
    """Parts of intervals a that are not covered by intervals b."""
    result = []
    b = ports_union(b)
    for lo, hi in ports_union(a):
        cur = lo
        for blo, bhi in b:
            if bhi < cur or blo > hi:
                continue
            if blo > cur:
                result.append((cur, blo - 1))
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            result.append((cur, hi))
    return result


def fmt_ports(ivs):
    if ivs == [(0, 65535)] or ivs == [(1, 65535)]:
        return "any port"
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ivs)


def split_intervals(ivs, cuts):
    """Splits intervals at every cut point c (a new piece starts at c)."""
    cuts = sorted(set(cuts))
    out = []
    for lo, hi in ivs:
        start = lo
        for c in cuts:
            if start < c <= hi:
                out.append((start, c - 1))
                start = c
        out.append((start, hi))
    return out


def group_by_key(keyed):
    """[(lo, hi, key)] -> [(key, merged_intervals)] in first-seen order."""
    groups = {}
    for lo, hi, key in keyed:
        groups.setdefault(key, []).append((lo, hi))
    return [(k, ports_union(v)) for k, v in groups.items()]


def rule_cuts(rule):
    """Port boundaries a rule introduces (so a range can be split where rules change)."""
    ranges = []
    if rule.get("destinationPortRange"):
        ranges.append(rule["destinationPortRange"])
    ranges += (rule.get("destinationPortRanges") or [])
    cuts = []
    for rg in ranges:
        try:
            if rg == "*":
                continue
            if "-" in rg:
                lo, hi = rg.split("-")
                cuts += [int(lo), int(hi) + 1]
            elif rg.isdigit():
                cuts += [int(rg), int(rg) + 1]
        except ValueError:
            continue
    return cuts


def port_matches(rule, port):
    ranges = []
    if rule.get("destinationPortRange"):
        ranges.append(rule["destinationPortRange"])
    ranges += (rule.get("destinationPortRanges") or [])
    for rg in ranges:
        if rg == "*":
            return True
        if "-" in rg:
            lo, hi = rg.split("-")
            if int(lo) <= int(port) <= int(hi):
                return True
        elif rg.isdigit() and int(rg) == int(port):
            return True
    return False


# --------------------------------------------------------------------------
# Network logic (same decisions as nsg_check_multihop_universal.py)
# --------------------------------------------------------------------------

def find(ip, rows):
    target = ipaddress.ip_address(ip)
    for r in rows:
        pfx = r.get("AddressPrefix")
        if not pfx:
            continue
        try:
            if target in ipaddress.ip_network(pfx, strict=False):
                return r
        except ValueError:
            continue
    return None


def build_asg_membership(asg_rows):
    membership = {}
    for row in asg_rows:
        ip, asg_id = row.get("PrivateIP"), row.get("ASGId")
        if not ip or not asg_id:
            continue
        membership.setdefault(ip, set()).add(asg_id.lower())
    return membership


def asg_matches(rule, asg_field, ip, asg_membership):
    asg_refs = rule.get(asg_field) or []
    ip_asgs = asg_membership.get(ip, set())
    if not asg_refs or not ip_asgs:
        return False
    return any(ref.get("id", "").lower() in ip_asgs for ref in asg_refs)


def get_route_table(route_table_id):
    if not route_table_id:
        return None
    return az(["az", "network", "route-table", "show", "--ids", route_table_id, "-o", "json"])


def find_matching_route(route_table, dst_ip):
    if not route_table:
        return None
    target = ipaddress.ip_address(dst_ip)
    best, best_len = None, -1
    for route in (route_table.get("routes") or []):
        prefix = route.get("addressPrefix")
        if not prefix:
            continue
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            continue
        if target in net and net.prefixlen > best_len:
            best, best_len = route, net.prefixlen
    return best


def get_peered_vnet_names(resource_group, vnet_name):
    data = az(["az", "network", "vnet", "peering", "list",
               "--resource-group", resource_group, "--vnet-name", vnet_name, "-o", "json"],
              quiet=True)
    if data is None:
        note(f"Could not confirm peering for VNet '{vnet_name}' (resource group '{resource_group}'). "
             "This may be Resource Graph indexing lag. Only same-VNet and explicit CIDR rules "
             "were checked for that hop.")
        data = []
    peered = set()
    for p in data:
        if p.get("peeringState") == "Connected":
            peered.add(p.get("remoteVirtualNetwork", {}).get("id", "").split("/")[-1])
    return peered


def ip_matches(rule, ip, prefix_field, prefixes_field, ip_owner_row, self_vnet_name, get_peered_names):
    values = []
    if rule.get(prefix_field):
        values.append(rule[prefix_field])
    values += (rule.get(prefixes_field) or [])
    target = ipaddress.ip_address(ip)
    is_private = target.is_private
    for v in values:
        if v == "*":
            return True
        if v in ("Internet", "AzureCloud"):
            if ip_owner_row is None and not is_private:
                return True
            continue
        if v == "VirtualNetwork":
            if ip_owner_row is None:
                continue
            owner_vnet = ip_owner_row["VNetName"]
            if owner_vnet == self_vnet_name or owner_vnet in get_peered_names():
                return True
            continue
        try:
            if target in ipaddress.ip_network(v, strict=False):
                return True
        except ValueError:
            continue
    return False


def evaluate_rules(rules, protocol, port, src_ip, src_row, dst_ip, dst_owner_row,
                   self_vnet, get_peered_names, asg):
    """First matching rule for one specific port."""
    for r in rules:
        if r["protocol"].lower() not in ("*", protocol.lower()):
            continue
        if not port_matches(r, port):
            continue
        src_addr = ip_matches(r, src_ip, "sourceAddressPrefix", "sourceAddressPrefixes",
                              src_row, self_vnet, get_peered_names)
        src_asg = asg_matches(r, "sourceApplicationSecurityGroups", src_ip, asg)
        dst_addr = ip_matches(r, dst_ip, "destinationAddressPrefix", "destinationAddressPrefixes",
                              dst_owner_row, self_vnet, get_peered_names)
        dst_asg = asg_matches(r, "destinationApplicationSecurityGroups", dst_ip, asg)
        if not (src_addr or src_asg) or not (dst_addr or dst_asg):
            continue
        via = []
        if src_asg and not src_addr:
            via.append("source ASG")
        if dst_asg and not dst_addr:
            via.append("destination ASG")
        return {"access": r["access"].upper(), "rule": r["name"],
                "priority": r["priority"], "via": via}
    return {"access": "DENY", "rule": None, "priority": None, "via": []}


def seg_text(access, rule, priority, via):
    if rule is None:
        return "no matching rule (default deny)"
    s = f'rule "{rule}" (priority {priority})'
    if via:
        s += f", matched via {', '.join(via)}"
    return s


def check_nsg_ports(nsg_row, direction, src_ip, src_row, dst_ip, dst_row, dst_is_external,
                    ports, protocol, asg):
    """
    Checks one NSG for a set of ports. Returns a list of segments, one per
    distinct outcome, e.g. [ALLOW for 8000-8049, DENY for 8050-8100].
    """
    if nsg_row["NSG_Name"] == "NO NSG ATTACHED":
        return [{"ports": ports, "access": "NONSG", "rule": None, "priority": None,
                 "via": [], "text": "No NSG attached, not restricted here"}]
    nsg = az(["az", "network", "nsg", "show", "--ids", nsg_row["NSG_Id"], "-o", "json"])
    if nsg is None:
        return [{"ports": ports, "access": "ERROR", "rule": None, "priority": None,
                 "via": [], "text": "Could not read this NSG's rules"}]
    self_vnet = nsg_row["VNetName"]
    peered = {}

    def get_peered_names():
        if "v" not in peered:
            peered["v"] = get_peered_vnet_names(nsg_row["ResourceGroup"], self_vnet)
        return peered["v"]

    rules = (nsg.get("securityRules") or []) + (nsg.get("defaultSecurityRules") or [])
    rules = [r for r in rules if r["direction"].lower() == direction]
    rules.sort(key=lambda r: r["priority"])
    cuts = []
    for r in rules:
        cuts += rule_cuts(r)
    owner = None if dst_is_external else dst_row
    keyed = []
    for lo, hi in split_intervals(ports, cuts):
        res = evaluate_rules(rules, protocol, lo, src_ip, src_row, dst_ip, owner,
                             self_vnet, get_peered_names, asg)
        keyed.append((lo, hi, (res["access"], res["rule"], res["priority"], tuple(res["via"]))))
    segs = []
    for key, ivs in group_by_key(keyed):
        access, rule, prio, via = key
        segs.append({"ports": ivs, "access": access, "rule": rule, "priority": prio,
                     "via": list(via), "text": seg_text(access, rule, prio, via)})
    return segs


def can_reach_directly(current_row, dst_row):
    if current_row["VNetName"] == dst_row["VNetName"]:
        return True
    return dst_row["VNetName"] in get_peered_vnet_names(current_row["ResourceGroup"],
                                                       current_row["VNetName"])


def lb_partitions(lb, ports, protocol):
    """
    Splits the ports by which Load Balancing Rule covers them and reports, for
    each group: ("floating_on" | "floating_off" | "unknown", backend IPs).
    Returns [{"ports": [...], "status": ..., "backend_ips": [...] or None}]
    """
    lb_full = az(["az", "resource", "show", "--ids", lb["LBId"], "-o", "json"])
    if not lb_full:
        return [{"ports": ports, "status": "unknown", "backend_ips": None}]
    usable = []
    for r in (lb_full.get("properties", {}).get("loadBalancingRules", []) or []):
        props = r.get("properties", r)
        proto = str(props.get("protocol", "")).lower()
        if protocol != "*" and proto not in (protocol.lower(), "all", "*"):
            continue
        usable.append(props)
    cuts = []
    for props in usable:
        try:
            n = int(props.get("frontendPort"))
        except (TypeError, ValueError):
            continue
        if n > 0:
            cuts += [n, n + 1]
    keyed = []
    for lo, hi in split_intervals(ports, cuts):
        match = None
        for i, props in enumerate(usable):
            fp = props.get("frontendPort")
            # HA Ports rules cover every port at once, represented as frontend port 0.
            if fp in (0, "0", None) or str(fp) == str(lo):
                match = i
                break
        keyed.append((lo, hi, match))
    out = []
    for key, ivs in group_by_key(keyed):
        if key is None:
            out.append({"ports": ivs, "status": "unknown", "backend_ips": None})
            continue
        props = usable[key]
        if props.get("enableFloatingIP"):
            out.append({"ports": ivs, "status": "floating_on", "backend_ips": None})
            continue
        ips = backend_ips_for_rule(props)
        if ips:
            out.append({"ports": ivs, "status": "floating_off", "backend_ips": ips})
        else:
            out.append({"ports": ivs, "status": "unknown", "backend_ips": None})
    return out


def backend_ips_for_rule(props):
    pool_id = (props.get("backendAddressPool") or {}).get("id")
    if not pool_id:
        return []
    pool = az(["az", "resource", "show", "--ids", pool_id, "-o", "json"])
    if not pool:
        return []
    pp = pool.get("properties", {}) or {}
    ips = []
    # NIC-based backend pool (VMs/VMSS in the same VNet as the LB).
    for cfg in (pp.get("backendIPConfigurations", []) or []):
        res = az(["az", "resource", "show", "--ids", cfg["id"], "-o", "json"])
        ip = (res or {}).get("properties", {}).get("privateIPAddress")
        if ip:
            ips.append(ip)
    # IP-based backend pool (plain addresses, no NIC resource in this subscription).
    for addr in (pp.get("loadBalancerBackendAddresses", []) or []):
        ip = (addr.get("properties") or {}).get("ipAddress")
        if ip:
            ips.append(ip)
    return ips


class Tracer:
    """Walks the path and collects it as plain data the GUI can draw."""

    def __init__(self, data, src_ip, dst_ip, protocol, max_hops):
        self.rows = data["rows"]
        self.lb_rows = data["lb_rows"]
        self.asg = data["asg"]
        self.src_ip, self.dst_ip = src_ip, dst_ip
        self.protocol, self.max_hops = protocol, max_hops
        self.src_row = find(src_ip, self.rows)
        self.dst_row = find(dst_ip, self.rows)
        self.dst_ext = self.dst_row is None
        self.paths = []

    # ---- helpers ---------------------------------------------------------

    def _where(self, row):
        return f"{row['VNetName']} / {row['SubnetName']}"

    def _finish(self, hops, tags, ports, reached=True, incomplete=False):
        if self.dst_ext:
            sub = "External (not in any known Azure subnet)"
        else:
            sub = self._where(self.dst_row)
        end = {"kind": "end", "role": "destination", "title": f"Destination {self.dst_ip}",
               "sub": sub, "reached": reached}
        self.paths.append({"tags": list(tags), "hops": self._restrict(hops, ports) + [end],
                           "incomplete": incomplete, "reached": reached})

    @staticmethod
    def _restrict(hops, ports):
        """Keeps only the part of each NSG result that applies to this path's ports."""
        out = []
        for c in hops:
            if c["kind"] != "hop":
                out.append(c)
                continue
            segs = []
            for s in c["segments"]:
                inter = ports_subtract(s["ports"], ports_subtract(s["ports"], ports))
                if inter:
                    segs.append(dict(s, ports=inter))
            out.append(dict(c, segments=segs or c["segments"]))
        return out

    def _msg(self, level, text):
        return {"kind": "msg", "level": level, "text": text}

    def _hop_card(self, num, row, direction, segs, role="hop", footnote=None):
        return {"kind": "hop", "num": num, "nsg": row["NSG_Name"], "sub": self._where(row),
                "direction": direction, "segments": segs, "role": role, "footnote": footnote}

    def _check(self, row, direction, dst_ip, dst_row, dst_ext, ports):
        return check_nsg_ports(row, direction, self.src_ip, self.src_row, dst_ip, dst_row,
                               dst_ext, ports, self.protocol, self.asg)

    def _group(self, ips):
        groups, unresolved = {}, []
        for ip in ips:
            row = find(ip, self.rows)
            if not row:
                unresolved.append(ip)
                continue
            key = (row["VNetName"], row["SubnetName"])
            groups.setdefault(key, {"row": row, "ips": []})["ips"].append(ip)
        return list(groups.values()), unresolved

    def _resolve(self, ip, ports):
        """If ip is a Load Balancer frontend, split ports by Floating IP setting."""
        lbs = [r for r in self.lb_rows if r.get("PrivateIP") == ip]
        if not lbs:
            return [{"ports": ports, "ips": [ip], "substituted": False,
                     "note": None, "level": "info", "tag": None}]
        lb = lbs[0]
        parts = lb_partitions(lb, ports, self.protocol)
        many = len(parts) > 1
        out = []
        for p in parts:
            label = fmt_ports(p["ports"])
            pre = f"{ip} is the frontend IP of Load Balancer '{lb['LBName']}'"
            pre += f" (ports {label}). " if many else ". "
            tag = f"Ports {label}" if many else None
            if p["status"] == "floating_on":
                out.append({"ports": p["ports"], "ips": [ip], "substituted": False, "level": "info",
                            "tag": tag, "note": pre + "Floating IP is ON for the matching rule, so "
                            "the NSG sees this frontend IP. Checked as given."})
            elif p["status"] == "floating_off":
                out.append({"ports": p["ports"], "ips": p["backend_ips"], "substituted": True,
                            "level": "info", "tag": tag,
                            "note": pre + "Floating IP is OFF for the matching rule, so the NSG sees "
                            f"the backend IP(s) {', '.join(p['backend_ips'])}. Checked those instead."})
            else:
                out.append({"ports": p["ports"], "ips": [ip], "substituted": False, "level": "warn",
                            "tag": tag, "note": pre + "Could not determine the Floating IP setting "
                            "(no matching load balancing rule, possibly an Inbound NAT rule, which "
                            "is not checked). Verify manually."})
        return out

    # ---- trace -----------------------------------------------------------

    def run(self, ports):
        src = {"kind": "end", "role": "source", "title": f"Source {self.src_ip}",
               "sub": self._where(self.src_row), "reached": True}
        self._trace_hop(self.src_row, [src], [], ports, 1, frozenset(), True)

    def _final_destination(self, hops, tags, ports, hop_num):
        dst_row = self.dst_row
        for part in self._resolve(self.dst_ip, ports):
            h, t = list(hops), list(tags)
            if part["note"]:
                h.append(self._msg(part["level"], part["note"]))
            if part["tag"]:
                t.append(part["tag"])
            if not part["substituted"]:
                segs = self._check(dst_row, "inbound", self.dst_ip, dst_row, False, part["ports"])
                h.append(self._hop_card(hop_num, dst_row, "Inbound", segs, role="destination"))
                self._finish(h, t, part["ports"])
                continue
            groups, unresolved = self._group(part["ips"])
            for ip in unresolved:
                h.append(self._msg("warn", f"Could not resolve backend IP {ip} to a known "
                                           "subnet, skipping its inbound check."))
            if not groups:
                self._finish(h, t, part["ports"], reached=False, incomplete=True)
                continue
            for g in groups:
                h2, t2 = list(h), list(t)
                ips = g["ips"]
                if len(ips) > 1:
                    h2.append(self._msg("info", f"{', '.join(ips)} share the same subnet/NSG, "
                                                "the result below applies to all of them."))
                t2.append("Backend " + ", ".join(ips))
                segs = self._check(g["row"], "inbound", ips[0], g["row"], False, part["ports"])
                h2.append(self._hop_card(hop_num, g["row"], "Inbound", segs, role="destination"))
                self._finish(h2, t2, part["ports"], incomplete=bool(unresolved))

    def _trace_hop(self, current, hops, tags, ports, hop_num, visited, is_first):
        key = (current["SubnetName"], current["VNetName"])
        if key in visited:
            hops = hops + [self._msg("warn", f"Routing loop detected at '{current['SubnetName']}', "
                                             "stopping the trace.")]
            self._finish(hops, tags, ports, reached=False, incomplete=True)
            return
        visited = visited | {key}
        if hop_num > self.max_hops:
            hops = hops + [self._msg("warn", f"Stopped after {self.max_hops} hops, destination not "
                                             "confirmed reached. The real path may be longer, or "
                                             "there may be a routing loop. Raise Max hops if you "
                                             "expect a longer path.")]
            self._finish(hops, tags, ports, reached=False, incomplete=True)
            return

        direction = "Outbound" if is_first else "Inbound"
        segs = self._check(current, direction.lower(), self.dst_ip, self.dst_row, self.dst_ext, ports)
        foot = None if is_first else ("NSG-only result. If this device runs its own firewall or "
                                      "inspection policy (e.g. Panorama), check that separately.")
        hops = hops + [self._hop_card(hop_num, current, direction, segs, footnote=foot)]

        # Reached the destination's own subnet.
        if not self.dst_ext and current["SubnetName"] == self.dst_row["SubnetName"] \
                and current["VNetName"] == self.dst_row["VNetName"]:
            if is_first:
                self._final_destination(hops, tags, ports, hop_num)
            else:
                self._finish(hops, tags, ports)
            return

        # Same or peered VNet: no more hops needed.
        if not self.dst_ext and can_reach_directly(current, self.dst_row):
            self._final_destination(hops, tags, ports, hop_num + 1)
            return

        route_table = get_route_table(current.get("RouteTableId"))
        route = find_matching_route(route_table, self.dst_ip)
        if not route:
            if self.dst_ext:
                if is_first:
                    hops = hops + [self._msg("info", "No further route table found, assuming this "
                                                     "hop can reach the internet directly.")]
                else:
                    hops = hops + [self._msg("info", "No further route table found here. Azure's "
                                                     "visibility ends at this point, whatever happens "
                                                     "next is up to this device's own policy "
                                                     "(e.g. Panorama).")]
                self._finish(hops, tags, ports)
            else:
                hops = hops + [self._msg("warn", f"No route found from '{current['SubnetName']}' toward "
                                                 f"{self.dst_ip}, and it is not in a peered VNet. The "
                                                 "path cannot be traced further.")]
                self._finish(hops, tags, ports, reached=False, incomplete=True)
            return

        nh = route.get("nextHopType")
        if nh == "VirtualAppliance":
            nva_ip = route.get("nextHopIpAddress")
            hops = hops + [self._msg("info", f"Route table sends traffic to a virtual appliance at "
                                             f"{nva_ip} (route \"{route.get('name')}\").")]
            if not nva_ip:
                hops = hops + [self._msg("warn", "No next-hop IP on this route, the trace stops here.")]
                self._finish(hops, tags, ports, reached=False, incomplete=True)
                return
            for part in self._resolve(nva_ip, ports):
                h, t = list(hops), list(tags)
                if part["note"]:
                    h.append(self._msg(part["level"], part["note"]))
                if part["tag"]:
                    t.append(part["tag"])
                groups, unresolved = self._group(part["ips"])
                for ip in unresolved:
                    h.append(self._msg("warn", f"Could not resolve {ip} to a known subnet, the "
                                               "trace stops here."))
                if not groups:
                    self._finish(h, t, part["ports"], reached=False, incomplete=True)
                    continue
                for g in groups:
                    h2, t2 = list(h), list(t)
                    if len(g["ips"]) > 1:
                        h2.append(self._msg("info", f"{', '.join(g['ips'])} share the same "
                                                    "subnet/NSG, the result below applies to all."))
                    elif len(part["ips"]) > 1 or len(groups) > 1:
                        h2.append(self._msg("info", f"Continuing the trace via {g['ips'][0]}."))
                        t2.append(f"via {g['ips'][0]}")
                    self._trace_hop(g["row"], h2, t2, part["ports"], hop_num + 1, visited, False)
            return

        if nh in ("VnetPeering", "VNetLocal"):
            if self.dst_ext:
                self._finish(hops, tags, ports)
            else:
                self._final_destination(hops, tags, ports, hop_num + 1)
            return
        if nh == "Internet":
            hops = hops + [self._msg("info", "Route table sends this traffic to the internet, no "
                                             "further Azure hops to trace.")]
            self._finish(hops, tags, ports)
            return
        hops = hops + [self._msg("warn", f"Route's next hop type is '{nh}', which this tool does "
                                         "not follow further.")]
        self._finish(hops, tags, ports, reached=False, incomplete=True)


def trace_check(data, src_ip, dst_ip, ports_text, protocol, max_hops):
    """Runs one check. Always returns a result dict, never raises for bad input."""
    res = {"src": src_ip, "dst": dst_ip, "ports_text": ports_text, "protocol": protocol,
           "ports": None, "error": None, "info": [], "paths": [], "notes": []}
    try:
        src_ip = str(ipaddress.ip_address(src_ip.strip()))
        dst_ip = str(ipaddress.ip_address(dst_ip.strip()))
    except ValueError:
        res["error"] = "Source and Destination must both be valid IP addresses."
        return res
    res["src"], res["dst"] = src_ip, dst_ip
    try:
        ports = parse_ports(ports_text)
    except ValueError as e:
        res["error"] = str(e)
        return res
    res["ports"] = ports
    del _notes[:]
    tracer = Tracer(data, src_ip, dst_ip, protocol, max_hops)
    if not tracer.src_row:
        res["error"] = f"Could not find source IP {src_ip} in any known subnet."
        return res
    if tracer.dst_ext:
        res["info"].append(f"Destination {dst_ip} is not in any known Azure subnet, treating it as external.")
    elif tracer.src_row["VNetName"] == tracer.dst_row["VNetName"]:
        res["info"].append("Source and destination are in the same VNet, no peering check needed.")
    tracer.run(ports)
    res["paths"] = tracer.paths
    res["notes"] = list(_notes)
    return res


def path_verdict(path):
    if any(s["access"] == "DENY" for h in path["hops"] if h["kind"] == "hop" for s in h["segments"]):
        return "DENY"
    if path["incomplete"] or any(s["access"] == "ERROR" for h in path["hops"]
                                 if h["kind"] == "hop" for s in h["segments"]):
        return "INCOMPLETE"
    return "ALLOW"


def check_verdict(res):
    """Returns (verdict, summary, fix). verdict: ALLOW, DENY, PARTIAL, INCOMPLETE, ERROR."""
    if res.get("error"):
        return "ERROR", res["error"], ""
    denied, seen, incomplete = [], set(), False
    denied_ports = []
    for p in res["paths"]:
        if p["incomplete"]:
            incomplete = True
        for h in p["hops"]:
            if h["kind"] != "hop":
                continue
            for s in h["segments"]:
                if s["access"] == "ERROR":
                    incomplete = True
                if s["access"] == "DENY":
                    k = (h["nsg"], h["direction"], tuple(s["ports"]))
                    denied_ports += s["ports"]
                    if k not in seen:
                        seen.add(k)
                        denied.append((h["num"], h["nsg"], h["direction"], fmt_ports(s["ports"])))
    flow = f"{res['src']} -> {res['dst']}"
    proto = "any protocol" if res["protocol"] == "*" else res["protocol"]
    if denied:
        summary = "Blocked at " + "; ".join(
            f"hop {n} ({nsg}, {d}) for {pt}" for n, nsg, d, pt in denied[:4])
        if len(denied) > 4:
            summary += f"; and {len(denied) - 4} more"
        fix = "Add an Allow rule: " + "; ".join(
            f"{nsg} ({d}) for {flow}, {pt}/{proto}" for _n, nsg, d, pt in denied[:4])
        allowed = ports_subtract(res["ports"], denied_ports)
        if allowed:
            return "PARTIAL", summary + f". Allowed: {fmt_ports(allowed)}", fix
        return "DENY", summary, fix
    if incomplete:
        return "INCOMPLETE", ("No DENY found, but the trace could not be completed. "
                              "See the notes in the path below."), ""
    return "ALLOW", "ALLOW at every hop checked", ""


# --------------------------------------------------------------------------
# Plain-text output (copy / export)
# --------------------------------------------------------------------------

def card_text(card, ports_label=""):
    k = card["kind"]
    if k == "end":
        return f"{card['title']} ({card['sub']})"
    if k == "msg":
        return card["text"]
    parts = []
    for s in card["segments"]:
        name = {"NONSG": "NO NSG", "ERROR": "UNKNOWN"}.get(s["access"], s["access"])
        parts.append(f"{fmt_ports(s['ports'])}: {name}, {s['text']}")
    label = "Destination NSG" if card["role"] == "destination" else "NSG"
    return (f"Hop {card['num']} {label} {card['nsg']} ({card['sub']}), {card['direction']}: "
            + "; ".join(parts))


def result_text(res):
    verdict, summary, fix = check_verdict(res)
    proto = "any protocol" if res["protocol"] == "*" else res["protocol"]
    lines = [f"{res['src']} -> {res['dst']}  port(s) {res['ports_text']}/{proto}: {verdict}",
             summary]
    if fix:
        lines.append(fix)
    for i in res["info"]:
        lines.append("Note: " + i)
    many = len(res["paths"]) > 1
    for n, p in enumerate(res["paths"], 1):
        if many or p["tags"]:
            lines.append(f"-- Path {n}" + (": " + "; ".join(p["tags"]) if p["tags"] else "")
                         + f" ({path_verdict(p)})")
        for c in p["hops"]:
            lines.append("  " + card_text(c))
    for n in res["notes"]:
        lines.append("Note: " + n)
    return "\n".join(lines)


def parse_protocol(raw):
    cleaned = raw.strip().lower()
    if cleaned in ("", "tcp"):
        return "Tcp"
    if cleaned == "udp":
        return "Udp"
    if cleaned == "any":
        return "*"
    return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

DARK = {"card": "#2a2a2a", "text": "#e6e8eb", "sub": "#9aa0a8", "link": "#555555",
        "ok": "#32b45a", "okbg": "#173d25", "bad": "#f04438", "badbg": "#3a2323",
        "warn": "#f79009", "warnbg": "#3a2f16", "info": "#5b8def", "infobg": "#1c2a44",
        "infotext": "#9cc0ff", "gray": "#7a808a", "badtext": "#ff8a80"}
LIGHT = {"card": "#f0f1f3", "text": "#1f2328", "sub": "#57606a", "link": "#b6bcc4",
         "ok": "#1a7f37", "okbg": "#dcf5e3", "bad": "#cf222e", "badbg": "#fde8e8",
         "warn": "#9a6700", "warnbg": "#fff4d6", "info": "#0b5fff", "infobg": "#e6efff",
         "infotext": "#0a3d99", "gray": "#8c959f", "badtext": "#a40e26"}

PROTOCOLS = ("Tcp", "Udp", "Any")


class App:
    def __init__(self, root):
        self.root = root
        self.data = None
        self.rows = []              # input rows: {"widgets", "vars"}
        self.results = []
        self.inbox = queue.Queue()
        self.loading = False
        self.tracing = False
        self._wrap_labels = []
        self.wrap = 700
        self._card_texts = {}

        root.title("NSG Multihop Checker")
        root.geometry("1100x720")
        root.minsize(760, 520)

        self.pal = self._detect_palette()
        base = tkfont.nametofont("TkDefaultFont")
        self.f_bold = base.copy()
        self.f_bold.configure(weight="bold")
        self.f_small = base.copy()
        self.f_small.configure(size=max(base.cget("size") - 1, 8))

        self._build_ui()
        self.add_row(focus=True)
        self.refresh_data()
        self.root.after(100, self._poll_inbox)

    def _detect_palette(self):
        try:
            bg = ttk.Style().lookup("TFrame", "background") or self.root.cget("bg")
            r, g, b = self.root.winfo_rgb(bg)
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 65535
            return DARK if lum < 0.5 else LIGHT
        except tk.TclError:
            return LIGHT

    # ---- layout ----------------------------------------------------------

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        self.count_var = tk.StringVar(value="Checks")
        ttk.Label(outer, textvariable=self.count_var, font=self.f_bold).pack(anchor="w")

        holder = ttk.Frame(outer)
        holder.pack(fill="x", pady=(4, 6))
        self.holder = holder
        cbg = ttk.Style().lookup("TFrame", "background") or self.root.cget("bg")
        self.icanvas = tk.Canvas(holder, height=130, highlightthickness=0, yscrollincrement=12, bg=cbg)
        iscroll = ttk.Scrollbar(holder, orient="vertical", command=self.icanvas.yview)
        self.icanvas.configure(yscrollcommand=iscroll.set)
        self.icanvas.pack(side="left", fill="x", expand=True)
        iscroll.pack(side="right", fill="y")
        self.iframe = ttk.Frame(self.icanvas)
        self.icanvas.create_window((0, 0), window=self.iframe, anchor="nw")
        self.iframe.bind("<Configure>",
                         lambda e: self.icanvas.configure(scrollregion=self.icanvas.bbox("all")))
        for col, text in enumerate(("", "Source IP", "", "Destination IP", "Port(s)", "Protocol")):
            if text:
                ttk.Label(self.iframe, text=text, font=self.f_small).grid(
                    row=0, column=col, sticky="w", padx=(0, 6))

        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="+ Add check", command=lambda: self.add_row(focus=True)).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left", padx=6)
        self.refresh_btn = ttk.Button(bar, text="Refresh data", command=self.refresh_data)
        self.refresh_btn.pack(side="left")
        self.trace_btn = ttk.Button(bar, text="Trace", command=self.trace_all, state="disabled")
        self.trace_btn.pack(side="right")
        self.hops_var = tk.StringVar(value="10")
        ttk.Spinbox(bar, from_=1, to=30, width=4, textvariable=self.hops_var).pack(side="right", padx=(4, 12))
        ttk.Label(bar, text="Max hops:").pack(side="right")

        ttk.Label(outer, text="Results", font=self.f_bold).pack(anchor="w")
        self.cbg = cbg
        self.tab_canvases = []
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(4, 8))
        self._build_overview()
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.nb.bind(seq, self._on_tab_menu)

        foot = ttk.Frame(outer)
        foot.pack(fill="x")
        ttk.Button(foot, text="Copy summary", command=self.copy_summary).pack(side="left")
        ttk.Button(foot, text="Export CSV", command=self.export_csv).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Starting...")
        ttk.Label(foot, textvariable=self.status).pack(side="right")
        self.progress = ttk.Progressbar(foot, mode="indeterminate", length=90)
        self._bind_shortcuts()

    def _bind_shortcuts(self):
        mac = platform.system() == "Darwin"
        pairs = ([("<Command-Control-f>", self._toggle_fullscreen),
                  ("<Command-Control-F>", self._toggle_fullscreen)] if mac
                 else [("<F11>", self._toggle_fullscreen)])
        for seq, fn in pairs:
            try:
                self.root.bind_all(seq, fn)
            except tk.TclError:
                pass

    def _toggle_fullscreen(self, event=None):
        self.root.attributes("-fullscreen", not bool(self.root.attributes("-fullscreen")))
        return "break"

    # ---- input rows ------------------------------------------------------

    def add_row(self, focus=False, values=None):
        vs = {"src": tk.StringVar(), "dst": tk.StringVar(),
              "port": tk.StringVar(value="443"), "proto": tk.StringVar(value="Tcp")}
        if values:
            for k, v in values.items():
                vs[k].set(v)
        w = {"num": ttk.Label(self.iframe, width=3, anchor="e")}
        w["src"] = ttk.Entry(self.iframe, textvariable=vs["src"], width=17)
        w["arrow"] = ttk.Label(self.iframe, text="->")
        w["dst"] = ttk.Entry(self.iframe, textvariable=vs["dst"], width=17)
        w["port"] = ttk.Entry(self.iframe, textvariable=vs["port"], width=16)
        w["proto"] = ttk.Combobox(self.iframe, textvariable=vs["proto"], values=PROTOCOLS,
                                  width=6, state="readonly")
        row = {"w": w, "v": vs}
        w["x"] = ttk.Button(self.iframe, text="x", width=3, command=lambda r=row: self.remove_row(r))
        for key in ("src", "dst", "port"):
            w[key].bind("<Return>", lambda e: self.trace_all())
        for v in vs.values():
            v.trace_add("write", lambda *a: self._update_count())
        self.rows.append(row)
        self._regrid()
        if focus:
            w["src"].focus_set()
            self.root.after_idle(lambda: self._sync_scroll(to="bottom"))

    def _regrid(self):
        order = ("num", "src", "arrow", "dst", "port", "proto", "x")
        for i, row in enumerate(self.rows, start=1):
            row["w"]["num"].config(text=f"{i}.")
            for col, key in enumerate(order):
                row["w"][key].grid(row=i, column=col, sticky="w", padx=(0, 6), pady=2)
        self._update_count()

    def remove_row(self, row):
        if len(self.rows) == 1:
            for v in row["v"].values():
                v.set("")
            row["v"]["port"].set("443")
            row["v"]["proto"].set("Tcp")
            return
        for wdg in row["w"].values():
            wdg.destroy()
        self.rows.remove(row)
        self._regrid()
        self.root.after_idle(lambda: self._sync_scroll())

    def clear_all(self):
        for row in self.rows[1:]:
            for wdg in row["w"].values():
                wdg.destroy()
        self.rows = self.rows[:1]
        for k, v in self.rows[0]["v"].items():
            v.set({"port": "443", "proto": "Tcp"}.get(k, ""))
        self._regrid()
        self._clear_results()
        self.rows[0]["w"]["src"].focus_set()
        self.root.after_idle(lambda: self._sync_scroll(to="top"))

    def _update_count(self):
        filled = sum(1 for r in self.rows if r["v"]["src"].get().strip() or r["v"]["dst"].get().strip())
        self.count_var.set(f"Checks ({filled})" if filled else "Checks")

    def _sync_scroll(self, to=None):
        self.icanvas.update_idletasks()
        self.icanvas.configure(scrollregion=self.icanvas.bbox("all"))
        if to == "top" or self.iframe.winfo_reqheight() <= self.icanvas.winfo_height():
            self.icanvas.yview_moveto(0)
        elif to == "bottom":
            self.icanvas.yview_moveto(1.0)
        else:
            self.icanvas.yview_moveto(min(self.icanvas.yview()[0], 1.0))

    def _scroll_target(self, x_root, y_root):
        w = self.root.winfo_containing(x_root, y_root)
        while w is not None:
            if w is self.holder:
                return self.icanvas
            if w in self.tab_canvases:
                return w
            w = w.master
        return None

    def _on_mousewheel(self, event):
        canvas = self._scroll_target(event.x_root, event.y_root)
        if canvas is None:
            return None
        if event.num == 4:
            units = -3
        elif event.num == 5:
            units = 3
        else:
            d = event.delta
            if d == 0:
                return None
            step = (abs(d) // 120) * 3 if abs(d) >= 120 else max(1, abs(d)) * 2
            units = -step if d > 0 else step
        canvas.yview_scroll(units, "units")
        return "break"

    # ---- loading data ----------------------------------------------------

    def refresh_data(self):
        if self.loading:
            return
        self.loading = True
        self.trace_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self.status.set("Loading subnets from Azure...")

        def work():
            self.inbox.put(("loaded",) + load_data())

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, data, err, warnings):
        self.loading = False
        self.refresh_btn.config(state="normal")
        if err:
            self.status.set("Could not load subnets")
            hint = ""
            low = err.lower()
            if "az login" in low or "please run" in low or "authenticat" in low:
                hint = "\n\nYour Azure login may have expired. Run 'az login' in a terminal, then click Refresh data."
            messagebox.showerror("Azure error", err[:1200] + hint)
            return
        self.data = data
        extra = f"  ({', '.join(warnings)})" if warnings else ""
        self.status.set(f"{len(data['rows'])} subnets loaded{extra}")
        self.trace_btn.config(state="normal")

    # ---- tracing ---------------------------------------------------------

    def trace_all(self):
        if self.tracing or self.data is None:
            return
        checks = []
        for r in self.rows:
            v = {k: x.get().strip() for k, x in r["v"].items()}
            if not v["src"] and not v["dst"]:
                continue
            checks.append(v)
        if not checks:
            self.status.set("Enter at least one check")
            return
        try:
            max_hops = max(1, min(30, int(self.hops_var.get())))
        except ValueError:
            max_hops = 10
        self._clear_results()
        self.tracing = True
        self.trace_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self.progress.pack(side="right", padx=8)
        self.progress.start(12)
        data = self.data

        def work():
            clear_cache()       # always read the current NSG rules, never a stale copy
            for i, c in enumerate(checks, 1):
                self.inbox.put(("status", f"Tracing {i} of {len(checks)}: {c['src']} -> {c['dst']} ..."))
                proto = parse_protocol(c["proto"]) or "Tcp"
                try:
                    res = trace_check(data, c["src"], c["dst"], c["port"], proto, max_hops)
                except Exception as e:      # never let one bad check kill the batch
                    res = {"src": c["src"], "dst": c["dst"], "ports_text": c["port"],
                           "protocol": proto, "ports": None, "paths": [], "info": [],
                           "notes": [], "error": f"Unexpected error: {e}\n{traceback.format_exc(limit=3)}"}
                self.inbox.put(("result", res))
            self.inbox.put(("done", len(checks)))

        threading.Thread(target=work, daemon=True).start()

    def _poll_inbox(self):
        try:
            while True:
                msg = self.inbox.get_nowait()
                kind = msg[0]
                if kind == "loaded":
                    self._on_loaded(msg[1], msg[2], msg[3])
                elif kind == "status":
                    self.status.set(msg[1])
                elif kind == "result":
                    self.results.append(msg[1])
                    self._add_tab(msg[1], len(self.results))
                elif kind == "done":
                    self.tracing = False
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.trace_btn.config(state="normal")
                    self.refresh_btn.config(state="normal")
                    self.status.set(f"Done, {msg[1]} check(s) traced")
                    # One check: go straight to it. Several: start on the overview.
                    self.nb.select(1 if len(self.results) == 1 else 0)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_inbox)

    # ---- drawing results -------------------------------------------------

    def _build_overview(self):
        """First tab: one row per check, double-click a row to open its tab."""
        P = self.pal
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="All checks")
        self.hint = ttk.Label(tab, foreground="gray", justify="left",
                              text="Enter a source, destination and port, then click Trace.\n"
                                   "Ports can be one port (443), a range (8000-8100), a list (80,443) or *.")
        self.hint.pack(anchor="w", padx=8, pady=8)
        cols = ("n", "src", "dst", "ports", "result", "detail")
        self.ov = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for key, title, width, stretch in (("n", "#", 36, False), ("src", "Source", 110, False),
                                           ("dst", "Destination", 110, False), ("ports", "Port(s)", 180, False),
                                           ("result", "Result", 100, False), ("detail", "Detail", 400, True)):
            self.ov.heading(key, text=title, anchor="w")
            self.ov.column(key, width=width, minwidth=width, stretch=stretch, anchor="w")
        for name, colr in (("ALLOW", P["ok"]), ("DENY", P["bad"]), ("PARTIAL", P["bad"]),
                           ("ERROR", P["bad"]), ("INCOMPLETE", P["warn"])):
            self.ov.tag_configure(name, foreground=colr)
        # Full detail of the selected row, since the Detail column can be cut off.
        self.ov_detail = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.ov_detail, wraplength=900, justify="left").pack(
            side="bottom", fill="x", padx=8, pady=(4, 6))
        ovs = ttk.Scrollbar(tab, orient="vertical", command=self.ov.yview)
        self.ov.configure(yscrollcommand=ovs.set)
        self.ov.pack(side="left", fill="both", expand=True)
        ovs.pack(side="right", fill="y")
        self.ov.bind("<<TreeviewSelect>>", self._on_overview_select)
        self.ov.bind("<Double-Button-1>", self._on_overview_open)
        self.ov.bind("<Return>", self._on_overview_open)

    def _on_overview_select(self, event=None):
        sel = self.ov.selection()
        if sel and 1 <= int(sel[0]) <= len(self.results):
            self.ov_detail.set(check_verdict(self.results[int(sel[0]) - 1])[1])
        else:
            self.ov_detail.set("")

    def _on_overview_open(self, event=None):
        sel = self.ov.selection()
        if sel:
            self.nb.select(int(sel[0]))      # tab index == check number

    def _clear_results(self):
        self.results = []
        self._wrap_labels = []
        for tab_id in self.nb.tabs()[1:]:
            tab = self.root.nametowidget(tab_id)
            self.nb.forget(tab_id)
            tab.destroy()
        self.tab_canvases = []
        self.ov.delete(*self.ov.get_children())
        self.ov_detail.set("")
        self.hint.pack(anchor="w", padx=8, pady=8, before=self.ov)
        self.nb.select(0)

    def _on_tab_resize(self, canvas, win, event):
        canvas.itemconfigure(win, width=event.width)
        self.wrap = max(240, event.width - 40)
        alive = []
        for lbl, off in self._wrap_labels:
            try:
                lbl.config(wraplength=max(120, self.wrap - off))
                alive.append((lbl, off))
            except tk.TclError:
                pass
        self._wrap_labels = alive

    def _add_tab(self, res, n):
        verdict, summary, _fix = check_verdict(res)
        tab = ttk.Frame(self.nb)
        canvas = tk.Canvas(tab, highlightthickness=0, yscrollincrement=14, bg=self.cbg)
        sb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        canvas.bind("<Configure>", lambda e, c=canvas, w=win: self._on_tab_resize(c, w, e))
        self.tab_canvases.append(canvas)
        self.nb.add(tab, text=f"{n}  {verdict}")
        self._add_block(res, inner)
        # Overview row.
        self.hint.pack_forget()
        proto = "Any" if res["protocol"] == "*" else res["protocol"]
        self.ov.insert("", "end", iid=str(n), tags=(verdict,),
                       values=(n, res["src"], res["dst"], f"{res['ports_text']}/{proto}",
                               verdict, summary.replace("\n", " ")))

    def _on_tab_menu(self, event):
        try:
            idx = self.nb.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        if idx < 1 or idx > len(self.results):
            return None
        res = self.results[idx - 1]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy this check", command=lambda: self._copy(result_text(res), "Copied check"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _lbl(self, parent, text, bg, fg, offset, bold=False, small=False, **kw):
        """
        Read-only text you can highlight and copy (drag, double-click a word,
        triple-click a line, Cmd/Ctrl+C). It is a Text widget that grows to fit
        its wrapped content, not a Label, because Labels can't be selected.
        """
        font = self.f_bold if bold else (self.f_small if small else tkfont.nametofont("TkDefaultFont"))
        t = tk.Text(parent, bg=bg, fg=fg, font=font, wrap="word", height=1, width=1,
                    borderwidth=0, highlightthickness=0, padx=0, pady=0, relief="flat",
                    cursor="xterm", insertwidth=0, takefocus=0, exportselection=False)
        t._font = font
        t.insert("1.0", text)

        def keys(e):
            # Allow copy / select-all and navigation, block anything that edits.
            if (e.state & 0x4 or e.state & 0x8) and e.keysym.lower() in ("c", "a"):
                return None
            if e.keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
                            "Shift_L", "Shift_R", "Control_L", "Control_R", "Meta_L", "Meta_R"):
                return None
            return "break"

        t.bind("<Key>", keys)
        for ev in ("<<Paste>>", "<<Cut>>"):
            t.bind(ev, lambda e: "break")
        t._fit_pending = False
        t._fit_width = -1

        def on_configure(e, w=t):
            # Re-fit only when the WIDTH changed. A height change must never
            # trigger another fit, or resizing would loop forever.
            if e.width != w._fit_width and not w._fit_pending:
                w._fit_pending = True
                w.after_idle(self._fit_text, w)

        t.bind("<Configure>", on_configure)
        t.bind("<FocusOut>", lambda e, w=t: w.tag_remove("sel", "1.0", "end"))
        return t

    @staticmethod
    def _wrapped_lines(font, text, width):
        """How many lines `text` takes when word-wrapped to `width` pixels."""
        total = 0
        for para in text.split("\n"):
            cur, lines = "", 1
            for word in para.split(" "):
                trial = word if not cur else cur + " " + word
                if font.measure(trial) <= width:
                    cur = trial
                    continue
                if cur:                         # start a new line with this word
                    lines += 1
                cur = word
                while font.measure(cur) > width and len(cur) > 1:   # very long word
                    k = max(1, int(len(cur) * width / max(font.measure(cur), 1)))
                    cur = cur[k:]
                    lines += 1
            total += lines
        return total

    def _fit_text(self, t):
        """Sets the Text height to the number of wrapped lines it needs.
        Done with plain font measurement (no Tk 'count -displaylines', which has
        crashed some macOS Tk builds)."""
        t._fit_pending = False
        try:
            width = t.winfo_width()
            if width < 30:
                return                          # not laid out yet
            t._fit_width = width
            n = self._wrapped_lines(t._font, t.get("1.0", "end-1c"), int(width * 0.97) - 2)
            if n != int(t.cget("height")):
                t.configure(height=n)
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _box(self, parent, edge, bg, indent=14):
        outer = tk.Frame(parent, bg=bg)
        tk.Frame(outer, bg=edge, width=5).pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=bg)
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=6)
        outer.pack(fill="x", padx=(indent, 4))
        return outer, inner

    def _tag(self, parent, access, bg):
        P = self.pal
        name, fg, tbg = {
            "ALLOW": ("ALLOW", P["ok"], P["okbg"]),
            "DENY": ("DENY", P["bad"], P["badbg"]),
            "NONSG": ("NO NSG", P["ok"], P["okbg"]),
            "ERROR": ("UNKNOWN", P["warn"], P["warnbg"]),
        }[access]
        return tk.Label(parent, text=f" {name} ", bg=tbg, fg=fg, font=self.f_bold)

    def _link(self, parent):
        tk.Frame(parent, bg=self.pal["link"], width=2, height=10).pack(anchor="w", padx=(26, 0))

    def _add_block(self, res, parent):
        P = self.pal
        verdict, summary, fix = check_verdict(res)
        colors = {"ALLOW": (P["ok"], P["okbg"]), "DENY": (P["bad"], P["badbg"]),
                  "PARTIAL": (P["bad"], P["badbg"]), "INCOMPLETE": (P["warn"], P["warnbg"]),
                  "ERROR": (P["bad"], P["badbg"])}
        fg, bg = colors[verdict]
        block = ttk.Frame(parent)
        block.pack(fill="x", pady=(4, 16), padx=4)

        banner = tk.Frame(block, bg=bg, highlightbackground=fg, highlightthickness=1)
        banner.pack(fill="x")
        proto = "any protocol" if res["protocol"] == "*" else res["protocol"]
        label = {"PARTIAL": "PARTIALLY BLOCKED"}.get(verdict, verdict)
        head = f"{label}   {res['src']} -> {res['dst']}   port(s) {res['ports_text']} / {proto}"
        self._lbl(banner, head, bg, fg, 40, bold=True).pack(fill="x", padx=12, pady=(8, 2))
        self._lbl(banner, summary, bg, P["text"], 40).pack(fill="x", padx=12, pady=(0, 2 if fix else 8))
        if fix:
            self._lbl(banner, fix, bg, P["badtext"], 40).pack(fill="x", padx=12, pady=(0, 8))
        self._bind_menu(banner, lambda e, r=res: self._menu(e, None, r))

        chain = ttk.Frame(block)
        chain.pack(fill="x", pady=(8, 0))
        for text in res["info"]:
            self._msg_card(chain, {"level": "info", "text": text})
        many = len(res["paths"]) > 1
        for n, path in enumerate(res["paths"], 1):
            if many or path["tags"]:
                pv = path_verdict(path)
                head = f"Path {n} of {len(res['paths'])}" if many else "Path"
                if path["tags"]:
                    head += ":  " + "  |  ".join(path["tags"])
                row = ttk.Frame(chain)
                row.pack(fill="x", padx=(14, 0), pady=(6, 4))
                ttk.Label(row, text=head, font=self.f_bold).pack(side="left")
                self._tag(row, {"ALLOW": "ALLOW", "DENY": "DENY", "INCOMPLETE": "ERROR"}[pv], None
                          ).pack(side="left", padx=8)
            for i, card in enumerate(path["hops"]):
                if i:
                    self._link(chain)
                self._draw_card(chain, card, res)
        for text in res["notes"]:
            self._msg_card(chain, {"level": "warn", "text": text})

    def _msg_card(self, parent, card):
        P = self.pal
        warn = card["level"] == "warn"
        edge, bg, fg = ((P["warn"], P["warnbg"], P["warn"]) if warn
                        else (P["info"], P["infobg"], P["infotext"]))
        outer, inner = self._box(parent, edge, bg, indent=40)
        self._lbl(inner, ("Warning: " if warn else "") + card["text"], bg, fg, 90).pack(fill="x")
        return outer

    def _draw_card(self, parent, card, res):
        P = self.pal
        kind = card["kind"]
        if kind == "msg":
            self._msg_card(parent, card)
            return
        if kind == "end":
            edge = P["info"] if card["reached"] else P["gray"]
            outer, inner = self._box(parent, edge, P["card"])
            self._lbl(inner, card["title"], P["card"], P["text"], 60, bold=True).pack(fill="x")
            self._lbl(inner, card["sub"], P["card"], P["sub"], 60, small=True).pack(fill="x")
            self._bind_menu(outer, lambda e, c=card, r=res: self._menu(e, c, r))
            return
        accesses = [s["access"] for s in card["segments"]]
        if "DENY" in accesses:
            edge, bg = P["bad"], P["badbg"]
        elif "ERROR" in accesses:
            edge, bg = P["warn"], P["warnbg"]
        else:
            edge, bg = P["ok"], P["card"]
        outer, inner = self._box(parent, edge, bg)
        top = tk.Frame(inner, bg=bg)
        top.pack(fill="x")
        role = "Destination NSG" if card["role"] == "destination" else "NSG"
        if len(card["segments"]) == 1:
            self._tag(top, card["segments"][0]["access"], bg).pack(side="right")
        self._lbl(top, f"Hop {card['num']}:  {role}  {card['nsg']}", bg, P["text"], 150,
                  bold=True).pack(side="left", fill="x", expand=True)
        self._lbl(inner, f"{card['sub']}   |   {card['direction']}", bg, P["sub"], 60,
                  small=True).pack(fill="x")
        single = len(card["segments"]) == 1
        for s in card["segments"]:
            line = tk.Frame(inner, bg=bg)
            line.pack(fill="x", pady=(3, 0))
            if not single:
                self._tag(line, s["access"], bg).pack(side="left", padx=(0, 8))
                text = f"{fmt_ports(s['ports'])}:  {s['text']}"
            else:
                text = s["text"]
            fg = P["badtext"] if s["access"] == "DENY" else P["text"]
            self._lbl(line, text, bg, fg, 120).pack(side="left", fill="x", expand=True)
        if card.get("footnote"):
            self._lbl(inner, card["footnote"], bg, P["sub"], 60, small=True).pack(fill="x", pady=(4, 0))
        self._bind_menu(outer, lambda e, c=card, r=res: self._menu(e, c, r))

    # ---- copy / export ---------------------------------------------------

    def _bind_menu(self, widget, handler):
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            widget.bind(seq, handler)
        for child in widget.winfo_children():
            self._bind_menu(child, handler)

    def _menu(self, event, card, res):
        menu = tk.Menu(self.root, tearoff=0)
        if card is not None:
            menu.add_command(label="Copy this card",
                             command=lambda: self._copy(card_text(card), "Copied"))
        menu.add_command(label="Copy this whole check",
                         command=lambda: self._copy(result_text(res), "Copied check"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _copy(self, text, label):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set(label)

    def copy_summary(self):
        if not self.results:
            self.status.set("Nothing to copy yet")
            return
        self._copy("\n\n".join(result_text(r) for r in self.results),
                   f"Copied summary of {len(self.results)} check(s)")

    def export_csv(self):
        if not self.results:
            self.status.set("Nothing to export yet")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="nsg_multihop_results.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["check", "source", "destination", "ports_requested", "protocol", "verdict",
                        "path", "hop", "nsg", "direction", "segment_ports", "result", "rule", "detail"])
            for i, res in enumerate(self.results, 1):
                verdict = check_verdict(res)[0]
                base = [i, res["src"], res["dst"], res["ports_text"], res["protocol"], verdict]
                if res.get("error") or not res["paths"]:
                    w.writerow(base + ["", "", "", "", "", "", "", res.get("error") or ""])
                    n += 1
                    continue
                for pn, p in enumerate(res["paths"], 1):
                    plabel = f"{pn}: " + "; ".join(p["tags"]) if p["tags"] else str(pn)
                    for c in p["hops"]:
                        if c["kind"] == "hop":
                            for s in c["segments"]:
                                w.writerow(base + [plabel, c["num"], c["nsg"], c["direction"],
                                                   fmt_ports(s["ports"]), s["access"], s["rule"] or "",
                                                   s["text"]])
                                n += 1
                        elif c["kind"] == "msg":
                            w.writerow(base + [plabel, "", "", "", "", c["level"].upper(), "", c["text"]])
                            n += 1
        self.status.set(f"Exported {n} row(s) to {path}")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
