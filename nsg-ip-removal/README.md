# NSG IP Removal

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Bulk-removes IPs from (or deletes) Azure NSG rules stored as Terraform CSV files, driven by a tracker Excel file.

Built for clean-ups with hundreds of rules where each rule needs a few IPs removed by hand.

## What it does

1. Reads the tracker Excel and looks at the **Remarks** column.
2. For each row, finds the vnet folder from the **VirtualNetwork** column:
   `<repo>/<VirtualNetwork>/network_rules/nsg_rules/*.csv`
3. Matches the CSV rule on **RuleName + NetworkSecurityGroupName**.
4. Applies the remark:

| Remark in Excel | Action |
|---|---|
| `Remove from Source Address:` + IPs | Removes those IPs from the `source` column |
| `Remove from Destination Address:` + IPs | Removes those IPs from the `dest` column |
| `Delete the NSG Rule` | Removes the whole rule row |

5. Writes `<name>_updated.csv` next to the original. **The original file is never changed.**
6. Writes a report CSV and prints a summary.

## Safety checks

- Never leaves a source/dest field empty. If removing IPs would empty it, the rule is skipped and flagged.
- Rule names are matched ignoring dots and spaces (`LDAPS` matches `LDAP.S`). Loose matches are flagged on screen.
- `local_id` values are not renumbered, so Terraform only sees the rules that actually changed.
- Keeps the CSV's original line endings and encoding.

## Requirements

Python 3.8+. No packages to install (the Excel file is read with built-in Python).

## Input format

**Tracker Excel** needs these columns (extra columns are ignored):

| Remarks | NetworkSecurityGroupName | VirtualNetwork | RuleName |
|---|---|---|---|
| Remove from Source Address:<br>10.0.0.12<br>10.0.0.13 | nsg-app01 | MY-VNET | ALLOW-IN-TCP-APP-TO-DB |
| Delete the NSG Rule | nsg-app01 | MY-VNET | ALLOW-IN-TCP-OLD-RULE |

**Terraform CSV** needs columns `name`, `source`, `dest`, `nsg`. Multiple IPs in one cell are separated by `;`.

## Usage

### GUI (easiest)

Run the script with no options and a window opens:

```
python remove_nsg_ips.py        # Windows
python3 remove_nsg_ips.py       # Mac
```

1. **Browse** to the tracker Excel. The VNet list fills in from the Excel.
2. **Browse** to the NSG rules CSV inside `network_rules/nsg_rules` (for example `networkmz_nsg_rules.csv`).
3. Pick a **VNet** (or `ALL`) and click **Run**.
4. Results show in a table: green = OK, amber = check (loose name match, partial, skipped), red = problem.
5. Use **Open output folder** or **Open report** to check the result.

Paths and the last VNet are remembered for next time.

> Windows Python from python.org includes the GUI library (tkinter). On Mac with Homebrew Python, run `brew install python-tk` once if the window does not open.

### Command line

**Mac / Linux**
```bash
python3 remove_nsg_ips.py --excel ~/Documents/tracker.xlsx --repo ~/Documents/terraform-repo --vnet MY-VNET
```

**Windows**
```
python remove_nsg_ips.py --excel "C:\path\tracker.xlsx" --repo "C:\path\terraform-repo" --vnet MY-VNET
```

Use `--vnet ALL` to process every vnet in the Excel.

| Option | Description |
|---|---|
| `--excel` | Tracker Excel file (required) |
| `--repo` | The NSG rules CSV, its `nsg_rules` folder, the vnet folder, or the repo root (required) |
| `--vnet` | Vnet to process, must match the folder name, or `ALL` (required) |
| `--sheet` | Excel sheet name (default: active sheet) |
| `--report` | Report path (default: `~/Documents/ip_removal_report.csv`) |

## Checking the result

```bash
diff networkmz_nsg_rules.csv networkmz_nsg_rules_updated.csv      # Mac / Linux
fc networkmz_nsg_rules.csv networkmz_nsg_rules_updated.csv        # Windows
```

When happy, replace the original with the `_updated` file and run `terraform plan`.

## Report statuses

| Status | Meaning |
|---|---|
| `REMOVED` | All listed IPs removed |
| `PARTIAL` | Some listed IPs were not in the rule |
| `NOTHING_TO_REMOVE` | None of the listed IPs were in the rule |
| `SKIPPED_WOULD_BE_EMPTY` | Removing would leave the field empty, rule left as is |
| `RULE_DELETED` | Whole rule removed |
| `RULE_NOT_FOUND` | No CSV rule matched the name + NSG |
| `VNET_FOLDER_NOT_FOUND` | No folder matched the VirtualNetwork name |
| `REMARK_NOT_UNDERSTOOD` | Remark has no recognised instruction |

## Changelog

### v1.1.1
- GUI picks the NSG rules CSV directly
- Command line `--repo` accepts the CSV, the nsg_rules folder, the VNet folder, or the repo root

### v1.1.0
- Added GUI: run with no options to open it
- Results table with colour coding, VNet list read from the Excel, remembers last paths
- Command line mode unchanged

### v1.0.0
- Remove IPs from source/dest based on Excel remarks
- Delete whole rules with "Delete the NSG Rule"
- Loose rule-name matching with on-screen warnings
- No external dependencies (built-in Excel reader)
- Works on Mac and Windows
