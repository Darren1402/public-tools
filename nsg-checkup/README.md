# Azure NSG Traffic Checker

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This script answers one question fast: if traffic tried to flow from a source IP to a destination IP, would Azure actually let it through.

It replaces the manual workflow of exporting a Resource Graph query to Excel, finding which subnet/NSG an IP belongs to by eye, and manually reading through NSG rules to guess whether traffic is allowed.

## Use Case
This is designed for working through firewall/access request tickets in a large Azure environment, where tracing which NSG governs a given source and destination, and whether it already allows the traffic, would otherwise take several manual steps per ticket.

It helps:
- Find the exact NSG rule (or default rule) that decides ALLOW or DENY
- Catch cases where a route table forces traffic through a firewall
- Catch cases where the destination is actually a Load Balancer frontend, not the real server
- Check many tickets at once from a CSV

---

## Features
| Feature | What it does |
|---|---|
| IP resolution | Finds the VNet, Subnet, and NSG for both IPs via Azure Resource Graph |
| Rule evaluation | Checks custom and default NSG rules in correct priority order, returns ALLOW/DENY and the exact matching rule |
| Peering check | Verifies VNets are genuinely peered before trusting a VirtualNetwork-tag rule, instead of guessing |
| Internet-aware | Recognizes a public destination and checks outbound only |
| Firewall/NVA hop | Detects a route table forcing traffic through a firewall, and checks that NSG too |
| Load Balancer aware | Detects an LB frontend IP, reads its Floating IP setting, and auto-redirects to the real backend IP if it's off |
| Scales | Pages through Resource Graph results, won't miss data past 1,000 rows |
| Batch mode | Checks a whole list of source/destination pairs from one CSV |
| Reporting | Saves a timestamped report to the script's own folder |

---

## Prerequisites
Before running the script, ensure you have:

- Python 3, no pip installs needed
- Azure CLI, logged in (`az login`)
- Reader access to the target subscription(s), no write permissions needed

---

## Where to put this

Keep this in its own standalone folder, not inside a Terraform or infrastructure repo:

```
Documents/
├── Terraform/
│   └── my-project/
├── Scripts/
│   └── nsg-check/
│       ├── nsg_check_v4.py
│       ├── mychecks.csv
│       └── README.md
```

---

## Usage

### Single check
```bash
python3 nsg_check_v4.py
```
You'll be prompted for Source IP, Destination IP, Port, and Protocol.

### Batch check
```bash
python3 nsg_check_v4.py --batch mychecks.csv
```

`mychecks.csv` is included in this repo as an empty template, header row only. Fill in your own rows locally before running.

**Before you edit it**, run this once so your local edits never get committed back:
```bash
git update-index --skip-worktree mychecks.csv
```
This tells git to ignore future changes to this one file on your machine, while keeping the empty template itself checked into the repo for anyone who clones it.

Columns: `source_ip,destination_ip,port,protocol`
```csv
source_ip,destination_ip,port,protocol
192.168.150.133,8.8.8.8,443,Tcp
192.168.150.134,8.8.8.8,443,Tcp
```

### Skip saving a report
```bash
python3 nsg_check_v4.py --no-report
```

---

## Notes
| Limitation | Detail |
|---|---|
| Firewall policy | Cannot see inside a firewall's own policy, such as Panorama. Only checks the NSG in front of a firewall/NVA hop |
| Application Security Groups | Not supported. A rule using an ASG instead of a plain IP is not evaluated for membership |
| Hop limit | Checks at most 3 hops total: source NSG, one firewall/NVA hop if detected, destination NSG. If real traffic crosses more hops than that, anything beyond the third is not checked at all |
| Multiple firewall hops | Only follows one firewall/NVA hop. A chain of two or more firewalls in sequence won't be traced past the first |
| Load Balancer NAT rules | Only checks standard Load Balancing Rules for Floating IP, not Inbound NAT Rules |
| Writes | None. Every operation is read-only, nothing is created, changed, or deleted |
| Real-world delivery | ALLOW on every NSG checked does not guarantee the traffic actually works end-to-end |

---

## Workflow
1. Run a check with the ticket's source IP, destination IP, port, protocol
2. Read the ALLOW/DENY result, warnings about a Load Balancer or firewall hop print automatically if relevant
3. If DENY, add the corresponding row to that VNet's rules CSV in the Terraform project, commit, and apply
4. Re-run the same check to confirm it now shows ALLOW

---

## Author
Built as part of a cloud automation project to speed up NSG access reviews on Azure.
