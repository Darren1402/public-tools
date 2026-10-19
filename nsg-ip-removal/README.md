# Azure NSG IP Removal

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-7B42BC?style=flat&logo=terraform&logoColor=white)

## Overview
This tool removes IPs from Azure NSG rules, or deletes whole rules, in a Terraform NSG rules CSV. It works from a tracker Excel where the **Remarks** column says what to change for each rule.

It is a desktop window (GUI): pick the Excel, pick the CSV, pick the VNet, click **Run**, and review the results table.

It replaces the manual workflow of reading each remark, searching the CSV for the rule, and deleting IPs by hand, rule by rule.

## Download
Get the latest version from [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-ip-removal&expanded=true):
- Desktop window (Windows, macOS, Linux): `nsg_ip_removal_gui.py`

## Use Case
This is designed for NSG clean-ups where hundreds of rules each need a few IPs removed, and some rules need deleting entirely.

It helps:
- Apply hundreds of IP removals in one run instead of editing the CSV by hand
- Avoid typos and missed IPs when editing semicolon-separated IP lists
- See at a glance which rules changed, which need checking, and which were not found
- Keep the original CSV untouched until the result has been reviewed

---

## Features
| Feature | What it does |
|---|---|
| Remove source IPs | `Remove from Source Address:` followed by IPs removes those IPs from the rule's `source` column |
| Remove destination IPs | `Remove from Destination Address:` followed by IPs removes them from the `dest` column. Both can appear in the same remark |
| Delete rule | `Delete the NSG Rule` removes the whole rule row |
| VNet filter | The VNet list fills in from the Excel. Only rows for the chosen VNet are processed, or pick `ALL` |
| Rule matching | Matches on rule name + NSG name. Ignores dots, spaces and case, so `LDAPS` matches `LDAP.S`. Loose matches are flagged |
| Results table | One row per change with the Excel row number, rule, field, status, IPs removed, and IPs not found. Colour coded |
| Safe output | Writes `<name>_updated.csv` next to the original. The original CSV is never changed |
| Never empties a rule | If removing IPs would leave the field empty, the rule is skipped and flagged for a manual decision |
| Stable IDs | `local_id` values are not renumbered, so Terraform only sees the rules that actually changed |
| Keeps formatting | Keeps the CSV's original column order, line endings, and encoding |
| Report | Saves `ip_removal_report.csv` to Documents. **Open report** and **Open output folder** buttons |
| Remembers paths | The Excel, CSV, and VNet from the last run are filled in next time |
| No pip installs | Standard library only, including reading the Excel file |

---

## Platform notes

The script works on every platform. There is no separate Windows-only or Mac-only file.

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
- The tracker Excel (`.xlsx`) with the columns below
- A local copy of the Terraform NSG rules CSV

**Tracker Excel** needs these columns (extra columns are ignored):

| Remarks | NetworkSecurityGroupName | VirtualNetwork | RuleName |
|---|---|---|---|
| Remove from Source Address:<br>10.0.0.12<br>10.0.0.13 | nsg-app01 | MY-VNET | ALLOW-IN-TCP-APP-TO-DB |
| Delete the NSG Rule | nsg-app01 | MY-VNET | ALLOW-IN-TCP-OLD-RULE |

**Terraform CSV** needs the columns `name`, `source`, `dest`, and `nsg`. Multiple IPs in one cell are separated by `;`.

---

## Where to put this

Keep this in its own standalone folder, not inside a Terraform or infrastructure repo:

```
Documents/
├── Terraform/
│   └── my-project/
│       └── MY-VNET/
│           └── network_rules/
│               └── nsg_rules/
│                   └── nsg_rules.csv
├── Scripts/
│   └── nsg-ip-removal/
│       ├── nsg_ip_removal_gui.py
│       └── README.md
```

---

## Usage

> **Windows:** type `python` instead of `python3`, for example `python nsg_ip_removal_gui.py`. On macOS and Linux, use `python3`.

```bash
# macOS / Linux
python3 nsg_ip_removal_gui.py

# Windows
python nsg_ip_removal_gui.py
```

1. Next to **Excel tracker**, click **Browse** and pick the tracker Excel. The **VNet** list fills in from it.
2. Next to **Terraform CSV**, click **Browse** and pick the NSG rules CSV in `network_rules/nsg_rules`.
3. Pick the **VNet**, or `ALL`, and click **Run**.
4. Check the results table. Then click **Open output folder** to find `<name>_updated.csv`.
5. Compare the original and updated CSV, then replace the original with the updated file and run `terraform plan`.

To compare the two files:
```bash
# macOS / Linux
diff nsg_rules.csv nsg_rules_updated.csv

# Windows
fc nsg_rules.csv nsg_rules_updated.csv
```

Row colours: green means done, amber means check it (loose name match, partial, or skipped), red means a problem (rule not found, or remark not understood).

| Status | Meaning |
|---|---|
| `REMOVED` | All listed IPs removed |
| `RULE_DELETED` | Whole rule removed |
| `PARTIAL` | Some listed IPs were not in the rule |
| `NOTHING_TO_REMOVE` | None of the listed IPs were in the rule |
| `SKIPPED_WOULD_BE_EMPTY` | Removing would leave the field empty, rule left as is |
| `RULE_NOT_FOUND` | No CSV rule matched the rule name + NSG name |
| `VNET_FOLDER_NOT_FOUND` | The CSV could not be found |
| `REMARK_NOT_UNDERSTOOD` | Remark has no recognised instruction |

---

## Changelog
See [Releases](https://github.com/Darren1402/public-tools/releases?q=nsg-ip-removal&expanded=true) for version history.

---

## Notes
| Note | Detail |
|---|---|
| Original is never changed | All changes go to `<name>_updated.csv`. Each run starts from the original again, so it is safe to re-run |
| Does not touch Azure | The tool only edits a local CSV. Changes reach Azure only when you apply them through Terraform |
| Check the CSV name | If the Note column shows `CSV name: ...`, the Excel and CSV spell the rule differently. The change was still made, check it is the right rule |
| Empty rules | A rule is never left with an empty source or destination. Use "Delete the NSG Rule" if the whole rule should go |
| Gaps in local_id | Deleted rules leave gaps in `local_id`. This is intended, renumbering would make Terraform recreate other rules |
| Close the CSV first | If the CSV is open in Excel, the updated file may fail to save. Close it and run again |
| Avoid saving from Numbers or Excel | Opening the CSV is fine, but saving it from a spreadsheet app can reformat it. Let the tool write the file |

---

## Author
Built as part of a cloud automation project to speed up NSG rule clean-ups on Azure.
