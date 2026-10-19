#!/usr/bin/env python3
"""
NSG IP Removal (GUI) v1.0.0

Removes IPs from (or deletes) Azure NSG rules in a Terraform CSV, driven by the
Remarks column of a tracker Excel.

  1. Pick the tracker Excel, the Terraform NSG rules CSV, and the VNet.
  2. Rows with "Remove from Source/Destination Address:" have those IPs removed.
     Rows with "Delete the NSG Rule" have the whole rule removed.
  3. Rules are matched on rule name + NSG name.
  4. Result is written to <name>_updated.csv next to the original. The original is never changed.

Run:  python3 nsg_ip_removal_gui.py   (Windows: python nsg_ip_removal_gui.py)
Standard library only. The GUI needs tkinter.
"""
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


def source_csvs(folder):
    return [p for p in glob.glob(os.path.join(folder, "*.csv")) if not p.endswith("_updated.csv")]


def resolve_nsg_dir(repo, vnet):
    """
    Accepts any of:
      <repo>                                   -> <repo>/<vnet>/network_rules/nsg_rules
      <repo>/<vnet>                            -> <repo>/<vnet>/network_rules/nsg_rules
      <repo>/<vnet>/network_rules/nsg_rules    -> used as is
    """
    if source_csvs(repo):                                   # pointed straight at nsg_rules
        return repo
    direct = os.path.join(repo, "network_rules", "nsg_rules")
    if os.path.isdir(direct):                               # pointed at the vnet folder
        return direct
    folder = find_vnet_folder(repo, vnet)                   # pointed at the repo root
    if folder:
        nested = os.path.join(folder, "network_rules", "nsg_rules")
        if os.path.isdir(nested):
            return nested
    return None


def load_vnet_csvs(repo, vnet, cache):
    if vnet in cache:
        return cache[vnet]
    if os.path.isfile(repo):                                # pointed straight at the CSV
        cache[vnet] = (os.path.dirname(repo), [CsvFile(repo)])
        return cache[vnet]
    folder = resolve_nsg_dir(repo, vnet)
    files = [CsvFile(p) for p in source_csvs(folder)] if folder else []
    cache[vnet] = (folder, files)
    return cache[vnet]


# ---------- core ----------
DEFAULT_REPORT = os.path.join(DOCS, "ip_removal_report.csv")
REPORT_HEADER = ["excel_row", "vnet", "nsg", "rule", "field", "status", "removed", "not_found", "csv_file"]


def list_vnets(excel, sheet=None):
    """Unique VirtualNetwork values in the tracker, in order of appearance."""
    seen = []
    for row in read_tracker(excel, sheet):
        if row["vnet"] and row["vnet"] not in seen:
            seen.append(row["vnet"])
    return seen


def process(excel, repo, vnet, sheet=None, report_path=DEFAULT_REPORT):
    """Run the whole job. Returns (report_rows, written_files, skipped_other_vnet)."""
    for p in (excel, repo):
        if not os.path.exists(p):
            raise FileNotFoundError(f"Not found: {p}")

    cache, report = {}, []
    skipped_other_vnet = 0

    for row in read_tracker(excel, sheet):
        if vnet.upper() != "ALL" and row["vnet"].lower() != vnet.lower():
            skipped_other_vnet += 1
            continue
        is_delete = "delete the nsg rule" in row["remark"].lower()
        todo = parse_remarks(row["remark"])
        if not is_delete and not todo["source"] and not todo["dest"]:
            report.append([row["excel_row"], row["vnet"], row["nsg"], row["rule"], "", "REMARK_NOT_UNDERSTOOD", "", "", ""])
            continue

        folder, files = load_vnet_csvs(repo, row["vnet"], cache)
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

    written = []
    for folder, files in cache.values():
        for cf in files:
            if cf.changes:
                written.append((cf.save_copy(), cf.changes))

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(REPORT_HEADER)
        w.writerows(report)

    return report, written, skipped_other_vnet


def summary_text(vnet, report, written, skipped_other_vnet, report_path):
    counts = {}
    for r in report:
        key = r[5].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    out = [f"VNet filter : {vnet}", f"Ignored     : {skipped_other_vnet} row(s) for other vnets", "", "Results:"]
    for k, v in sorted(counts.items()):
        out.append(f"  {k:24} {v}")
    for r in report:
        if r[5] not in ("REMOVED", "RULE_DELETED"):
            out.append(f"  ! row {r[0]}: {r[3]}  ->  {r[5]}" + (f"  (not found: {r[7]})" if r[7] else ""))
    out.append("")
    out.append("Updated copies:" if written else "No changes, no copy written.")
    for p, n in written:
        out.append(f"  {p}  ({n} change(s))")
    out.append("")
    out.append(f"Report: {report_path}")
    return "\n".join(out)


# ---------- GUI ----------
SETTINGS = os.path.join(os.path.expanduser("~"), ".nsg_ip_removal.json")


def open_path(path):
    """Open a file or folder with the OS default app."""
    import subprocess
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


