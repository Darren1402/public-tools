# Azure NSG Multi-Hop Traffic Tracer

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This tool traces the full network path from a source IP to a destination IP, checking every NSG along the way, and reports ALLOW or DENY at each hop.

It follows route tables and VNet peering hop by hop, not limited to a fixed number of hops, so it can trace a real path through a firewall, a transit VNet, or any chain of route-table forwarding, and tells you exactly which NSG needs a rule if something is blocked.

It comes in two forms: a desktop window (GUI) where you enter several checks at once and get one tab per check, and a command-line script for terminals and Cloud Shell.

## Download
Get the latest version from [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-multihop&expanded=true):
- Desktop window (Windows, macOS, Linux): `nsg_check_multihop_gui.py`
- Command line (Windows, macOS, Linux, Cloud Shell): `nsg_check_multihop_universal.py`

## Use Case
This is designed for working through firewall or access request tickets where a request's real path crosses multiple NSGs, for example through a firewall and then one or more transit VNets before reaching the final destination.

It helps:
- Trace a request through a firewall and one or more transit VNets in a single run
- Find the exact NSG and rule that is blocking traffic, wherever in the path it happens
- Automatically check the real backend IP when the destination is a Load Balancer with Floating IP off
- Check a ticket with several flows or several ports at once, and copy the result into the ticket

---

## Features
| Feature | What it does | GUI | CLI |
|---|---|---|---|
| IP resolution | Finds the Subscription, Resource Group, VNet, Subnet, and NSG for any IP via Azure Resource Graph | Yes | Yes |
| Full path tracing | Follows route tables and peering hop by hop until the destination is reached, not capped at a fixed number of hops | Yes | Yes |
| Rule evaluation | Checks custom and default NSG rules at every hop in correct priority order | Yes | Yes |
| Peering aware | Verifies VNets are genuinely peered before assuming traffic can reach directly, does not guess | Yes | Yes |
| Loop protection | Detects a routing loop and stops instead of tracing forever | Yes | Yes |
| Max hop limit | Configurable safety limit, stops and warns if the destination is not reached within it | Yes | Yes |
| Load Balancer aware | Detects a Load Balancer frontend IP anywhere in the path, reads its Floating IP setting, and automatically traces to the real backend IP(s) if Floating IP is off | Yes | Yes |
| ASG aware | Checks Application Security Group membership on both source and destination, and shows when a rule matched through an ASG | Yes | Yes |
| Grouped backends | Backend IPs in the same subnet and NSG are checked and reported together | Yes | Yes |
| Protocol validation | Rejects an invalid protocol instead of silently matching nothing | Yes | Yes |
| Batch mode | Checks multiple source/destination pairs from a CSV file in one run | - | Yes |
| Multiple checks | Add as many checks as needed with **+ Add check**, each with its own source, destination, ports and protocol | Yes | - |
| Port ranges and lists | Ports accept a single port, a range (`8000-8100`), a list (`80,443`), a mix, or `*` for all ports. A mixed result is split per port, for example "22 allowed, 443 blocked" | Yes | - |
| One tab per check | An **All checks** overview tab, plus one tab per check so you do not scroll through long output | Yes | - |
| Hop cards | Every hop is shown as a card with a green ALLOW or red DENY badge, the rule that decided it, and the rule priority | Yes | - |
| Path splits | If the traffic splits (different ports take different Load Balancer paths, or several backends), each path is shown separately with its own verdict | Yes | - |
| Selectable text | Any text in the results can be highlighted and copied | Yes | - |
| Copy and export | **Copy summary** puts the full result on the clipboard for a ticket, **Export CSV** saves one row per check. Right-click a tab to copy just that check | Yes | - |
| Fix suggestion | For every block, names the NSG, direction and the Allow rule to add | Yes | Yes |
| Read-only | No changes made to any Azure resource | Yes | Yes |
| No pip installs | Standard library only | Yes | Yes |

### What the results mean
| Result | Meaning |
|---|---|
| ALLOW | Every hop allows every requested port |
| DENY | Every requested port is blocked at some hop |
| PARTIAL | Some ports get through and some are blocked. The detail says which are which. This also happens when different ports take different paths and only some are blocked |

---

## Platform notes

Each script works on every platform: both auto-detect the OS at runtime and adjust how they call `az` accordingly, since Windows resolves the Azure CLI as `az.cmd` rather than `az`. There is no separate Windows-only or Mac/Linux-only file.

The GUI needs tkinter, which is part of Python's standard library but is packaged differently depending on how Python was installed:

| Setup | tkinter |
|---|---|
| Windows (python.org installer) | Included. Check with `python -m tkinter`, a small test window should open |
| macOS (python.org installer) | Included |
| macOS (Homebrew Python) | Installed separately: `brew install python-tk@3.14`. Match the number to your Python version, check it with `python3 --version` |
| Linux (Debian, Ubuntu) | `sudo apt install python3-tk` |
| Azure Cloud Shell | Not available, there is no display. Use the command-line script instead |

If tkinter is missing, the GUI prints a message saying so instead of crashing.

On Windows, the command is `python` rather than `python3`. The Usage section shows both.

---

## Prerequisites
Before running the script, ensure you have:

