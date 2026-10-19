#!/usr/bin/env python3
"""
Remove IPs from Terraform NSG rule CSVs, driven by the Remarks column of the tracker Excel.

Flow:
  1. Read the tracker Excel (--excel).
  2. For rows whose Remarks say "Remove from Source Address:" (or Destination),
     collect the IPs listed under it. "Delete the NSG Rule" rows drop the whole rule row.
  3. Use the VirtualNetwork column to find <repo>/<VirtualNetwork>/network_rules/nsg_rules/*.csv
  4. Match the CSV row on rule name + NSG name, remove the IPs.
  5. Write <name>_updated.csv next to the original. The original is never touched.
  6. Write a report CSV and print a summary.

Usage:
  python3 remove_nsg_ips.py --excel tracker.xlsx --repo /path/to/terraform-repo --vnet MY-VNET
  python3 remove_nsg_ips.py --excel tracker.xlsx --repo /path/to/terraform-repo --vnet ALL

No extra packages needed: the Excel file is read with built-in Python only.
"""
import argparse
import csv
import glob
import io
import ipaddress
import os
import re
import sys

import posixpath
import zipfile
import xml.etree.ElementTree as ET

DOCS = os.path.expanduser("~/Documents")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
SECTION_RE = re.compile(r"remove\s+from\s+(source|destination|dest)\b[^:\n]*:?", re.I)


def norm(value):
    """10.0.0.1 == 10.0.0.1/32. Non-IP values (ASG names, *, Storage) compare case-insensitively."""
    value = value.strip()
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return value.lower()


def loose(name):
    """Compare names ignoring case and anything that isn't a letter, digit or hyphen (e.g. LDAP.S == LDAPS)."""
    return re.sub(r"[^a-z0-9-]", "", name.lower())


def parse_remarks(text):
    out = {"source": [], "dest": []}
    parts = SECTION_RE.split(text or "")
    for i in range(1, len(parts), 2):
        field = "source" if parts[i].lower() == "source" else "dest"
        out[field].extend(IP_RE.findall(parts[i + 1]))
    return out


