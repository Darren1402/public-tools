# Azure NSG Subnet Lookup

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Azure CLI](https://img.shields.io/badge/Azure%20CLI-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This tool looks up any IP address in an Azure environment and returns which Subscription, Resource Group, VNet, Subnet, and NSG it belongs to using the Azure Resource Graph.

It comes in two forms: a desktop window (GUI) where you type or paste IPs and get a results table, and a command-line script for terminals and Cloud Shell.

It replaces the manual workflow of exporting a Resource Graph query to Excel and searching for the right subnet by eye.

## Download
Get the latest version from [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-lookup&expanded=true):
- Desktop window (Windows, macOS, Linux): `nsg_lookup_gui.py`
- Command line (Windows, macOS, Linux, Cloud Shell): `nsg_lookup_universal.py`

## Use Case
This is designed for quickly identifying network context when working on tickets that reference an IP address, without opening the Portal or maintaining a spreadsheet.

It helps:
- Find the subnet/NSG for any IP in seconds
- Cover large environments across multiple subscriptions without missing data
- Check several IPs at once, by pasting a list into the window or from a CSV
- Filter the results and copy them straight into a ticket

---

## Features
| Feature | What it does | GUI | CLI |
|---|---|---|---|
| Single lookup | Looks up one IP | Yes | Yes |
| Multiple IPs | Add as many IPs as needed with **+ Add IP**, or paste a whole list (one per line, or separated by commas or spaces) and it splits into rows | Yes | - |
| Batch from CSV | Looks up many IPs at once from a CSV | - | Yes |
| Numbered rows | Input boxes and results are numbered, with a live count of how many IPs you are checking | Yes | - |
| Results table | One row per IP with Subscription, Resource Group, VNet, Subnet, and NSG. Not found, invalid IPs, and subnets with no NSG are colour coded | Yes | - |
| Filter | Type in the filter box and only matching rows stay visible, for example `untrust` or `vnet-prod subnet-app`. Several words narrow it down | Yes | - |
| Auto-fit columns | Columns size to their content and shrink to fit the window. Long values end in `...` and show in full when you hover over them or drag the column divider wider | Yes | - |
| Copy and export | Copy the results to the clipboard (pastes into Excel or a ticket) or export them to a CSV | Yes | - |
| Refresh | Re-pulls the subnet data from Azure without restarting | Yes | - |
| Pagination | Automatically pages through Resource Graph results past the 1,000-row limit | Yes | Yes |
| Read-only | No changes made to any Azure resource | Yes | Yes |
| No pip installs | Standard library only | Yes | Yes |

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
│       ├── nsg_lookup_gui.py
│       ├── nsg_lookup_universal.py
│       ├── ips.csv
│       └── README.md
```

`ips.csv` is only needed for the command-line batch mode.

---

## Usage

> **Windows:** type `python` instead of `python3` in every command below, for example `python nsg_lookup_gui.py`. On macOS and Linux, use `python3`.

### Desktop window (GUI)
```bash
# macOS / Linux
python3 nsg_lookup_gui.py

# Windows
python nsg_lookup_gui.py
```

1. The window opens and loads all subnets from Azure once. When the status in the bottom right says "N subnets loaded", it is ready.
2. Type an IP in the first box. Press Enter or click **+ Add IP** for another box, or paste a whole list into any box and it splits into rows.
3. Click **Look up all**. The results table fills in, one numbered row per IP.
4. Use the **Filter** box above the results to narrow them down. Click the **x** next to it, or press Esc, to show everything again.
5. Click **Copy results** to paste into a ticket or Excel, or **Export CSV** to save them.
6. After changing subnets in Azure, click **Refresh subnets** to reload the data. Clear all empties the boxes, the results, and the filter so you can start fresh.

Row colours: red means the IP was not found in any known subnet or is not a valid IP address, orange means the subnet has no NSG attached.

Keyboard shortcuts:

| Action | macOS | Windows and Linux |
|---|---|---|
| Next IP box (adds one at the end of the list) | Enter | Enter |
| Jump to the filter box | Cmd+F | Ctrl+F |
| Clear the filter (while in the filter box) | Esc | Esc |
| Copy the selected result rows | Cmd+C | Ctrl+C |
| Full screen on or off | Cmd+Ctrl+F | F11 |

Window snapping and maximising work as normal for your OS. Dragging a column heading reorders the columns, and dragging a divider resizes them.

### Command line: single lookup
```bash
# macOS / Linux / Cloud Shell
python3 nsg_lookup_universal.py

# Windows
python nsg_lookup_universal.py
```

### Command line: batch lookup
```bash
# macOS / Linux / Cloud Shell
python3 nsg_lookup_universal.py --batch ips.csv

# Windows
python nsg_lookup_universal.py --batch ips.csv
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
| Read-only | Every Azure call this tool makes is a read-only GET (`az graph query`) |
| Not found | If an IP isn't found, it isn't in any subnet this account has Reader access to |
| Copy and export follow the filter | In the GUI, Copy results and Export CSV include only the rows currently shown. Clear the filter first to get everything |
| Duplicate IPs | In the GUI, an IP listed twice is looked up once |
| Subnet data is a snapshot | The GUI loads subnets once at startup. Use Refresh subnets after network changes |
| Need ALLOW/DENY? | Use `nsg_check` instead, this tool only resolves subnet/NSG, it doesn't evaluate rules |

---

## Author
Built as part of a cloud automation project to speed up NSG and network troubleshooting on Azure.
