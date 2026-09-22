#!/usr/bin/env python3
"""
nsg_lookup.py -- run locally or in Cloud Shell. Requires: az login, python3.
No pip installs needed -- standard library only.

Pure IP lookup: given an IP, tells you which Subscription, Resource Group,
VNet, Subnet, and NSG it belongs to. No rule evaluation, no ALLOW/DENY, no
firewall/LB detection -- just the fast "where does this IP live" answer.

Same underlying query as the original Resource Graph Explorer KQL, run
through the az CLI with automatic pagination so it never silently misses
data past Azure's 1,000-row-per-query limit.

USAGE
  Single lookup (interactive):
      python3 nsg_lookup.py

  Batch lookup (one IP per line from a CSV, column: ip):
      python3 nsg_lookup.py --batch ips.csv
"""

import subprocess, json, ipaddress, sys, argparse, csv

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
    '| join kind=leftouter ( '
    "    ResourceContainers "
    "    | where type == 'microsoft.resources/subscriptions' "
    "    | project subscriptionId, SubscriptionName = name "
    ') on subscriptionId '
    '| project '
    '    SubscriptionName = iff(isnotempty(SubscriptionName), SubscriptionName, subscriptionId), '
    '    ResourceGroup = resourceGroup, '
    '    VNetName = name, '
    '    SubnetName = tostring(subnet.name), '
    '    AddressPrefix = tostring(subnet.properties.addressPrefix), '
    '    NSG_Name = iff(isnotempty(nsgName), tostring(nsgName), "NO NSG ATTACHED")'
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


def print_result(ip, rows):
    row = find(ip, rows)
    print(f"\n{ip}")
    if not row:
        print("  Not found in any known subnet.")
        return
    print(f"  Subscription : {row['SubscriptionName']}")
    print(f"  Resource Grp : {row['ResourceGroup']}")
    print(f"  VNet         : {row['VNetName']}")
    print(f"  Subnet       : {row['SubnetName']} ({row['AddressPrefix']})")
    print(f"  NSG          : {row['NSG_Name']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", help="CSV file with one column: ip")
    args = ap.parse_args()

    print("Looking up subnets in Azure...")
    rows = run_resource_graph_query_all_pages(QUERY)
    print(f"    ({len(rows)} subnet rows retrieved)")

    if args.batch:
        with open(args.batch, newline="") as f:
            reader = csv.DictReader(f)
            ips = [row["ip"].strip() for row in reader]
        print(f"\nLooking up {len(ips)} IPs from {args.batch}...")
        for ip in ips:
            print_result(ip, rows)
    else:
        ip = input("\nIP to look up: ").strip()
        print_result(ip, rows)


if __name__ == "__main__":
    main()
