#!/usr/bin/env python3
"""
NSG Resource Group Lookup (GUI) v1.1.1

Takes a list of NSG names (from the NSG IP Removal report, or any CSV / Excel with an
NSG column) and finds, for each NSG:
  - the Subscription and Resource Group it lives in (Azure Resource Graph)
  - any resource locks that apply to it: on the subscription, the resource group, or the NSG itself

Use it to hand your senior engineer a clean list of locked resource groups to unlock
before NSG rules can be deleted.

Read-only: it only runs `az graph query` and `az lock list`. Nothing in Azure is changed.

Run:  python3 nsg_rg_lookup_gui.py   (Windows: python nsg_rg_lookup_gui.py)
Needs: Python 3 with tkinter, Azure CLI logged in (az login), Reader access.
Standard library only.
"""
import csv
import io
import json
import os
import posixpath
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile

VERSION = "1.1.1"
DOCS = os.path.join(os.path.expanduser("~"), "Documents")
SETTINGS = os.path.join(os.path.expanduser("~"), ".nsg_rg_lookup.json")
NSG_COLUMNS = ("nsg", "networksecuritygroupname", "networksecuritygroup", "nsg_name", "nsgname")
BATCH = 40           # NSG names per Resource Graph query, keeps the command line short on Windows
PAGE = 1000