# ---------- Excel ----------
NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def col_index(ref):
    """'A1' -> 0, 'AB12' -> 27"""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def read_xlsx_rows(path, sheet=None):
    """Read an .xlsx with built-in Python only (no openpyxl needed). Returns a list of row lists."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = wb.findall("m:sheets/m:sheet", NS)
        if sheet:
            chosen = next((s for s in sheets if s.get("name") == sheet), None)
            if chosen is None:
                sys.exit(f"Sheet '{sheet}' not found. Sheets: {[s.get('name') for s in sheets]}")
        else:
            active = 0
            view = wb.find("m:bookViews/m:workbookView", NS)
            if view is not None and view.get("activeTab"):
                active = int(view.get("activeTab"))
            chosen = sheets[active]
        rid = chosen.get(f"{{{NS['r']}}}id")

        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.findall("rel:Relationship", NS) if r.get("Id") == rid)
        sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))

        rows = []
        for row in ET.fromstring(z.read(sheet_path)).iter(f"{{{NS['m']}}}row"):
            rnum = int(row.get("r", len(rows) + 1))
            while len(rows) < rnum - 1:  # keep Excel row numbers aligned when blank rows are skipped
                rows.append([])
            values = {}
            for c in row.findall("m:c", NS):
                t, v = c.get("t"), c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                else:
                    val = v.text if v is not None else None
                values[col_index(c.get("r", "A"))] = val
            if values:
                rows.append([values.get(i) for i in range(max(values) + 1)])
            else:
                rows.append([])
        return rows


def read_tracker(path, sheet):
    rows = read_xlsx_rows(path, sheet)

    # Find the header row (the one containing "Remarks")
    for h_idx, row in enumerate(rows):
        header = [str(c).strip().lower() if c is not None else "" for c in row]
        if "remarks" in header:
            break
    else:
        sys.exit("Could not find a 'Remarks' header in the Excel.")

    need = {"remarks": None, "networksecuritygroupname": None, "virtualnetwork": None, "rulename": None}
    for k in need:
        if k not in header:
            sys.exit(f"Excel is missing column '{k}'. Found: {[h for h in header if h]}")
        need[k] = header.index(k)

    for r_idx, r in enumerate(rows[h_idx + 1:], start=h_idx + 2):
        get = lambda k: str(r[need[k]]).strip() if need[k] < len(r) and r[need[k]] is not None else ""
        remark = get("remarks")
        if not remark:
            continue
        yield {
            "excel_row": r_idx,
            "remark": remark,
            "nsg": get("networksecuritygroupname"),
            "vnet": get("virtualnetwork"),
            "rule": get("rulename"),
        }


# ---------- CSV ----------
class CsvFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
            self.encoding = "utf-8-sig" if self.bom else "utf-8"
        except UnicodeDecodeError:  # file saved by Excel on Windows
            text = raw.decode("cp1252")
            self.encoding = "cp1252"
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing_newline = text.endswith(("\n", "\r"))
        self.rows = list(csv.reader(io.StringIO(text, newline="")))
        self.header = [h.strip().lower() for h in self.rows[0]]
        for col in ("name", "source", "dest", "nsg"):
            if col not in self.header:
                raise ValueError(f"{path}: missing column '{col}'")
        self.changes = 0
        self.to_delete = set()  # row indexes to drop when saving

    def col(self, name):
        return self.header.index(name)

    def out_path(self):
        base, ext = os.path.splitext(self.path)
        return f"{base}_updated{ext}"

    def save_copy(self):
        buf = io.StringIO()
        keep = [r for i, r in enumerate(self.rows) if i not in self.to_delete]
        csv.writer(buf, lineterminator=self.newline).writerows(keep)
        data = buf.getvalue()
        if not self.trailing_newline:
            data = data[: -len(self.newline)]
        with open(self.out_path(), "w", encoding=self.encoding, newline="") as f:
            f.write(data)
        return self.out_path()


def remove_ips(cell, ips):
    entries = [e.strip() for e in cell.split(";") if e.strip()]
    targets = {norm(i) for i in ips}
    present = {norm(e) for e in entries}
    kept = [e for e in entries if norm(e) not in targets]
    removed = [e for e in entries if norm(e) in targets]
    not_found = [i for i in ips if norm(i) not in present]
    return ";".join(kept), removed, not_found


def find_vnet_folder(repo, vnet):
    """Case-insensitive match of the VirtualNetwork name to a folder under the repo."""
    for d in os.listdir(repo):
        if d.lower() == vnet.lower() and os.path.isdir(os.path.join(repo, d)):
            return os.path.join(repo, d)
    return None


def load_vnet_csvs(repo, vnet, cache):
    if vnet in cache:
        return cache[vnet]
    folder = find_vnet_folder(repo, vnet)
    files = []
    if folder:
        for p in glob.glob(os.path.join(folder, "network_rules", "nsg_rules", "*.csv")):
            if not p.endswith("_updated.csv"):
                files.append(CsvFile(p))
    cache[vnet] = (folder, files)
    return cache[vnet]


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--excel", required=True, help="Tracker .xlsx with the Remarks column")
    ap.add_argument("--repo", required=True, help="Terraform repo root containing the vnet folders")
    ap.add_argument("--vnet", required=True, help="VirtualNetwork to process (matches folder name), or ALL")
    ap.add_argument("--sheet", help="Excel sheet name (default: active sheet)")
    ap.add_argument("--report", default=os.path.join(DOCS, "ip_removal_report.csv"))
    args = ap.parse_args()

    for p in (args.excel, args.repo):
        if not os.path.exists(p):
            sys.exit(f"Not found: {p}")

    cache, report = {}, []
    skipped_other_vnet = 0

    for row in read_tracker(args.excel, args.sheet):
        if args.vnet.upper() != "ALL" and row["vnet"].lower() != args.vnet.lower():
            skipped_other_vnet += 1
            continue
        is_delete = "delete the nsg rule" in row["remark"].lower()
        todo = parse_remarks(row["remark"])
        if not is_delete and not todo["source"] and not todo["dest"]:
            report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], "", "REMARK_NOT_UNDERSTOOD", "", "", ""])
            continue

        folder, files = load_vnet_csvs(args.repo, row["vnet"], cache)
        if not folder:
            report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], "", "VNET_FOLDER_NOT_FOUND", "", "", ""])
            continue

        matches = []
        for cf in files:
            n, r = cf.col("nsg"), cf.col("name")
            for i, line in enumerate(cf.rows[1:], start=1):
                if len(line) > max(n, r) and loose(line[r]) == loose(row["rule"]) \
                        and loose(line[n]) == loose(row["nsg"]):
                    exact = line[r].strip().lower() == row["rule"].lower()
                    matches.append((cf, i, exact, line[r].strip()))
        if not matches:
            report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], "", "RULE_NOT_FOUND", "", "", ""])
            continue

        if is_delete:
            for cf, i, exact, csv_name in matches:
                cf.to_delete.add(i)
                cf.changes += 1
                status = "RULE_DELETED" + ("" if exact else f" (CSV name: {csv_name})")
                report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], "whole rule", status,
                               "", "", os.path.basename(cf.path)])
            continue

        for cf, i, exact, csv_name in matches:
            for field, ips in todo.items():
                if not ips:
                    continue
                c = cf.col(field)
                new, removed, not_found = remove_ips(cf.rows[i][c], ips)
                if not new:
                    status = "SKIPPED_WOULD_BE_EMPTY"
                elif not removed:
                    status = "NOTHING_TO_REMOVE"
                else:
                    status = "REMOVED" if not not_found else "PARTIAL"
                    cf.rows[i][c] = new
                    cf.changes += 1
                if not exact:
                    status += f" (CSV name: {csv_name})"
                report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], field, status,
                               ";".join(removed), ";".join(not_found), os.path.basename(cf.path)])

    # Write updated copies
    written = []
    for folder, files in cache.values():
        for cf in files:
            if cf.changes:
                written.append((cf.save_copy(), cf.changes))

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["excel_row", "vnet", "nsg", "rule", "field", "status", "removed", "not_found", "csv_file"])
        w.writerows(report)

    # Summary
    counts = {}
    for r in report:
        key = r[5].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    print(f"VNet filter : {args.vnet}")
    print(f"Ignored     : {skipped_other_vnet} row(s) for other vnets\n")
    print("Results:")
    for k, v in sorted(counts.items()):
        print(f"  {k:24} {v}")
    for r in report:
        if r[5] not in ("REMOVED", "RULE_DELETED"):
            print(f"  ! row {r[0]}: {r[3]}  ->  {r[5]}" + (f"  (not found: {r[7]})" if r[7] else ""))
    print("\nUpdated copies:" if written else "\nNo changes, no copy written.")
    for p, n in written:
        print(f"  {p}  ({n} change(s))")
    print(f"\nReport: {args.report}")


if __name__ == "__main__":
    main()
