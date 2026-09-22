#!/usr/bin/env python3
"""
nsg_check_final.py -- run locally (or in Cloud Shell). Requires: az login, python3.
No pip installs needed -- uses only Python's standard library + the az CLI.

USAGE
  Single check (interactive, same as before):
      python3 nsg_check_final.py

  Batch check (reads multiple src/dst/port/protocol rows from a CSV):
      python3 nsg_check_final.py --batch mychecks.csv

      CSV columns required: source_ip,destination_ip,port,protocol

WHAT IT DOES
  For each source IP, destination IP, port, and protocol you give it:
    - finds which VNet / Subnet / NSG the source and destination sit in
    - checks whether the traffic is currently allowed or blocked, checking
      BOTH the outbound side (leaving the source) and the inbound side
      (arriving at the destination)
    - if a rule relies on the "VirtualNetwork" service tag, it actually
      checks whether the two VNets are the same or genuinely peered
      (via `az network vnet peering list`) instead of guessing
    - if the destination isn't one of your known Azure subnets, it's
      treated as a public/internet address, and only the outbound side
      is checked
    - if the source subnet has a route table forcing traffic through a
      firewall/NVA (nextHopType = VirtualAppliance), it follows that hop
      and checks the NSG there too. LIMIT: it cannot see the firewall's
      own policy (e.g. Panorama) -- only the NSG in front of it.
    - if the destination IP is a Load Balancer's frontend IP, it looks up
      that LB rule's Floating IP (DSR) setting automatically:
        * ON  -> the frontend IP is genuinely correct; checks it as given
        * OFF -> the real destination is the backend VM(s); automatically
                 finds their private IP(s) and checks those instead
      LIMIT: only covers standard Load Balancing rules, not Inbound NAT
      rules, and if a rule can't be matched to your exact port/protocol,
      it falls back to just warning you (as before).

It only reads information from Azure (Reader access is enough). It does
not change any NSG, file, or run terraform. You add the CSV row yourself.
"""

import subprocess, json, ipaddress, sys, argparse, csv, io, datetime, os