# ---------- reading the input file ----------
NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def col_index(ref):
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def read_xlsx_rows(path):
    """Read the active sheet of an .xlsx with built-in Python only."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = wb.findall("m:sheets/m:sheet", NS)
        active = 0
        view = wb.find("m:bookViews/m:workbookView", NS)
        if view is not None and view.get("activeTab"):
            active = int(view.get("activeTab"))
        rid = sheets[active].get(f"{{{NS['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.findall("rel:Relationship", NS) if r.get("Id") == rid)
        sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        rows = []
        for row in ET.fromstring(z.read(sheet_path)).iter(f"{{{NS['m']}}}row"):
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
            rows.append([values.get(i) for i in range(max(values) + 1)] if values else [])
        return rows


def read_csv_rows(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    return list(csv.reader(io.StringIO(text, newline="")))


def read_nsgs(path):
    """
    Returns an ordered dict-like list of NSGs with counts from the input file:
      [{"nsg": name, "deleted": n, "updated": n, "not_found": n, "rows": n}, ...]
    Works with the NSG IP Removal report (nsg + status columns) or any file with an NSG column.
    """
    rows = read_xlsx_rows(path) if path.lower().endswith((".xlsx", ".xlsm")) else read_csv_rows(path)
    header_idx, header = None, []
    for i, row in enumerate(rows[:20]):
        h = [str(c).strip().lower().replace(" ", "") if c is not None else "" for c in row]
        if any(name in h for name in NSG_COLUMNS):
            header_idx, header = i, h
            break
    if header_idx is None:
        raise ValueError("No NSG column found. The file needs a column called 'nsg' or 'NetworkSecurityGroupName'.")
    nsg_col = next(header.index(n) for n in NSG_COLUMNS if n in header)
    status_col = header.index("status") if "status" in header else None

    out, index = [], {}
    for row in rows[header_idx + 1:]:
        name = str(row[nsg_col]).strip() if nsg_col < len(row) and row[nsg_col] else ""
        if not name:
            continue
        key = name.lower()
        if key not in index:
            index[key] = {"nsg": name, "deleted": 0, "updated": 0, "not_found": 0, "rows": 0}
            out.append(index[key])
        item = index[key]
        item["rows"] += 1
        status = str(row[status_col]).upper() if status_col is not None and status_col < len(row) and row[status_col] else ""
        if status.startswith("RULE_DELETED"):
            item["deleted"] += 1
        elif status.startswith(("REMOVED", "PARTIAL")):
            item["updated"] += 1
        elif status.startswith("RULE_NOT_FOUND"):
            item["not_found"] += 1
    return out


def read_many(paths):
    """Merge several reports. Each NSG appears once, with counts summed and the reports it came from."""
    merged, index, problems = [], {}, []
    for path in paths:
        try:
            items = read_nsgs(path)
        except Exception as e:
            problems.append(f"{os.path.basename(path)}: {e}")
            continue
        for it in items:
            key = it["nsg"].lower()
            if key not in index:
                index[key] = {**it, "sources": []}
                merged.append(index[key])
            else:
                for k in ("deleted", "updated", "not_found", "rows"):
                    index[key][k] += it[k]
            name = os.path.basename(path)
            if name not in index[key]["sources"]:
                index[key]["sources"].append(name)
    return merged, problems


# ---------- Azure CLI ----------
class AzError(Exception):
    pass


def az_exe():
    """Windows resolves the Azure CLI as az.cmd, macOS/Linux as az."""
    for name in (("az.cmd", "az") if sys.platform.startswith("win") else ("az",)):
        path = shutil.which(name)
        if path:
            return path
    raise AzError("Azure CLI (az) not found. Install it and run 'az login' first.")


def az(args):
    cmd = [az_exe()] + args + ["--output", "json", "--only-show-errors"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise AzError(f"Could not run the Azure CLI: {e}")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        if "az login" in err or "AADSTS" in err or "expired" in err.lower():
            raise AzError("Not logged in to Azure (or the login expired). Run 'az login' and try again.\n\n" + err[:600])
        raise AzError(err[:800] or f"az exited with code {p.returncode}")
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        raise AzError("Unexpected output from the Azure CLI:\n" + p.stdout[:500])


def kql_list(names):
    return ", ".join("'" + n.replace("'", "''") + "'" for n in names)


def graph_query(query):
    """Run a Resource Graph query and page past the 1,000-row limit."""
    rows, skip = [], None
    while True:
        args = ["graph", "query", "-q", query, "--first", str(PAGE)]
        if skip:
            args += ["--skip-token", skip]
        res = az(args) or {}
        rows += res.get("data", [])
        skip = res.get("skip_token") or res.get("skipToken")
        if not skip:
            return rows


def find_nsgs(names, progress=lambda msg: None):
    """name(lower) -> list of {name, resourceGroup, subscriptionId, subscription, location, id}"""
    found = {}
    for i in range(0, len(names), BATCH):
        chunk = names[i:i + BATCH]
        progress(f"Looking up NSGs {i + 1}-{i + len(chunk)} of {len(names)} in Resource Graph...")
        q = ("resources "
             "| where type =~ 'microsoft.network/networksecuritygroups' "
             f"| where name in~ ({kql_list(chunk)}) "
             "| join kind=leftouter (resourcecontainers "
             "| where type =~ 'microsoft.resources/subscriptions' "
             "| project subscriptionId, subscription=name) on subscriptionId "
             "| project name, resourceGroup, subscriptionId, subscription, location, id")
        for r in graph_query(q):
            found.setdefault(r["name"].lower(), []).append(r)
    return found


def lock_scope(lock_id):
    """The resource a lock sits on: everything before /providers/Microsoft.Authorization/locks/."""
    low = lock_id.lower()
    cut = low.rfind("/providers/microsoft.authorization/locks/")
    return lock_id[:cut] if cut >= 0 else lock_id


def get_locks(subscription_ids, progress=lambda msg: None):
    """subscriptionId -> (list of locks, error or None). One az call per subscription."""
    out = {}
    for n, sub in enumerate(subscription_ids, 1):
        progress(f"Reading locks for subscription {n} of {len(subscription_ids)}...")
        try:
            out[sub] = (az(["lock", "list", "--subscription", sub]) or [], None)
        except AzError as e:
            out[sub] = ([], str(e).splitlines()[0][:200])
    return out


def locks_for(nsg, sub_locks):
    """Locks that apply to this NSG: on its subscription, its resource group, or the NSG itself."""
    sub_scope = f"/subscriptions/{nsg['subscriptionId']}".lower()
    rg_scope = f"{sub_scope}/resourcegroups/{nsg['resourceGroup']}".lower()
    nsg_scope = nsg["id"].lower()
    hits = []
    for lk in sub_locks:
        scope = lock_scope(lk.get("id", "")).lower().rstrip("/")
        if scope == sub_scope:
            where = "Subscription"
        elif scope == rg_scope:
            where = "Resource group"
        elif scope == nsg_scope:
            where = "NSG"
        else:
            continue
        hits.append({"name": lk.get("name", ""), "level": lk.get("level", ""), "where": where,
                     "notes": lk.get("notes") or ""})
    return hits


def lookup(nsg_items, progress=lambda msg: None):
    """Full lookup. Returns result rows (dicts), one per NSG found (or one NOT_FOUND row)."""
    names = [i["nsg"] for i in nsg_items]
    found = find_nsgs(names, progress)
    subs = sorted({r["subscriptionId"] for rs in found.values() for r in rs})
    locks = get_locks(subs, progress)

    results = []
    for item in nsg_items:
        matches = found.get(item["nsg"].lower(), [])
        if not matches:
            results.append({**item, "status": "NOT_FOUND", "subscription": "", "subscriptionId": "",
                            "resourceGroup": "", "locks": [], "lock_error": None})
            continue
        dup_note = ""
        if len(matches) > 1:
            n_subs = len({m["subscriptionId"] for m in matches})
            dup_note = (f"same name in {len(matches)} RGs" if n_subs == 1 else
                        f"same name in {len(matches)} RGs, {n_subs} subscriptions")
        for m in matches:
            sub_locks, err = locks.get(m["subscriptionId"], ([], None))
            hits = locks_for(m, sub_locks)
            if err:
                status = "LOCKS_UNKNOWN"
            elif any(h["level"].lower() == "readonly" for h in hits):
                status = "READ_ONLY_LOCK"
            elif hits:
                status = "DELETE_LOCK"
            else:
                status = "NO_LOCK"
            results.append({**item, "status": status, "subscription": m.get("subscription") or m["subscriptionId"],
                            "subscriptionId": m["subscriptionId"], "resourceGroup": m["resourceGroup"],
                            "locks": hits, "lock_error": err, "multiple": len(matches) > 1, "dup_note": dup_note})
    return results


def blocks_change(r):
    """Does a lock stop the change this NSG needs? CanNotDelete blocks deleting rules; ReadOnly blocks everything."""
    if r["status"] == "READ_ONLY_LOCK":
        return True
    if r["status"] == "DELETE_LOCK":
        return r["deleted"] > 0 or (r["updated"] == 0 and r["not_found"] == 0)
    return False


def describe_locks(r):
    if r["lock_error"]:
        return f"Could not read locks: {r['lock_error']}"
    return "; ".join(f"{h['name']} ({h['level']}, on {h['where']})" for h in r["locks"])


def unlock_summary(results):
    """Text for the senior engineer: each resource group that needs a lock lifted."""
    groups = {}
    for r in results:
        if not r["subscriptionId"] or not blocks_change(r):
            continue
        key = (r["subscription"], r["resourceGroup"])
        g = groups.setdefault(key, {"nsgs": [], "locks": {}})
        g["nsgs"].append(r["nsg"])
        if r.get("multiple"):
            g.setdefault("dups", []).append(r["nsg"])
        g.setdefault("sources", set()).update(r.get("sources", []))
        for h in r["locks"]:
            g["locks"][(h["name"], h["level"], h["where"])] = 1
    if not groups:
        return "No resource locks block these changes."
    lines = ["Resource groups with locks that block the NSG rule changes:", ""]
    for (sub, rg), g in sorted(groups.items()):
        lines.append(f"Subscription:   {sub}")
        lines.append(f"Resource group: {rg}")
        for (name, level, where) in g["locks"]:
            lines.append(f"  Lock: {name} ({level}, on {where})")
        lines.append("  NSGs: " + ", ".join(sorted(set(g["nsgs"]))))
        for n in sorted(set(g.get("dups", []))):
            lines.append(f"  Check: {n} has the same name in another resource group. Confirm this is the right one")
        if g.get("sources"):
            lines.append("  From: " + ", ".join(sorted(g["sources"])))
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------- GUI ----------
def open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


COLOUR_RANK = {"bad": 0, "warn": 1, "ok": 2}   # red, amber, green

STATUS_TEXT = {
    "READ_ONLY_LOCK": "Locked (read-only)",
    "DELETE_LOCK": "Locked (delete)",
    "NO_LOCK": "No lock",
    "NOT_FOUND": "NSG not found",
    "LOCKS_UNKNOWN": "Could not read locks",
}


def gui_main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(f"NSG Resource Group Lookup v{VERSION}")
    root.geometry("1280x660")
    root.minsize(860, 480)

    try:
        with open(SETTINGS, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    saved_files = saved.get("files") or ([saved["file"]] if saved.get("file") else [])
    state_files = [f for f in saved_files if os.path.isfile(f)]
    file_var = tk.StringVar()

    def show_files():
        fs = state_files
        if not fs:
            file_var.set("")
        elif len(fs) == 1:
            file_var.set(fs[0])
        else:
            file_var.set(f"{len(fs)} files: " + ", ".join(os.path.basename(f) for f in fs))
    show_files()
    status_var = tk.StringVar(value="Pick one or more reports (Cmd/Ctrl+click to pick several), then click Look up.")
    state = {"results": [], "busy": False}

    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)

    def pick_file():
        start = os.path.dirname(state_files[0]) if state_files else DOCS
        ps = filedialog.askopenfilenames(title="Select one or more reports (Cmd/Ctrl+click to pick several)",
                                         initialdir=start if os.path.isdir(start) else None,
                                         filetypes=[("CSV or Excel", "*.csv *.xlsx"), ("All files", "*.*")])
        if ps:
            state_files[:] = [os.path.normpath(p) for p in root.tk.splitlist(ps)]
            show_files()

    ttk.Label(top, text="Reports").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(top, textvariable=file_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(top, text="Browse...", command=pick_file).grid(row=0, column=2)

    btns = ttk.Frame(top)
    btns.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
    run_btn = ttk.Button(btns, text="Look up")
    run_btn.pack(side="left")
    unlock_btn = ttk.Button(btns, text="Copy unlock list", state="disabled")
    unlock_btn.pack(side="left", padx=8)
    copy_btn = ttk.Button(btns, text="Copy results", state="disabled")
    copy_btn.pack(side="left")
    export_btn = ttk.Button(btns, text="Export CSV", state="disabled")
    export_btn.pack(side="left", padx=8)
    copied_var = tk.StringVar(value="")
    ttk.Label(btns, textvariable=copied_var, foreground="#1a7f37").pack(side="left", padx=4)

    ttk.Label(root, textvariable=status_var, padding=(12, 4)).pack(fill="x")
    mod = "Command" if sys.platform == "darwin" else "Control"
    copy_key = "Cmd+C" if sys.platform == "darwin" else "Ctrl+C"
    ttk.Label(root, foreground="#666666", padding=(12, 0, 12, 4),
              text=("Red = lock blocks the change, amber = check, green = no lock.   Click Resource group to group by colour.   "
                    f"Double-click to copy a cell, right-click for more, {copy_key} copies selected rows.")
              ).pack(fill="x")

    cols = ("nsg", "sub", "rg", "status", "locks", "deleted", "updated", "blocks", "from")
    heads = ("NSG", "Subscription", "Resource group", "Lock status", "Locks", "Rules deleted", "Rules updated",
             "Blocks change?", "From report(s)")
    widths = (330, 200, 260, 140, 380, 95, 95, 110, 260)
    frame = ttk.Frame(root, padding=(12, 0, 12, 12))
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
    for c, h, w in zip(cols, heads, widths):
        tree.heading(c, text=h, command=lambda c=c: sort_by(c))
        tree.column(c, width=w, minwidth=40, anchor="w", stretch=False)  # no stretch: dragged widths stay put
    ysb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ysb.grid(row=0, column=1, sticky="ns")
    xsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree.tag_configure("ok", foreground="#1a7f37")
    tree.tag_configure("warn", foreground="#9a6700")
    tree.tag_configure("bad", foreground="#cf222e")

    # ----- sorting: click a heading to sort, click again to reverse -----
    sort_state = {"col": None, "reverse": False}

    def sort_key(item, c):
        v = tree.set(item, c)
        tag = (tree.item(item, "tags") or ("ok",))[0]
        if c in ("deleted", "updated"):
            return (int(v or 0),)
        if c == "rg":                          # Resource group: red, amber, green, then A to Z
            return (COLOUR_RANK.get(tag, 9), v.lower(), tree.set(item, "nsg").lower())
        if c == "status":                      # Lock status: by colour, then text
            return (COLOUR_RANK.get(tag, 9), v.lower())
        return (v.lower(),)

    def sort_by(c):
        reverse = (not sort_state["reverse"]) if sort_state["col"] == c else False
        items = sorted(tree.get_children(""), key=lambda it: sort_key(it, c), reverse=reverse)
        for i, it in enumerate(items):
            tree.move(it, "", i)
        sort_state.update(col=c, reverse=reverse)
        for col, h in zip(cols, heads):
            tree.heading(col, text=h + ((" \u25bc" if reverse else " \u25b2") if col == c else ""))

    # ----- copy helpers (same behaviour as nsg-lookup) -----
    clear_job = {"id": None}

    def to_clipboard(text, what):
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        copied_var.set(f"Copied {what}")
        if clear_job["id"]:
            root.after_cancel(clear_job["id"])
        clear_job["id"] = root.after(2500, lambda: copied_var.set(""))

    def rows_tsv(items):
        lines = ["\t".join(heads)]
        lines += ["\t".join(str(v) for v in tree.item(it, "values")) for it in items]
        return "\n".join(lines)

    def cell_at(event):
        item, col = tree.identify_row(event.y), tree.identify_column(event.x)
        return (item, int(col[1:]) - 1) if item and col else (None, None)

    def copy_cell(item, idx):
        val = str(tree.item(item, "values")[idx])
        to_clipboard(val, f"{heads[idx]}: {val[:60]}" if val else f"{heads[idx]} (empty)")

    def copy_rows(items):
        if items:
            to_clipboard(rows_tsv(items), f"{len(items)} row(s)")

    def copy_column(idx):
        vals = [str(tree.item(it, "values")[idx]) for it in tree.get_children()]
        to_clipboard("\n".join(vals), f"{heads[idx]} column ({len(vals)} values)")

    def copy_all():
        items = tree.get_children()
        if items:
            to_clipboard(rows_tsv(items), f"all {len(items)} row(s)")

    def copy_unlock():
        to_clipboard(unlock_summary(state["results"]), "unlock list, ready to send")

    def on_double(event):
        if tree.identify_region(event.x, event.y) != "cell":
            return
        item, idx = cell_at(event)
        if item is not None:
            copy_cell(item, idx)
        return "break"

    menu = tk.Menu(root, tearoff=0)

    def on_right(event):
        item, idx = cell_at(event)
        if item is None:
            return
        if item not in tree.selection():
            tree.selection_set(item)
        sel = tree.selection()
        menu.delete(0, "end")
        menu.add_command(label=f"Copy cell ({heads[idx]})", command=lambda: copy_cell(item, idx))
        menu.add_command(label=f"Copy {len(sel)} selected row{'s' if len(sel) > 1 else ''}", command=lambda: copy_rows(sel))
        menu.add_command(label=f"Copy {heads[idx]} column", command=lambda: copy_column(idx))
        menu.add_separator()
        menu.add_command(label="Copy all results", command=copy_all)
        menu.add_command(label="Copy unlock list", command=copy_unlock)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    tree.bind("<Double-1>", on_double)
    tree.bind("<Button-3>", on_right)
    if sys.platform == "darwin":
        tree.bind("<Button-2>", on_right)
        tree.bind("<Control-Button-1>", on_right)
    tree.bind(f"<{mod}-c>", lambda e: copy_rows(tree.selection()))
    tree.bind(f"<{mod}-C>", lambda e: copy_rows(tree.selection()))
    tree.bind(f"<{mod}-a>", lambda e: (tree.selection_set(tree.get_children()), "break")[1])
    copy_btn.configure(command=copy_all)
    unlock_btn.configure(command=copy_unlock)

    def export_csv():
        p = filedialog.asksaveasfilename(title="Export results", defaultextension=".csv",
                                         initialdir=DOCS if os.path.isdir(DOCS) else None,
                                         initialfile="nsg_rg_lookup.csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(heads)
            for it in tree.get_children():
                w.writerow(tree.item(it, "values"))
        copied_var.set(f"Exported to {os.path.basename(p)}")

    export_btn.configure(command=export_csv)

    # ----- run the lookup in the background so the window stays responsive -----
    def autofit():
        """Size each column to its longest value (capped), like nsg-lookup. Drag a divider to change it."""
        import tkinter.font as tkfont
        try:
            body = tkfont.nametofont(ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        except tk.TclError:
            body = tkfont.nametofont("TkDefaultFont")
        head = tkfont.nametofont("TkHeadingFont")
        for i, c in enumerate(cols):
            width = head.measure(heads[i] + " \u25b2") + 24
            for it in tree.get_children():
                width = max(width, body.measure(str(tree.item(it, "values")[i])) + 24)
            tree.column(c, width=min(width, 520))

    def colour(r):
        if r["status"] in ("NOT_FOUND", "LOCKS_UNKNOWN") or r.get("multiple"):
            return "warn"
        if blocks_change(r):
            return "bad"
        if r["status"] in ("DELETE_LOCK", "READ_ONLY_LOCK"):
            return "warn"
        return "ok"

    def show(results):
        tree.delete(*tree.get_children())
        sort_state.update(col=None, reverse=False)
        for c, h in zip(cols, heads):
            tree.heading(c, text=h)
        for r in results:
            blocks = blocks_change(r)
            tag = colour(r)
            status = STATUS_TEXT[r["status"]] + (f" ({r['dup_note']})" if r.get("dup_note") else "")
            tree.insert("", "end", tags=(tag,), values=(
                r["nsg"], r["subscription"], r["resourceGroup"], status, describe_locks(r),
                r["deleted"], r["updated"], "Yes" if blocks else "No", ", ".join(r.get("sources", []))))

        autofit()

        locked_rgs = {(r["subscription"], r["resourceGroup"]) for r in results if blocks_change(r)}
        nf = sum(1 for r in results if r["status"] == "NOT_FOUND")
        unk = sum(1 for r in results if r["status"] == "LOCKS_UNKNOWN")
        n_files = len({s for r in results for s in r.get("sources", [])})
        msg = (f"Done. {len({r['nsg'].lower() for r in results})} unique NSG(s) from {n_files} report(s) checked. "
               f"{len(locked_rgs)} resource group(s) have a lock that blocks the change.")
        if nf:
            msg += f"  {nf} NSG(s) not found."
        if unk:
            msg += f"  Locks could not be read for {unk}."
        dups = len({r["nsg"].lower() for r in results if r.get("multiple")})
        if dups:
            msg += f"  {dups} duplicate NSG name(s), see amber rows."
        status_var.set(msg)
        for b in (copy_btn, export_btn, unlock_btn):
            b.configure(state="normal" if results else "disabled")
        tree.focus_set()

    def finish(results=None, error=None):
        state["busy"] = False
        run_btn.configure(state="normal")
        if error:
            status_var.set("Failed.")
            messagebox.showerror("Lookup failed", error)
            return
        state["results"] = results
        show(results)

    def run():
        if state["busy"]:
            return
        files = [f for f in state_files if os.path.isfile(f)]
        if not files:
            messagebox.showwarning("No file", "Pick one or more report files first.")
            return
        items, problems = read_many(files)
        if problems:
            messagebox.showwarning("Some files were skipped", "\n".join(problems))
        if not items:
            messagebox.showwarning("No NSGs", "No NSG names found in the selected file(s).")
            return
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"files": files}, f)
        except Exception:
            pass

        state["busy"] = True
        run_btn.configure(state="disabled")
        tree.delete(*tree.get_children())
        status_var.set(f"Found {len(items)} unique NSG(s) in {len(files) - len(problems)} file(s). Asking Azure...")

        def progress(msg):
            root.after(0, lambda: status_var.set(msg))

        def work():
            try:
                res = lookup(items, progress)
                root.after(0, lambda: finish(res))
            except AzError as e:
                root.after(0, lambda: finish(error=str(e)))
            except Exception as e:
                root.after(0, lambda: finish(error=f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=run)
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
