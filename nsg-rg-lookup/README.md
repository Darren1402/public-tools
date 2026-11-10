# Azure NSG Resource Group Lookup

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This tool takes a list of NSG names and finds, for each one, the Subscription and Resource Group it lives in, plus any resource locks that apply to it. It uses Azure Resource Graph and the Azure CLI, and makes no changes.

It is a desktop window (GUI): pick one or more NSG IP Removal reports (or any CSV or Excel files with an NSG column), click **Look up**, and get a results table and a ready-to-send list of locked resource groups.

It replaces the manual workflow of searching the Portal for each NSG, opening its resource group, and checking the Locks blade one by one.

## Download
Get the latest version from [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-rg-lookup&expanded=true):
- Desktop window (Windows, macOS, Linux): `nsg_rg_lookup_gui.py`

## Use Case
This is designed for the step after an NSG rule clean-up, when rules need deleting but resource locks are in the way.

It helps:
- Find the Subscription and Resource Group of every NSG across all your reports in one go
- See which locks apply (on the subscription, the resource group, or the NSG itself) and their level
- See which locks actually block the change, so only those get lifted
- Hand a senior engineer a clean, copy-ready list of resource groups to unlock

---

## Features
| Feature | What it does |
|---|---|
| Several reports at once | Pick as many reports as you like (Cmd/Ctrl+click in the file picker). An NSG that appears in several reports is checked once, its rule counts are added up, and the **From report(s)** column shows where it came from |
| Reads the report | Works with the NSG IP Removal report, the tracker Excel, or any CSV / `.xlsx` with an `nsg` or `NetworkSecurityGroupName` column. A file without an NSG column is skipped with a warning |
| Resource group lookup | Finds each NSG's Subscription and Resource Group through Azure Resource Graph, across every subscription you have Reader access to |
| Lock check | Lists every lock that applies to the NSG, and where it sits: Subscription, Resource group, or NSG |
| Blocks change? | Works out whether a lock stops the change. A `CanNotDelete` lock blocks deleting rules, a `ReadOnly` lock blocks any change |
| Copy unlock list | Copies a short summary of each locked resource group, its locks, and the NSGs affected, ready to send |
| Results table | One row per NSG, colour coded: red = lock blocks the change, amber = check, green = no lock |
| Copy a single value | Double-click any cell to copy just that value, or right-click for a menu: copy the cell, the selected rows, the column, all results, or the unlock list |
| Copy and export | **Copy results** pastes into Excel or a ticket. **Export CSV** saves the table |
| Large lists | Looks up NSGs in batches and pages past the Resource Graph 1,000-row limit |
| Remembers the files | The last set of reports is filled in next time |
| Read-only | Only runs `az graph query` and `az lock list`. Nothing in Azure is changed |
| No pip installs | Standard library only, including reading Excel files |

---

## Platform notes

The script works on every platform. It finds the Azure CLI as `az.cmd` on Windows and `az` on macOS and Linux, so there is no separate Windows-only file.

The GUI needs tkinter, which is part of Python's standard library but is packaged differently depending on how Python was installed:

| Setup | tkinter |
|---|---|
| Windows (python.org installer) | Included. Check with `python -m tkinter`, a small test window should open |
| macOS (python.org installer) | Included |
| macOS (Homebrew Python) | Installed separately: `brew install python-tk@3.14`. Match the number to your Python version, check it with `python3 --version` |
| Linux (Debian, Ubuntu) | `sudo apt install python3-tk` |

If tkinter is missing, the script prints a message saying so instead of crashing.

On Windows, the command is `python` rather than `python3`. The Usage section shows both.

---

## Prerequisites
Before running the script, ensure you have:

- Python 3
- tkinter (see Platform notes)
- Azure CLI, logged in (`az login`)
- Reader access to the subscriptions the NSGs live in (Reader includes reading locks)

---

## Where to put this

Keep this in its own standalone folder, not inside a Terraform or infrastructure repo:

```
Documents/
├── Terraform/
│   └── my-project/
├── Scripts/
│   ├── nsg-ip-removal/
│   │   └── nsg_ip_removal_gui.py
│   └── nsg-rg-lookup/
│       ├── nsg_rg_lookup_gui.py
│       └── README.md
├── Reports/
│   ├── CMT-20170006.csv
│   └── CMT-20170009.csv
```

---

## Usage

> **Windows:** type `python` instead of `python3`, for example `python nsg_rg_lookup_gui.py`. On macOS and Linux, use `python3`.

```bash
# macOS / Linux
python3 nsg_rg_lookup_gui.py

# Windows
python nsg_rg_lookup_gui.py
```

1. Run `az login` first if you have not already.
2. Click **Browse** and pick your reports. Hold Cmd (Mac) or Ctrl (Windows) and click to pick several, or click the first and Shift+click the last to pick a range.
3. Click **Look up**. The status line shows progress, then a summary.
4. Check the table. Red rows have a lock that blocks the change.
5. Click **Copy unlock list** and send it to whoever can remove the locks.

Example of what **Copy unlock list** gives you:
```
Resource groups with locks that block the NSG rule changes:

Subscription:   SUB-NP-SECURITY
Resource group: rg-security-prd
  Lock: rg-nodelete (CanNotDelete, on Resource group)
  NSGs: nsg-app01, nsg-app02
  From: CMT-20170006.csv, CMT-20170009.csv
```

| Lock status | Meaning |
|---|---|
| Locked (read-only) | A `ReadOnly` lock applies. Blocks any change to the NSG |
| Locked (delete) | A `CanNotDelete` lock applies. Blocks deleting rules. Removing IPs from a rule is still allowed |
| No lock | Nothing in the way |
| NSG not found | No NSG with that name in any subscription you can read |
| Could not read locks | The lock list for that subscription could not be read, usually a permissions issue. Check it in the Portal |

Keyboard shortcuts:

| Action | macOS | Windows and Linux |
|---|---|---|
| Copy one value | Double-click the cell | Double-click the cell |
| Copy the selected rows | Cmd+C | Ctrl+C |
| Select all rows | Cmd+A | Ctrl+A |
| Right-click menu | Right-click or Ctrl+click | Right-click |

---

## Changelog
See [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-rg-lookup&expanded=true) for version history.

---

## Notes
| Note | Detail |
|---|---|
| Read-only | Every Azure call this tool makes is a read: `az graph query` and `az lock list` |
| Blocks change? uses the report | "Rules deleted" and "Rules updated" come from the report's status column. With a plain NSG list (no status), any lock is treated as blocking |
| Locks are inherited | A lock on the subscription or resource group applies to every NSG inside it. The Locks column says where each lock sits, so the right one gets removed |
| Subscription-level locks | If a lock sits on the subscription, it has to be lifted there, not on the resource group |
| Same NSG name twice | If an NSG name exists in more than one subscription, each one gets its own row, marked "in several subscriptions" |
| Rename reports | NSG IP Removal always saves `ip_removal_report.csv`, so rename each one (for example by VNet) before the next run, or it gets overwritten |
| Put locks back | Locks are there for a reason. Ask for them to be put back once the rules are deleted |

---

## Author
Built as part of a cloud automation project to speed up NSG rule clean-ups on Azure.