def gui_main():
    import json
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    def load_settings():
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings():
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"excel": excel_var.get(), "repo": repo_var.get(), "vnet": vnet_var.get()}, f)
        except Exception:
            pass

    root = tk.Tk()
    root.title("NSG IP Removal v1.0.0")
    root.geometry("1280x660")
    root.minsize(800, 480)

    saved = load_settings()
    excel_var = tk.StringVar(value=saved.get("excel", ""))
    repo_var = tk.StringVar(value=saved.get("repo", ""))
    vnet_var = tk.StringVar(value=saved.get("vnet", ""))
    status_var = tk.StringVar(value="Pick the Excel file and the Terraform repo folder, choose a VNet, then click Run.")
    state = {"written": [], "report": DEFAULT_REPORT}

    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)

    def refresh_vnets(*_):
        path = excel_var.get()
        if os.path.isfile(path):
            try:
                vnets = list_vnets(path)
                vnet_box["values"] = vnets + ["ALL"]
                if vnet_var.get() not in vnet_box["values"] and vnets:
                    vnet_var.set(vnets[0])
            except SystemExit as e:
                messagebox.showerror("Excel problem", str(e))
            except Exception as e:
                messagebox.showerror("Excel problem", f"Could not read the Excel file:\n{e}")

    def pick_excel():
        p = filedialog.askopenfilename(title="Select tracker Excel", filetypes=[("Excel", "*.xlsx"), ("All files", "*.*")])
        if p:
            excel_var.set(os.path.normpath(p))
            refresh_vnets()

    def pick_repo():
        start = repo_var.get()
        start = os.path.dirname(start) if os.path.isfile(start) else (start if os.path.isdir(start) else os.path.expanduser("~"))
        p = filedialog.askopenfilename(title="Select the NSG rules CSV (in network_rules/nsg_rules)",
                                       initialdir=start, filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if p:
            repo_var.set(os.path.normpath(p))

    ttk.Label(top, text="Excel tracker").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(top, textvariable=excel_var).grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(top, text="Browse...", command=pick_excel).grid(row=0, column=2)

    ttk.Label(top, text="Terraform CSV").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(top, textvariable=repo_var).grid(row=1, column=1, sticky="ew", padx=8)
    ttk.Button(top, text="Browse...", command=pick_repo).grid(row=1, column=2)

    ttk.Label(top, text="VNet").grid(row=2, column=0, sticky="w", pady=4)
    vnet_box = ttk.Combobox(top, textvariable=vnet_var, width=40)
    vnet_box.grid(row=2, column=1, sticky="w", padx=8)

    btns = ttk.Frame(top)
    btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
    run_btn = ttk.Button(btns, text="Run")
    run_btn.pack(side="left")
    folder_btn = ttk.Button(btns, text="Open output folder", state="disabled",
                            command=lambda: open_path(os.path.dirname(state["written"][0][0])))
    folder_btn.pack(side="left", padx=8)
    report_btn = ttk.Button(btns, text="Open report", state="disabled", command=lambda: open_path(state["report"]))
    report_btn.pack(side="left")

    ttk.Label(root, textvariable=status_var, padding=(12, 4)).pack(fill="x")

    cols = ("row", "rule", "field", "status", "removed", "not_found", "note", "file")
    heads = ("Row", "Rule", "Field", "Status", "Removed", "Not found", "Note", "CSV file")
    widths = (45, 400, 75, 175, 240, 120, 380, 200)
    table_frame = ttk.Frame(root, padding=(12, 0, 12, 12))
    table_frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(table_frame, columns=cols, show="headings")
    for c, h, w in zip(cols, heads, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, anchor="w", stretch=(c == "rule"))
    ysb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    xsb = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ysb.grid(row=0, column=1, sticky="ns")
    xsb.grid(row=1, column=0, sticky="ew")
    table_frame.rowconfigure(0, weight=1)
    table_frame.columnconfigure(0, weight=1)
    tree.tag_configure("ok", foreground="#1a7f37")
    tree.tag_configure("warn", foreground="#9a6700")
    tree.tag_configure("bad", foreground="#cf222e")

    def run():
        excel, repo, vnet = excel_var.get().strip(), repo_var.get().strip(), vnet_var.get().strip()
        if not (excel and repo and vnet):
            messagebox.showwarning("Missing input", "Please fill in Excel tracker, Terraform CSV and VNet.")
            return
        save_settings()
        tree.delete(*tree.get_children())
        status_var.set("Running...")
        root.update_idletasks()
        try:
            report, written, skipped = process(excel, repo, vnet, report_path=DEFAULT_REPORT)
        except SystemExit as e:
            status_var.set("Failed.")
            messagebox.showerror("Error", str(e))
            return
        except Exception as e:
            status_var.set("Failed.")
            messagebox.showerror("Error", f"{type(e).__name__}: {e}")
            return

        for r in report:
            st = r[5]
            if st.startswith(("REMOVED", "RULE_DELETED")):
                tag = "warn" if "(CSV name:" in st else "ok"
            elif st.startswith(("PARTIAL", "NOTHING_TO_REMOVE", "SKIPPED")):
                tag = "warn"
            else:
                tag = "bad"
            base, _, note = st.partition(" (")
            note = note.rstrip(")")
            tree.insert("", "end", values=(r[0], r[3], r[4], base, r[6].replace(";", ", "),
                                           r[7].replace(";", ", "), note, r[8]), tags=(tag,))

        state["written"], state["report"] = written, DEFAULT_REPORT
        folder_btn.configure(state="normal" if written else "disabled")
        report_btn.configure(state="normal")
        counts = {}
        for r in report:
            k = r[5].split(" (")[0]
            counts[k] = counts.get(k, 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing matched"
        files = "; ".join(os.path.basename(p) for p, _ in written) or "no file written"
        status_var.set(f"Done. {parts}.   Output: {files}   (green = OK, amber = check, red = problem)")

    run_btn.configure(command=run)
    vnet_box.bind("<Button-1>", lambda e: refresh_vnets() if not vnet_box["values"] else None)
    if excel_var.get():
        root.after(100, refresh_vnets)
    root.mainloop()


def main():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("This tool needs tkinter, which is missing from this Python install.\n"
                 "Windows: re-run the python.org installer and tick 'tcl/tk and IDLE'.\n"
                 "macOS (Homebrew): brew install python-tk@<your version>, check with python3 --version.\n"
                 "Linux (Debian/Ubuntu): sudo apt install python3-tk")
    gui_main()


if __name__ == "__main__":
    main()
