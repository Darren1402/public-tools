#!/usr/bin/env python3
"""
nsg_check_multihop.py -- run locally or in Cloud Shell. Requires: az login, python3.
No pip installs needed, standard library only.

Traces the FULL path from source to destination, following route tables
hop by hop (not capped at 3), instead of only checking source NSG, one
firewall hop, and destination NSG.

At each hop it checks the NSG there, then asks "where does this subnet's
route table send traffic next." It keeps following that chain until it
reaches the destination, runs out of routing information, or hits the
max-hop safety limit.

LIMITS, same as always:
- Cannot see inside a firewall's own policy (e.g. Panorama). Only checks
  the NSG at each Azure-visible hop.
- A hop only appears if an actual Azure route table points to it. Anything
  a firewall does internally (NAT, forwarding out a different interface)
  with no corresponding Azure route is invisible to this script.
- Read-only. Nothing is created, changed, or deleted.

USAGE
    python3 nsg_check_multihop.py
    python3 nsg_check_multihop.py --batch mychecks.csv
    python3 nsg_check_multihop.py --max-hops 15
"""

import subprocess, json, ipaddress, sys, argparse, csv

def az(args, quiet=False):
    result = subprocess.run(args, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        if not quiet:
            print("Something went wrong running:", " ".join(args))
            print(result.stderr)
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None

subprocess.run(["az", "extension", "add", "--name", "resource-graph", "-y"], capture_output=True, shell=True)

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


def run_resource_graph_query_all_pages(query, max_pages=25):
    all_rows = []
    skip_token = None
    for _ in range(max_pages):
        cmd = ["az", "graph", "query", "-q", query, "-o", "json"]
        if skip_token:
            cmd += ["--skip-token", skip_token]
        result = az(cmd)
        if result is None:
            break
        all_rows.extend(result.get("data", []))
        skip_token = result.get("skip_token") or result.get("skipToken") or result.get("$skipToken")
        if not skip_token:
            break
    return all_rows


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
    """Returns {ip: set(asg_id.lower())} from the ASG_QUERY results."""
    membership = {}
    for row in asg_rows:
        ip = row.get("PrivateIP")
        asg_id = row.get("ASGId")
        if not ip or not asg_id:
            continue
        membership.setdefault(ip, set()).add(asg_id.lower())
    return membership


def asg_matches(rule, asg_field, ip, asg_membership):
    """True if this rule's ASG list (source or destination) includes an ASG the IP belongs to."""
    asg_refs = rule.get(asg_field) or []
    if not asg_refs:
        return False
    ip_asgs = asg_membership.get(ip, set())
    if not ip_asgs:
        return False
    for ref in asg_refs:
        if ref.get("id", "").lower() in ip_asgs:
            return True
    return False


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


_peering_cache = {}
_peering_warned = set()

def get_peered_vnet_names(resource_group, vnet_name):
    key = (resource_group, vnet_name)
    if key in _peering_cache:
        return _peering_cache[key]
    data = az(["az", "network", "vnet", "peering", "list",
               "--resource-group", resource_group, "--vnet-name", vnet_name, "-o", "json"],
              quiet=True)
    if data is None:
        if key not in _peering_warned:
            print(f"  [i] Could not confirm peering for VNet '{vnet_name}' (resource group "
                  f"'{resource_group}') -- Azure could not find it there, this may be a Resource")
            print("      Graph indexing lag rather than a real problem. Continuing without peering")
            print("      info for this hop, only same-VNet and explicit CIDR rules will be checked.")
            _peering_warned.add(key)
        data = []
    peered = set()
    for p in data:
        if p.get("peeringState") == "Connected":
            remote_id = p.get("remoteVirtualNetwork", {}).get("id", "")
            peered.add(remote_id.split("/")[-1])
    _peering_cache[key] = peered
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
            if owner_vnet == self_vnet_name:
                return True
            if owner_vnet in get_peered_names():
                return True
            continue
        try:
            if target in ipaddress.ip_network(v, strict=False):
                return True
        except ValueError:
            continue
    return False


def check_nsg_direction(nsg_row, direction, src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership):
    if nsg_row["NSG_Name"] == "NO NSG ATTACHED":
        return "No NSG attached -- not restricted here.", None
    self_vnet_name = nsg_row["VNetName"]

    _peered_result = {}
    def get_peered_names():
        if "value" not in _peered_result:
            _peered_result["value"] = get_peered_vnet_names(nsg_row["ResourceGroup"], self_vnet_name)
        return _peered_result["value"]

    nsg = az(["az", "network", "nsg", "show", "--ids", nsg_row["NSG_Id"], "-o", "json"])
    if nsg is None:
        return "Could not read this NSG's rules.", None
    rules = (nsg.get("securityRules") or []) + (nsg.get("defaultSecurityRules") or [])
    rules = [r for r in rules if r["direction"].lower() == direction]
    rules.sort(key=lambda r: r["priority"])
    for r in rules:
        if r["protocol"].lower() not in ("*", protocol.lower()):
            continue
        if not port_matches(r, port):
            continue
        src_addr_match = ip_matches(r, src_ip, "sourceAddressPrefix", "sourceAddressPrefixes",
                                     src_row, self_vnet_name, get_peered_names)
        src_asg_match = asg_matches(r, "sourceApplicationSecurityGroups", src_ip, asg_membership)
        src_match = src_addr_match or src_asg_match

        dst_addr_match = ip_matches(r, dst_ip, "destinationAddressPrefix", "destinationAddressPrefixes",
                                     (None if dst_is_external else dst_row), self_vnet_name, get_peered_names)
        dst_asg_match = asg_matches(r, "destinationApplicationSecurityGroups", dst_ip, asg_membership)
        dst_match = dst_addr_match or dst_asg_match

        if not src_match or not dst_match:
            continue
        access = r["access"].upper()
        via = []
        if src_asg_match and not src_addr_match:
            via.append("source ASG")
        if dst_asg_match and not dst_addr_match:
            via.append("destination ASG")
        via_note = f" (matched via {', '.join(via)})" if via else ""
        return f'{access} -- matched rule "{r["name"]}" (priority {r["priority"]}){via_note}', access
    return "DENY -- no matching rule (falls to default deny)", "DENY"


def can_reach_directly(current_row, dst_row):
    """True if current subnet's VNet is the same as, or peered with, the destination's VNet."""
    if current_row["VNetName"] == dst_row["VNetName"]:
        return True
    peered = get_peered_vnet_names(current_row["ResourceGroup"], current_row["VNetName"])
    return dst_row["VNetName"] in peered


def resolve_ip_via_generic_show(resource_id):
    """Generic read of any resource by ID -- used to get a NIC ipConfiguration's private IP."""
    result = az(["az", "resource", "show", "--ids", resource_id, "-o", "json"])
    if not result:
        return None
    return result.get("properties", {}).get("privateIPAddress")


def check_floating_ip(lb, dst_ip, port, protocol):
    """
    Looks up the LB rule matching this port/protocol and returns:
      ("floating_on", None)                 -- frontend IP is correct as-is
      ("floating_off", [backend_ip, ...])    -- re-check these instead
      ("unknown", None)                      -- couldn't determine, warn generically
    """
    lb_full = az(["az", "resource", "show", "--ids", lb["LBId"], "-o", "json"])
    if not lb_full:
        return "unknown", None
    rules = lb_full.get("properties", {}).get("loadBalancingRules", []) or []

    matching = None
    for r in rules:
        props = r.get("properties", r)
        if props.get("protocol", "").lower() not in (protocol.lower(), "all", "*"):
            continue
        frontend_port = props.get("frontendPort")
        # HA Ports rules cover every port at once, represented as frontend port 0.
        if frontend_port in (0, "0", None) or str(frontend_port) == str(port):
            matching = props
            break
    if not matching:
        return "unknown", None

    if matching.get("enableFloatingIP"):
        return "floating_on", None

    pool_ref = matching.get("backendAddressPool") or {}
    pool_id = pool_ref.get("id")
    if not pool_id:
        return "unknown", None
    pool = az(["az", "resource", "show", "--ids", pool_id, "-o", "json"])
    if not pool:
        return "unknown", None
    backend_configs = pool.get("properties", {}).get("backendIPConfigurations", []) or []
    backend_ips = []
    for cfg in backend_configs:
        ip = resolve_ip_via_generic_show(cfg["id"])
        if ip:
            backend_ips.append(ip)
    if not backend_ips:
        return "unknown", None
    return "floating_off", backend_ips


def resolve_effective_destination(dst_ip, port, protocol, lb_rows):
    """
    If dst_ip is an LB frontend, checks Floating IP and returns the real
    IP(s) the NSG actually evaluates. Otherwise returns [dst_ip] unchanged.
    """
    matching_lbs = [r for r in lb_rows if r.get("PrivateIP") == dst_ip]
    if not matching_lbs:
        return [dst_ip]

    lb = matching_lbs[0]
    print(f"\n[!] {dst_ip} is the FRONTEND IP of Load Balancer '{lb['LBName']}'.")
    status, backend_ips = check_floating_ip(lb, dst_ip, port, protocol)
    if status == "floating_on":
        print("    Floating IP (DSR) is ON for the matching LB rule -- this frontend IP")
        print("    is genuinely what the NSG evaluates. Checking it as given.")
        return [dst_ip]
    elif status == "floating_off":
        print("    Floating IP (DSR) is OFF for the matching LB rule -- the NSG actually")
        print(f"    evaluates the backend VM's own IP. Found backend IP(s): {', '.join(backend_ips)}")
        print("    Automatically tracing to the backend IP(s) instead.")
        return backend_ips
    else:
        print("    Could not determine the Floating IP setting for the matching rule (or no")
        print("    rule matched this port/protocol -- e.g. it may be an Inbound NAT rule,")
        print("    which this script doesn't check yet). Verify manually if unsure.")
        return [dst_ip]


def trace_path(src_ip, dst_ip_original, port, protocol, rows, lb_rows, asg_membership, max_hops):
    effective_dsts = resolve_effective_destination(dst_ip_original, port, protocol, lb_rows)
    for dst_ip in effective_dsts:
        if len(effective_dsts) > 1:
            print(f"\n--- Tracing to backend IP {dst_ip} ---")
        trace_single_path(src_ip, dst_ip, port, protocol, rows, lb_rows, asg_membership, max_hops)


def trace_single_path(src_ip, dst_ip, port, protocol, rows, lb_rows, asg_membership, max_hops):
    src_row = find(src_ip, rows)
    if not src_row:
        print(f"Could not find source IP {src_ip} in any known subnet.")
        return

    dst_row = find(dst_ip, rows)
    dst_is_external = dst_row is None
    if dst_is_external:
        print(f"\nDestination IP {dst_ip} isn't in any known Azure subnet -- treating as external.")

    print(f"\nSource      -> VNet: {src_row['VNetName']}  Subnet: {src_row['SubnetName']}  NSG: {src_row['NSG_Name']}")
    if not dst_is_external:
        print(f"Destination -> VNet: {dst_row['VNetName']}  Subnet: {dst_row['SubnetName']}  NSG: {dst_row['NSG_Name']}")
        if src_row["VNetName"] == dst_row["VNetName"]:
            print("Note: source and destination are in the same VNet -- no peering check needed.")

    any_deny = [False]
    _trace_hop(src_row, src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol,
               rows, lb_rows, asg_membership, hop_num=1, visited=set(), is_first=True,
               any_deny=any_deny, max_hops=max_hops)

    print(f"\n{'DENY somewhere in the path' if any_deny[0] else 'ALLOW at every hop checked'} for {src_ip} -> {dst_ip} port {port}/{protocol}.")
    if any_deny[0]:
        print("Add an Allow rule at whichever hop above showed DENY.")
    print("Reminder: this only proves what Azure NSGs allow. Any firewall's own policy")
    print("(e.g. Panorama) along the way still needs to be checked separately.")


def _trace_hop(current, src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol,
                rows, lb_rows, asg_membership, hop_num, visited, is_first, any_deny, max_hops):
    key = current["SubnetName"] + current["VNetName"]
    if key in visited:
        print(f"\n[!] Routing loop detected at '{current['SubnetName']}' -- stopping trace.")
        return
    visited = visited | {key}

    if hop_num > max_hops:
        print(f"\n[!] Stopped after {max_hops} hops -- destination not confirmed reached.")
        print("    This may mean the real path is longer than expected, or there's a")
        print("    routing loop. Increase --max-hops if you expect a longer real path.")
        return

    print(f"\nHop {hop_num} -- NSG '{current['NSG_Name']}' (subnet: {current['SubnetName']}, VNet: {current['VNetName']}):")

    if is_first:
        result_text, access = check_nsg_direction(current, "outbound", src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership)
        print(f"  Outbound: {result_text}")
        if access == "DENY":
            any_deny[0] = True
    else:
        result_text, access = check_nsg_direction(current, "inbound", src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership)
        print(f"  Inbound:  {result_text}")
        if access == "DENY":
            any_deny[0] = True
        print("  (NSG-only result -- if this device runs its own firewall or inspection")
        print("   policy, e.g. Panorama, that still needs to be checked separately)")

    # Reached the destination's own subnet -- do the final inbound check and stop.
    if not dst_is_external and current["SubnetName"] == dst_row["SubnetName"] and current["VNetName"] == dst_row["VNetName"]:
        if is_first:
            result_text, access = check_nsg_direction(current, "inbound", src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership)
            print(f"  Inbound:  {result_text}")
            if access == "DENY":
                any_deny[0] = True
        return

    # Can we reach the destination directly from here (same/peered VNet), no more hops needed?
    if not dst_is_external and can_reach_directly(current, dst_row):
        print(f"\nHop {hop_num + 1} -- Destination NSG '{dst_row['NSG_Name']}' (subnet: {dst_row['SubnetName']}):")
        result_text, access = check_nsg_direction(dst_row, "inbound", src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership)
        print(f"  Inbound:  {result_text}")
        if access == "DENY":
            any_deny[0] = True
        return

    # Otherwise, consult this hop's route table to find the next hop.
    route_table = get_route_table(current.get("RouteTableId"))
    matched_route = find_matching_route(route_table, dst_ip)

    if not matched_route:
        if dst_is_external:
            if is_first:
                print("\nNo further route table found -- assuming this hop can reach the internet directly.")
            else:
                print("\nNo further route table found here. Azure's visibility ends at this point --")
                print("whatever happens next is entirely up to this device's own policy (e.g. Panorama).")
        else:
            print(f"\n[!] No route found from '{current['SubnetName']}' toward {dst_ip}, and it's not in a")
            print("    peered VNet. The path cannot be traced further with the routing info available.")
        return

    next_hop_type = matched_route.get("nextHopType")
    if next_hop_type == "VirtualAppliance":
        nva_ip = matched_route.get("nextHopIpAddress")
        print(f"  Route table sends traffic to a virtual appliance at {nva_ip} (route: \"{matched_route.get('name')}\")")
        if not nva_ip:
            print("  No next-hop IP on this route -- trace stops here.")
            return

        # Check whether this mid-trace hop is itself a Load Balancer frontend,
        # same Floating IP logic used for the final destination, applied here too.
        effective_ips = resolve_effective_destination(nva_ip, port, protocol, lb_rows)
        if len(effective_ips) > 1 or effective_ips[0] != nva_ip:
            print(f"  This hop resolves to {len(effective_ips)} real IP(s) after the Floating IP check above.")

        # Group backend IPs that land in the exact same subnet, since same subnet
        # means same NSG and same route table, the trace result would be identical.
        groups = {}
        unresolved = []
        for eff_ip in effective_ips:
            next_row = find(eff_ip, rows)
            if not next_row:
                unresolved.append(eff_ip)
                continue
            group_key = (next_row["VNetName"], next_row["SubnetName"])
            groups.setdefault(group_key, {"row": next_row, "ips": []})["ips"].append(eff_ip)

        for eff_ip in unresolved:
            print(f"  Could not resolve {eff_ip} to a known subnet -- trace stops here.")

        for group_key, group in groups.items():
            ips_in_group = group["ips"]
            if len(ips_in_group) > 1:
                print(f"\n--- {', '.join(ips_in_group)} share the same subnet/NSG, "
                      f"result below applies to all of them ---")
            elif len(effective_ips) > 1 or len(groups) > 1:
                print(f"\n--- Continuing hop trace via {ips_in_group[0]} ---")
            _trace_hop(group["row"], src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol,
                       rows, lb_rows, asg_membership, hop_num + 1, visited, False, any_deny, max_hops)
        return

    elif next_hop_type in ("VnetPeering", "VNetLocal"):
        if dst_is_external:
            return
        print(f"\nHop {hop_num + 1} -- Destination NSG '{dst_row['NSG_Name']}' (subnet: {dst_row['SubnetName']}):")
        result_text, access = check_nsg_direction(dst_row, "inbound", src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol, asg_membership)
        print(f"  Inbound:  {result_text}")
        if access == "DENY":
            any_deny[0] = True
        return
    elif next_hop_type == "Internet":
        print("  Route table sends this traffic to the internet -- no further Azure hops to trace.")
        return
    else:
        print(f"  Route's next hop type is '{next_hop_type}' -- not something this script follows further.")
        return


def parse_protocol(raw):
    """Validates and normalizes a protocol string. Returns None if invalid."""
    cleaned = raw.strip().lower()
    if cleaned in ("", "tcp"):
        return "Tcp"
    if cleaned == "udp":
        return "Udp"
    if cleaned == "any":
        return "*"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", help="CSV file with columns: source_ip,destination_ip,port,protocol")
    ap.add_argument("--max-hops", type=int, default=10, help="Safety limit on hops to follow (default 10)")
    args = ap.parse_args()

    print("Looking up subnets in Azure...")
    rows = run_resource_graph_query_all_pages(QUERY)
    print(f"    ({len(rows)} subnet rows retrieved)")
    lb_rows = run_resource_graph_query_all_pages(LB_QUERY)
    asg_rows = run_resource_graph_query_all_pages(ASG_QUERY)
    asg_membership = build_asg_membership(asg_rows)

    if args.batch:
        with open(args.batch, newline="") as f:
            reader = csv.DictReader(f)
            checks = list(reader)
        for row in checks:
            protocol = parse_protocol(row["protocol"])
            if protocol is None:
                print(f"\nSkipping row (invalid protocol '{row['protocol']}', must be Tcp/Udp/Any): "
                      f"{row['source_ip']} -> {row['destination_ip']}")
                continue
            print(f"\n{'='*60}")
            print(f"Tracing: {row['source_ip']} -> {row['destination_ip']}  port {row['port']}/{protocol}")
            print(f"{'='*60}")
            trace_path(row["source_ip"].strip(), row["destination_ip"].strip(),
                       row["port"].strip(), protocol, rows, lb_rows, asg_membership, args.max_hops)
    else:
        src_ip = input("\nSource IP: ").strip()
        dst_ip = input("Destination IP: ").strip()
        port = input("Port (e.g. 443): ").strip()
        protocol_raw = input("Protocol - Tcp, Udp, or Any [Tcp]: ")
        protocol = parse_protocol(protocol_raw)
        while protocol is None:
            print(f"'{protocol_raw}' is not valid. Enter Tcp, Udp, or Any.")
            protocol_raw = input("Protocol - Tcp, Udp, or Any [Tcp]: ")
            protocol = parse_protocol(protocol_raw)
        trace_path(src_ip, dst_ip, port, protocol, rows, lb_rows, asg_membership, args.max_hops)


if __name__ == "__main__":
    main()