- Python 3
- tkinter (GUI only, see Platform notes)
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
│   └── nsg-multihop-checker/
│       ├── nsg_check_multihop_gui.py
│       ├── nsg_check_multihop_universal.py
│       ├── mychecks.csv
│       └── README.md
```

`mychecks.csv` is only needed for command-line batch mode.

---

## Usage

> **Windows:** type `python` instead of `python3` in every command below, for example `python nsg_check_multihop_gui.py`. On macOS and Linux, use `python3`.

### Desktop window (GUI)
```bash
# macOS / Linux
python3 nsg_check_multihop_gui.py

# Windows
python nsg_check_multihop_gui.py
```

1. The window opens and loads the network data from Azure. Wait until the status in the bottom right says it is ready.
2. Fill in the first row: Source IP, Destination IP, Port(s) and Protocol. Click **+ Add check** for more rows. Use the **x** on a row to remove it.
3. Ports can be a single port (`443`), a range (`8000-8100`), a list (`80,443`), a mix (`22,8000-8100`), or `*` for all ports.
4. Click **Trace**. The **All checks** tab shows one line per check with its result. Click a line to see its full detail under the table.
5. Open a check's own tab to see every hop, which rule allowed or blocked it, and what rule to add if something is blocked.
6. Click **Copy summary** to paste the whole result into a ticket, or **Export CSV** to save it. Right-click a tab and choose **Copy this check** to copy just one.
7. After changing NSGs or routes in Azure, click **Trace** again. Each trace re-reads the live rules. Click **Refresh data** only if you added or moved subnets or VNets.

Max hops (default 10) is next to the Trace button. Raise it if a real path is expected to be longer.

Reading a result:

- A green ALLOW badge on a hop means the NSG at that hop lets the traffic through. A red DENY badge shows the rule that blocked it.
- When the destination is a Load Balancer frontend with Floating IP off, the card says so and shows the backend IPs that were checked instead.
- A yellow note means the tool could not be sure of something, for example it could not find the matching Load Balancing rule. Verify those manually.
- Double-check PARTIAL results: the detail names the ports that are blocked and the hop that blocks them.

### Command line: single check
```bash
# macOS / Linux / Cloud Shell
python3 nsg_check_multihop_universal.py

# Windows
python nsg_check_multihop_universal.py
```

You will be prompted for Source IP, Destination IP, Port, and Protocol.

Set a different hop limit if a real path is expected to be longer than the default of 10:

```bash
# macOS / Linux / Cloud Shell
python3 nsg_check_multihop_universal.py --max-hops 15

# Windows
python nsg_check_multihop_universal.py --max-hops 15
```

### Command line: batch
Check multiple pairs from a CSV file:

```bash
# macOS / Linux / Cloud Shell
python3 nsg_check_multihop_universal.py --batch mychecks.csv

# Windows
python nsg_check_multihop_universal.py --batch mychecks.csv
```

The CSV needs this header row, with protocol as `Tcp`, `Udp`, or `Any`:

```
source_ip,destination_ip,port,protocol
10.1.1.4,10.2.5.10,443,Tcp
10.4.2.8,10.2.5.10,53,Udp
```

Rows with an invalid protocol are skipped with a message. Both flags can be combined, e.g. `--batch mychecks.csv --max-hops 15`.

---

## Notes
| Limitation | Detail |
|---|---|
| Firewall policy | Cannot see inside a firewall's own policy, such as Panorama. Only checks the NSG at each hop the route tables point to |
| Route-table dependent hops | A hop only exists if a real Azure route table points to it. Anything a firewall does internally, such as NAT or forwarding out a different interface, with no matching Azure route is invisible. Direct VNet peering does not need a route table and is always detected |
| Application Security Groups | Supported for both source and destination. Membership is checked against each NIC's current ASG tags, if a NIC's ASG membership just changed, there may be a brief delay before Azure's data reflects it |
| Load Balancer NAT rules | Only checks standard Load Balancing Rules for Floating IP, not Inbound NAT Rules. The tool warns when it cannot find a matching rule |
| Backend pool types | Supports both NIC-based and IP-based (address-only) backend pools when resolving the real backend IP(s) behind a Load Balancer |
| Port ranges | GUI only. The command line checks one port per run |
| Saving results | The command line prints to screen only. The GUI can copy a summary or export a CSV, nothing is saved unless you choose to |
| Writes | None. Every operation is read-only, nothing is created, changed, or deleted |
| Real-world delivery | ALLOW at every hop does not guarantee the traffic actually works end to end |

---

## Workflow
1. Run a check with the ticket's source IP, destination IP, ports, protocol (add several rows in the GUI, or use a CSV on the command line)
2. Read the hop-by-hop result, a Load Balancer or Floating IP redirect shows automatically if relevant
3. If any hop shows DENY, add the corresponding row to that VNet's rules CSV in the Terraform project, commit, and apply
4. Trace again to confirm it now shows ALLOW at every hop. The GUI re-reads the live rules each time, so there is no need to restart it

---

## Changelog
See [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-multihop&expanded=true) for version history.

---

## Author
Built as part of a cloud automation project to trace multi-hop NSG access paths on Azure.
