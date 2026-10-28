# NSG Rule Checker

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-7B42BC?style=flat&logo=terraform&logoColor=white)

## Overview
This tool checks an NSG rules sheet (the tracker `.xlsx` or the Terraform `nsg_rules` `.csv`) for typing and Excel mistakes that break a Terraform deploy, then saves a fixed copy.

---

## Download
Download `nsg_rule_checker_gui.py` from the latest [Release](../../releases), or copy it from this folder.

---

## Use Case
NSG rules for the GCC NP Terraform infra are added through a CSV. Small human errors slip in: a space in front of a value, `,` instead of `;`, or Excel turning `22,443` into the number 22443. This tool finds them before they reach the pipeline.

---

## Features
- Reads `.xlsx` and `.csv`, standard library only (no pip installs)
- Finds the header row and columns by name (name, priority, direction, access, protocol, port, source, dest, nsg)
- Spaces in front or at the end, hidden non-breaking spaces, line breaks inside cells
- `,` or spaces used instead of `;` in ports and addresses, empty entries (`;;`, trailing `;`), duplicates
- Ports Excel joined into one number, e.g. `804,438,443` is split back into `80;443;8443` using a list of common ports plus the ports already used in the file
- Numbers shown with a thousands comma (`1,433`), which a CSV export would write with the comma
- Invalid IPs or CIDRs, CIDRs with host bits set, service tag spelling, a service tag mixed with IPs
- Direction, access and protocol spelling (`TCP` to `Tcp`), priority range 100 to 4096
- Duplicate priority (same NSG and direction) and duplicate rule names (same NSG)
- Saves `<name>_fixed.xlsx` or `<name>_fixed.csv`, keeps the original untouched, then re-reads the saved file to confirm every fix landed
- CSV output only changes the rows that were fixed and keeps the line endings, so the git diff stays small

---

## Platform notes
One file works on Windows, macOS and Linux.

| Setup | tkinter |
|---|---|
| Windows (python.org installer) | Included. Check with `python -m tkinter` |
| macOS (python.org installer) | Included |
| macOS (Homebrew Python) | `brew install python-tk@3.x`, match your `python3 --version` |
| Linux (Debian, Ubuntu) | `sudo apt install python3-tk` |

On Windows, use `python` instead of `python3` in the commands below.

---

## Prerequisites
- Python 3
- tkinter (see Platform notes)

---

## Where to put this

```
Documents/
├── Scripts/
│   └── nsg-rule-checker/
│       ├── nsg_rule_checker_gui.py
│       └── README.md
```

---

## Usage

```bash
python3 nsg_rule_checker_gui.py
```

1. Click **Browse…** and pick the `.xlsx` or `.csv`. For `.xlsx`, choose the sheet (defaults to `nsg`).
2. Click **Check** (or press F5). Each problem cell is listed with the current and fixed value.
3. Review the rows:

| Fix | Colour | Meaning | Saved? |
|---|---|---|---|
| Auto | White | Safe fix | Yes |
| Check | Orange | Best guess, e.g. a joined port number | Only if "Also save orange 'Check' fixes" is ticked |
| Manual | Red | Fix by hand (bad IP, duplicate priority) | No, unless you edit it |
| Note | Yellow | Worth a look, nothing changes | No |
| Edited | Green | Your own value | Yes |

4. Right-click a row to **Edit fixed value…**, **Don't fix this**, or **Restore suggested fix**.
5. Click **Save fixed copy**. Run Check on the `_fixed` file to confirm only Manual items are left.

In the Current value column, `␣` is a space, `⍽` a non-breaking space and `⏎` a line break.

Keyboard shortcuts:

| Action | macOS | Windows and Linux |
|---|---|---|
| Run check | F5 | F5 |
| Jump to the filter box | Cmd+F | Ctrl+F |
| Clear the filter (in the filter box) | Esc | Esc |
| Copy the selected result rows | Cmd+C | Ctrl+C |

Double-click a cell to copy it. **Copy results** copies the whole table for a ticket or Excel, **Export report** saves it as CSV.

---

## Changelog
See [Releases](../../releases).

- **v1.0.0**: first release

---

## Notes
- Encrypted workbooks (for example some sensitivity labels) can't be read. Save a copy as CSV UTF-8 and check that.
- Joined port numbers can't be split with certainty: `223,389` could be `22;3389` or `223;389`. That's why they are orange. The details pane lists the other options.
- In a fixed `.xlsx`, corrected cells are stored as text, so Excel can't turn them into numbers again.

---

## Author
Darren1402
