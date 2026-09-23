# Azure NSG Multi-Hop Traffic Tracer with Python

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This script traces the full network path from a source IP to a destination IP, checking every NSG along the way, and reports ALLOW or DENY at each hop.

It follows route tables and VNet peering hop by hop, not limited to a fixed number of hops, so it can trace a real path through a firewall, a transit VNet, or any chain of route-table forwarding, and tells you exactly which NSG needs a rule if something is blocked.

## Use Case
This is designed for working through firewall or access request tickets where a request's real path crosses multiple NSGs, for example through a firewall and then one or more transit VNets before reaching the final destination.

It helps:
- Trace a request through a firewall and one or more transit VNets in a single run
- Find the exact NSG and rule that is blocking traffic, wherever in the path it happens
- Automatically check the real backend IP when the destination is a Load Balancer with Floating IP off

---

## Features
| Feature | What it does |
|---|---|
| IP resolution | Finds the Subscription, Resource Group, VNet, Subnet, and NSG for any IP via Azure Resource Graph |
| Full path tracing | Follows route tables and peering hop by hop until the destination is reached, not capped at a fixed number of hops |
| Rule evaluation | Checks custom and default NSG rules at every hop in correct priority order |
| Peering aware | Verifies VNets are genuinely peered before assuming traffic can reach directly, does not guess |
| Loop protection | Detects a routing loop and stops instead of tracing forever |
| Max hop limit | Configurable safety limit, stops and warns if the destination is not reached within it |
| Load Balancer aware | Detects an LB frontend IP as the destination, reads its Floating IP setting, and automatically traces to the real backend IP if Floating IP is off, including every backend if there is more than one |
| Protocol validation | Rejects an invalid protocol instead of silently matching nothing |

---

## Platform notes

| Platform | Script to use |
|---|---|
| macOS / Linux | `nsg_check_multihop.py` |
| Windows | `nsg_check_multihop_win.py` |

Windows needed a small change to how the script calls `az`, since Windows resolves the Azure CLI as `az.cmd` rather than `az`. `nsg_check_multihop_win.py` is a separate copy with that adjustment, it is Windows-specific and should not be run on macOS or Linux.

Both scripts otherwise contain identical logic, including the same pagination handling, this is not a platform-specific fix, both files check for the same set of possible field names Azure CLI can return for continuing to the next page of results.

---
Before running the script, ensure you have:

- Python 3, no pip installs needed
- Azure CLI, logged in (`az login`)
- Reader access to the target subscription(s), no write permissions needed

---

## Usage

```bash
python3 nsg_check_multihop.py
```

You will be prompted for Source IP, Destination IP, Port, and Protocol.

Set a different hop limit if a real path is expected to be longer than the default of 10:

```bash
python3 nsg_check_multihop.py --max-hops 15
```

---

## Notes
| Limitation | Detail |
|---|---|
| Firewall policy | Cannot see inside a firewall's own policy, such as Panorama. Only checks the NSG at each hop the route tables point to |
| Route-table dependent hops | A hop only exists if a real Azure route table points to it. Anything a firewall does internally, such as NAT or forwarding out a different interface, with no matching Azure route is invisible. Direct VNet peering does not need a route table and is always detected |
| Application Security Groups | Not supported. A rule using an ASG instead of a plain IP is not evaluated for membership |
| Load Balancer NAT rules | Only checks standard Load Balancing Rules for Floating IP, not Inbound NAT Rules |
| No batch mode | This version checks one source/destination pair per run |
| No report file | This version does not save a report to disk |
| Writes | None. Every operation is read-only, nothing is created, changed, or deleted |
| Real-world delivery | ALLOW at every hop does not guarantee the traffic actually works end to end |

---

## Workflow
1. Run a check with the ticket's source IP, destination IP, port, protocol
2. Read the hop-by-hop result, a Load Balancer or Floating IP redirect prints automatically if relevant
3. If any hop shows DENY, add the corresponding row to that VNet's rules CSV in the Terraform project, commit, and apply
4. Re-run the same check to confirm it now shows ALLOW at every hop

---

## Author
Built as part of a cloud automation project to trace multi-hop NSG access paths on Azure.