def az(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print("Something went wrong running:", " ".join(args))
        print(result.stderr)
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None

subprocess.run(["az", "extension", "add", "--name", "resource-graph", "-y"], capture_output=True)

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
    'PrivateIP = tostring(fic.properties.privateIPAddress), '
    'FrontendConfigName = tostring(fic.name)'
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
        skip_token = result.get("skipToken") or result.get("$skipToken")
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

def get_peered_vnet_names(resource_group, vnet_name):
    key = (resource_group, vnet_name)
    if key in _peering_cache:
        return _peering_cache[key]
    data = az(["az", "network", "vnet", "peering", "list",
               "--resource-group", resource_group, "--vnet-name", vnet_name, "-o", "json"]) or []
    peered = set()
    for p in data:
        if p.get("peeringState") == "Connected":
            remote_id = p.get("remoteVirtualNetwork", {}).get("id", "")
            peered.add(remote_id.split("/")[-1])
    _peering_cache[key] = peered
    return peered


def ip_matches(rule, ip, prefix_field, prefixes_field, ip_owner_row, self_vnet_name, peered_names):
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
            if owner_vnet == self_vnet_name or owner_vnet in peered_names:
                return True
            continue
        try:
            if target in ipaddress.ip_network(v, strict=False):
                return True
        except ValueError:
            continue
    return False


def check_nsg(nsg_row, direction, src_ip, src_row, dst_ip, dst_row, dst_is_external, port, protocol):
    if nsg_row["NSG_Name"] == "NO NSG ATTACHED":
        return "No NSG attached here -- traffic isn't restricted at this point."
    self_owner_row = src_row if direction == "outbound" else dst_row
    self_vnet_name = self_owner_row["VNetName"]
    peered_names = get_peered_vnet_names(self_owner_row["ResourceGroup"], self_vnet_name)
    nsg = az(["az", "network", "nsg", "show", "--ids", nsg_row["NSG_Id"], "-o", "json"])
    if nsg is None:
        return "Could not read this NSG's rules (check permissions)."
    rules = (nsg.get("securityRules") or []) + (nsg.get("defaultSecurityRules") or [])
    rules = [r for r in rules if r["direction"].lower() == direction]
    rules.sort(key=lambda r: r["priority"])
    for r in rules:
        if r["protocol"] not in ("*", protocol):
            continue
        if not port_matches(r, port):
            continue
        src_match = ip_matches(r, src_ip, "sourceAddressPrefix", "sourceAddressPrefixes",
                                src_row, self_vnet_name, peered_names)
        dst_match = ip_matches(r, dst_ip, "destinationAddressPrefix", "destinationAddressPrefixes",
                                (None if dst_is_external else dst_row), self_vnet_name, peered_names)
        if not src_match or not dst_match:
            continue
        return f'{r["access"].upper()} -- matched rule "{r["name"]}" (priority {r["priority"]})'
    return "DENY -- no matching rule found (falls through to the default deny rule)"


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
    rg = lb["ResourceGroup"]
    lb_name = lb["LBName"]
    rules = az(["az", "network", "lb", "rule", "list", "--lb-name", lb_name,
                "--resource-group", rg, "-o", "json"])
    if not rules:
        return "unknown", None

    matching = None
    for r in rules:
        if r.get("protocol", "").lower() not in (protocol.lower(), "all"):
            continue
        if str(r.get("frontendPort")) == str(port):
            matching = r
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


def run_single_check(src_ip, dst_ip, port, protocol, rows, lb_rows):
    print(f"\n{'='*60}")
    print(f"Checking: {src_ip} -> {dst_ip}  port {port}/{protocol}")
    print(f"{'='*60}")

    matching_lbs = [r for r in lb_rows if r.get("PrivateIP") == dst_ip]
    effective_dst_ips = [dst_ip]
    if matching_lbs:
        lb = matching_lbs[0]
        print(f"\n[!] {dst_ip} is the FRONTEND IP of Load Balancer '{lb['LBName']}'.")
        status, backend_ips = check_floating_ip(lb, dst_ip, port, protocol)
        if status == "floating_on":
            print("    Floating IP (DSR) is ON for the matching LB rule -- this frontend IP")
            print("    is genuinely what the NSG evaluates. Checking it as given.")
        elif status == "floating_off":
            print("    Floating IP (DSR) is OFF for the matching LB rule -- the NSG actually")
            print(f"    evaluates the backend VM's own IP, not the frontend. Found backend IP(s): "
                  f"{', '.join(backend_ips)}")
            print("    Automatically re-checking against the backend IP(s) instead.")
            effective_dst_ips = backend_ips
        else:
            print("    Could not determine the Floating IP setting for the matching rule (or no")
            print("    rule matched this port/protocol -- e.g. it may be an Inbound NAT rule,")
            print("    which this script doesn't check yet). Verify manually if unsure, or")
            print("    re-run with the backend VM's own private IP.")

    for effective_dst in effective_dst_ips:
        if len(effective_dst_ips) > 1 or effective_dst != dst_ip:
            print(f"\n--- Checking against {effective_dst} ---")

        src = find(src_ip, rows)
        dst = find(effective_dst, rows)
        if not src:
            print(f"Could not find source IP {src_ip} in any known subnet.")
            continue
        dst_is_external = dst is None
        if dst_is_external:
            print(f"\nDestination IP {effective_dst} isn't in any known Azure subnet -- "
                  f"treating it as a public/internet address.")

        print(f"\nSource      -> VNet: {src['VNetName']}   Subnet: {src['SubnetName']}   NSG: {src['NSG_Name']}")
        if not dst_is_external:
            print(f"Destination -> VNet: {dst['VNetName']}   Subnet: {dst['SubnetName']}   NSG: {dst['NSG_Name']}")

        nva_hop = None
        route_table = get_route_table(src.get("RouteTableId"))
        matched_route = find_matching_route(route_table, effective_dst)
        if matched_route and matched_route.get("nextHopType") == "VirtualAppliance":
            nva_ip = matched_route.get("nextHopIpAddress")
            print(f"\n[i] Route table on the source subnet sends this traffic through a "
                  f"firewall/NVA at {nva_ip} (route: \"{matched_route.get('name')}\").")
            nva_row = find(nva_ip, rows) if nva_ip else None
            if nva_row:
                print(f"    That IP sits in VNet: {nva_row['VNetName']}   Subnet: {nva_row['SubnetName']}   "
                      f"NSG: {nva_row['NSG_Name']}")
                nva_hop = nva_row
            else:
                print("    Could not resolve that IP to a known subnet -- can't check an NSG for this hop.")
            print("    IMPORTANT: this only checks the NSG in front of the firewall. It cannot see the")
            print("    firewall's own policy (e.g. Panorama) -- that must be verified separately.")

        print(f"\nOutbound check on source NSG '{src['NSG_Name']}':")
        print("  " + check_nsg(src, "outbound", src_ip, src, effective_dst, dst, dst_is_external, port, protocol))

        if nva_hop:
            print(f"\nInbound check on firewall/NVA-hop NSG '{nva_hop['NSG_Name']}' (NOT the final destination):")
            print("  " + check_nsg(nva_hop, "inbound", src_ip, src, effective_dst, dst, dst_is_external, port, protocol))
            print("  ^ NSG-only result. The firewall's own policy still governs whether this actually passes.")

        if not dst_is_external:
            print(f"\nInbound check on destination NSG '{dst['NSG_Name']}':")
            print("  " + check_nsg(dst, "inbound", src_ip, src, effective_dst, dst, dst_is_external, port, protocol))
        else:
            print("\nInbound check skipped -- destination is external, no Azure NSG to check.")

        if nva_hop:
            print("Reminder: even if every NSG above says ALLOW, this traffic still passes through a "
                  "firewall/NVA whose own policy this script cannot verify -- check that separately.")


class Tee:
    """Writes everything to both the real terminal and an in-memory buffer,
    so every run's output can be saved to a report file afterward."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
    def flush(self):
        for s in self.streams:
            s.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", help="CSV file with columns: source_ip,destination_ip,port,protocol")
    ap.add_argument("--no-report", action="store_true", help="Skip saving a report file for this run")
    args = ap.parse_args()

    buffer = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = Tee(real_stdout, buffer)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    print(f"NSG Traffic Checker -- report generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    print("Looking up subnets in Azure...")
    rows = run_resource_graph_query_all_pages(QUERY)
    print(f"    ({len(rows)} subnet rows retrieved)")
    lb_rows = run_resource_graph_query_all_pages(LB_QUERY)

    if args.batch:
        with open(args.batch, newline="") as f:
            reader = csv.DictReader(f)
            checks = list(reader)
        print(f"\nRunning {len(checks)} checks from {args.batch}...")
        for row in checks:
            protocol = row["protocol"].strip()
            protocol = "*" if protocol.lower() == "any" else protocol.capitalize()
            run_single_check(row["source_ip"].strip(), row["destination_ip"].strip(),
                              row["port"].strip(), protocol, rows, lb_rows)
    else:
        print("\n=== NSG Traffic Checker ===")
        src_ip = input("Source IP: ").strip()
        dst_ip = input("Destination IP: ").strip()
        port = input("Port (e.g. 443): ").strip()
        protocol_raw = input("Protocol - Tcp, Udp, or Any [Tcp]: ").strip() or "Tcp"
        protocol = "*" if protocol_raw.lower() == "any" else protocol_raw.capitalize()
        print(f"Source IP: {src_ip}")
        print(f"Destination IP: {dst_ip}")
        print(f"Port: {port}")
        print(f"Protocol: {protocol_raw}")
        run_single_check(src_ip, dst_ip, port, protocol, rows, lb_rows)

    print(
        "\nIf any line above says DENY, that's the NSG (and direction) you need "
        "to add a new Allow rule for. Add the row to that VNet's *_nsg_rules.csv as usual."
    )

    sys.stdout = real_stdout
    if not args.no_report:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        report_name = os.path.join(script_dir, f"nsg_check_report_{timestamp}.txt")
        with open(report_name, "w") as f:
            f.write(buffer.getvalue())
        print(f"\n[Report saved to {report_name}]")


if __name__ == "__main__":
    main()
