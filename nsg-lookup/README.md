# Azure NSG Subnet Lookup

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This script looks up any IP address in an Azure environment and returns which Subscription, Resource Group, VNet, Subnet, and NSG it belongs to using the Azure Resource Graph.

It replaces the manual workflow of exporting a Resource Graph query to Excel and searching for the right subnet by eye.

## Download
Get the latest version from [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-lookup&expanded=true):
- Mac/Linux/Cloud Shell: `nsg_lookup.py`
- Windows: `nsg_lookup_win.py`

## Use Case
This is designed for quickly identifying network context when working on tickets that reference an IP address, without opening the Portal or maintaining a spreadsheet.

It helps:
- Find the subnet/NSG for any IP in seconds
- Cover large environments across multiple subscriptions without missing data
- Check several IPs at once from a CSV

---

## Features
| Feature | What it does |
|---|---|
| Single lookup | Looks up one IP interactively |
| Batch lookup | Looks up many IPs at once from a CSV |
| Pagination | Automatically pages through Resource Graph results past the 1,000-row limit |
| Read-only | No changes made to any Azure resource |
| No dependencies | No pip installs required, standard library only |

---

## Platform notes

| Platform | Script to use |
|---|---|
| macOS / Linux | `nsg_lookup.py` |
| Windows | `nsg_lookup_win.py` |

Windows needed a small change to how the script calls `az`, since Windows resolves the Azure CLI as `az.cmd` rather than `az`. `nsg_lookup_win.py` is a separate copy with that adjustment, it is Windows-specific and should not be run on macOS or Linux.

Both scripts otherwise contain identical logic, including the same pagination handling, this is not a platform-specific fix, both files check for the same set of possible field names Azure CLI can return for continuing to the next page of results.

---

## Prerequisites
Before running the script, ensure you have:

- Python 3
- Azure CLI, logged in (`az login`)
- Reader access to the target subscription(s)

---

## Where to put this

Keep this in its own standalone folder, not inside a Terraform or infrastructure repo:

```
Documents/
├── Terraform/
│   └── my-project/
├── Scripts/
│   └── nsg-lookup/
│       ├── nsg_lookup.py
│       ├── ips.csv
│       └── README.md
```

---

## Usage

### Single lookup
```bash
python3 nsg_lookup.py
```

### Batch lookup
```bash
python3 nsg_lookup.py --batch ips.csv
```

CSV needs one column, `ip`:
```csv
ip
192.168.150.133
10.10.1.4
```

---

## Changelog
See [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-lookup&expanded=true) for version history.

---

## Notes
| Note | Detail |
|---|---|
| Read-only | Every Azure call this script makes is a read-only GET (`az graph query`) |
| Not found | If an IP isn't found, it isn't in any subnet this account has Reader access to |
| Need ALLOW/DENY? | Use `nsg_check` instead, this script only resolves subnet/NSG, it doesn't evaluate rules |

---

## Author
Built as part of a cloud automation project to speed up NSG and network troubleshooting on Azure.
