#!/usr/bin/env python3
"""
nsg_lookup_gui.py -- desktop window version of nsg_lookup_universal.py.
Run locally (Windows, macOS, or Linux). Requires: az login, python3 with
tkinter (bundled with Python on Windows and macOS). No pip installs needed,
standard library only.

Pure IP lookup: type or paste one or more IPs, click "Look up all", and see
which Subscription, Resource Group, VNet, Subnet, and NSG each one belongs
to. No rule evaluation, no ALLOW/DENY.

Subnet data is loaded from Azure Resource Graph once at startup (with
automatic pagination past the 1,000-row limit) and kept in memory, so every
lookup after that is instant. Click "Refresh" to re-pull it.

Read-only. Every Azure call is a read-only `az graph query`.

USAGE
    python3 nsg_lookup_gui.py
"""

import csv
import ipaddress
import json
import platform
import queue
import re
import subprocess
import threading

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont
except ImportError:
    raise SystemExit(
        "tkinter is not available in this Python install.\n"
        "Windows/macOS: reinstall Python from python.org (tkinter is included).\n"
        "Debian/Ubuntu: sudo apt install python3-tk"
    )

# On Windows, `az` resolves to az.cmd, which subprocess can only find via the
# shell. shell=True with an argument list works on Windows but breaks argument
# handling on macOS/Linux, so this must be conditional.
IS_WINDOWS = platform.system() == "Windows"

QUERY = (
    'Resources '
    '| where type =~ "microsoft.network/virtualnetworks" '
    '| mvexpand subnet = properties.subnets '
    '| extend nsgId = tostring(subnet.properties.networkSecurityGroup.id) '
    "| extend nsgName = split(nsgId, '/')[-1] "
    '| join kind=leftouter ( '
    "    ResourceContainers "
    "    | where type == 'microsoft.resources/subscriptions' "
    "    | project subscriptionId, SubscriptionName = name "
    ') on subscriptionId '
    '| project '
    '    SubscriptionName = iff(isnotempty(SubscriptionName), SubscriptionName, subscriptionId), '
    '    ResourceGroup = resourceGroup, '
    '    VNetName = name, '
    '    SubnetName = tostring(subnet.name), '
    '    AddressPrefix = tostring(subnet.properties.addressPrefix), '
    '    NSG_Name = iff(isnotempty(nsgName), tostring(nsgName), "NO NSG ATTACHED")'
)

COLUMNS = [
    ("n", "#", 40),
    ("ip", "IP", 115),
    ("sub", "Subscription", 120),
    ("rg", "Resource Group", 185),
    ("vnet", "VNet", 115),
    ("subnet", "Subnet", 235),
    ("nsg", "NSG", 195),
]


# --------------------------------------------------------------------------
# Azure / lookup logic (no GUI code in here)
# --------------------------------------------------------------------------

def run_az(args):
    """Runs an az command. Returns (parsed_json_or_None, error_text_or_None)."""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW, no console flash
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                shell=IS_WINDOWS, **kwargs)
    except FileNotFoundError:
        return None, "Azure CLI ('az') was not found. Install it and run 'az login'."
    if result.returncode != 0:
        return None, (result.stderr or "az command failed").strip()
    if not result.stdout.strip():
        return None, None
    return json.loads(result.stdout), None


def load_subnets(max_pages=25):
    """Returns (rows, error_text). Pages through Resource Graph results."""
    # Make sure the resource-graph extension is present (no-op if it is).
    run_az(["az", "extension", "add", "--name", "resource-graph", "-y"])

    all_rows = []
    skip_token = None
    for _ in range(max_pages):
        cmd = ["az", "graph", "query", "-q", QUERY, "-o", "json"]
        if skip_token:
            cmd += ["--skip-token", skip_token]
        result, err = run_az(cmd)
        if err:
            return all_rows, err
        if result is None:
            break
        all_rows.extend(result.get("data", []))
        skip_token = result.get("skip_token") or result.get("skipToken") or result.get("$skipToken")
        if not skip_token:
            break
    return all_rows, None


def find(ip, rows):
    target = ipaddress.ip_address(ip)
    for r in rows:
        pfx = r.get("AddressPrefix")
        if not pfx:
            continue
        try:
            if target in ipaddress.ip_network(pfx, strict=False):
                return r
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.rows = []              # subnet rows from Azure
        self.ip_rows = []           # list of {"frame", "entry", "var"}
        self.inbox = queue.Queue()  # results from background threads
        self.loading = False
        self.full = {}              # result row id -> full (untruncated) values
        self.order = []             # result row ids in original order (incl. filtered-out)
        self._fit_job = None
        self._tip = None
        self._tip_job = None
        self._tip_key = None

        root.title("NSG Subnet Lookup")
        root.geometry("1100x620")
        root.minsize(700, 480)

        # Fonts used to measure text so columns can be sized to their content.
        style = ttk.Style()
        try:
            self.font = tkfont.nametofont(style.lookup("Treeview", "font") or "TkDefaultFont")
        except tk.TclError:
            self.font = tkfont.nametofont("TkDefaultFont")
        try:
            self.hfont = tkfont.nametofont("TkHeadingFont")
        except tk.TclError:
            self.hfont = self.font

        self._build_ui()
        self.add_ip_row(focus=True)
        self.refresh_data()
        self.root.after(100, self._poll_inbox)

    # ---- layout ----------------------------------------------------------

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        self.ip_count_var = tk.StringVar(value="IP addresses")
        ttk.Label(outer, textvariable=self.ip_count_var,
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        # Scrollable area holding the IP input rows.
        holder = ttk.Frame(outer)
        holder.pack(fill="x", pady=(4, 6))
        self.holder = holder
        self.canvas = tk.Canvas(holder, height=130, highlightthickness=0, yscrollincrement=12)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")
        self.ip_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.ip_frame, anchor="nw")
        self.ip_frame.bind("<Configure>",
                           lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        # Mouse wheel / trackpad scrolling for the IP input area. Bound on the
        # whole window and filtered by pointer position, because the wheel
        # event goes to whichever widget is under the mouse (e.g. an Entry),
        # not to the canvas.
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")   # Linux up
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")   # Linux down

        # Action buttons.
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="+ Add IP", command=lambda: self.add_ip_row(focus=True)).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left", padx=6)
        self.refresh_btn = ttk.Button(bar, text="Refresh subnets", command=self.refresh_data)
        self.refresh_btn.pack(side="left")
        self.lookup_btn = ttk.Button(bar, text="Look up all", command=self.lookup_all, state="disabled")
        self.lookup_btn.pack(side="right")

        # Results header with a live filter box on the right.
        res_head = ttk.Frame(outer)
        res_head.pack(fill="x")
        ttk.Label(res_head, text="Results",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_count = tk.StringVar(value="")
        ttk.Button(res_head, text="x", width=3,
                   command=self.clear_filter).pack(side="right")
        self.filter_entry = ttk.Entry(res_head, textvariable=self.filter_var, width=28)
        self.filter_entry.pack(side="right", padx=4)
        ttk.Label(res_head, text="Filter:").pack(side="right")
        ttk.Label(res_head, textvariable=self.filter_count).pack(side="right", padx=(0, 12))
        self.filter_var.trace_add("write", lambda *a: self._apply_filter())
        self.filter_entry.bind("<Escape>", lambda e: self.clear_filter())
        self._bind_shortcuts()

        # Results table.
        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True, pady=(4, 8))
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            if key == "n":
                self.tree.column(key, width=width, minwidth=width, stretch=False, anchor="center")
            else:
                self.tree.column(key, width=width, anchor="w")
        vs = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        # Colors chosen to stay readable on both light and dark system themes.
        self.tree.tag_configure("notfound", foreground="#f04438")
        self.tree.tag_configure("nonsg", foreground="#f79009")
        self.tree.bind("<Control-c>", lambda e: self.copy_results(selected_only=True))
        self.tree.bind("<Command-c>", lambda e: self.copy_results(selected_only=True))
        # Re-fit columns whenever the table is resized; tooltip on cut-off values.
        self.tree.bind("<Configure>", self._schedule_fit)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tip())
        self.tree.bind("<ButtonPress>", lambda e: self._hide_tip(), add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")
        # Copy a single value: double-click it, or right-click for a menu.
        self.tree.bind("<Double-Button-1>", self._on_cell_double_click)
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.tree.bind(seq, self._on_cell_menu)

        # Footer.
        foot = ttk.Frame(outer)
        foot.pack(fill="x")
        ttk.Button(foot, text="Copy results", command=self.copy_results).pack(side="left")
        ttk.Button(foot, text="Export CSV", command=self.export_csv).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Starting...")
        ttk.Label(foot, textvariable=self.status).pack(side="right")

    # ---- IP input rows ---------------------------------------------------

    def add_ip_row(self, focus=False, text=""):
        frame = ttk.Frame(self.ip_frame)
        frame.pack(fill="x", pady=2)
        num = ttk.Label(frame, width=4, anchor="e")
        num.pack(side="left", padx=(0, 6))
        var = tk.StringVar(value=text)
        entry = ttk.Entry(frame, textvariable=var, width=34)
        entry.pack(side="left")
        row = {"frame": frame, "entry": entry, "var": var, "num": num}
        ttk.Button(frame, text="x", width=3,
                   command=lambda r=row: self.remove_ip_row(r)).pack(side="left", padx=4)
        entry.bind("<Return>", lambda e, r=row: self._on_enter(r))
        entry.bind("<<Paste>>", lambda e, r=row: self._on_paste(r))
        var.trace_add("write", lambda *a: self._update_count())
        self.ip_rows.append(row)
        self._renumber()
        if focus:
            entry.focus_set()
            self.root.after(50, lambda: self._sync_scroll(to="bottom"))
        return row

    def remove_ip_row(self, row):
        if len(self.ip_rows) == 1:
            row["var"].set("")      # keep at least one row, just clear it
            return
        self.ip_rows.remove(row)
        row["frame"].destroy()
        self._renumber()
        self.root.after_idle(self._sync_scroll)

    def _sync_scroll(self, to=None):
        """
        Recalculates the scrollable area and keeps the view inside it. Without
        this, shrinking the list (Clear all, removing rows) can leave the view
        scrolled past the end, so the remaining rows are out of sight.
        to: "top" / "bottom" to jump there, or None to just stay in range.
        """
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        content_h = self.ip_frame.winfo_reqheight()
        if to == "top" or content_h <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)
        elif to == "bottom":
            self.canvas.yview_moveto(1.0)
        else:
            self.canvas.yview_moveto(min(self.canvas.yview()[0], 1.0))

    def clear_all(self):
        for row in self.ip_rows[1:]:
            row["frame"].destroy()
        self.ip_rows = self.ip_rows[:1]
        self.ip_rows[0]["var"].set("")
        self.ip_rows[0]["entry"].focus_set()
        self._clear_results()
        self.filter_var.set("")
        self._renumber()
        self.root.after_idle(lambda: self._sync_scroll(to="top"))

    def _renumber(self):
        """Keeps the 1., 2., 3. labels beside the input boxes in order."""
        for i, row in enumerate(self.ip_rows, start=1):
            row["num"].config(text=f"{i}.")
        self._update_count()

    def _update_count(self):
        filled = sum(1 for r in self.ip_rows if r["var"].get().strip())
        self.ip_count_var.set(f"IP addresses ({filled})" if filled else "IP addresses")

    def _over_ip_area(self, x_root, y_root):
        """True if the pointer is over the IP input area (or any widget inside it)."""
        w = self.root.winfo_containing(x_root, y_root)
        while w is not None:
            if w is self.holder:
                return True
            w = w.master
        return False

    def _on_mousewheel(self, event):
        if not self._over_ip_area(event.x_root, event.y_root):
            return None             # let the results table etc. handle it normally
        if event.num == 4:
            units = -3
        elif event.num == 5:
            units = 3
        else:
            d = event.delta
            if d == 0:
                return None
            # Windows reports multiples of 120; macOS reports small numbers.
            step = (abs(d) // 120) * 3 if abs(d) >= 120 else max(1, abs(d)) * 2
            units = -step if d > 0 else step
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _on_enter(self, row):
        idx = self.ip_rows.index(row)
        if idx == len(self.ip_rows) - 1:
            if row["var"].get().strip():
                self.add_ip_row(focus=True)
        else:
            self.ip_rows[idx + 1]["entry"].focus_set()
        return "break"

    def _on_paste(self, row):
        """Pasting several IPs (newline/comma/space separated) splits them into rows."""
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            return None
        parts = [p for p in re.split(r"[\s,;]+", clip.strip()) if p]
        if len(parts) <= 1:
            return None             # normal single paste, let tkinter handle it
        idx = self.ip_rows.index(row)
        row["var"].set(parts[0])
        for offset, part in enumerate(parts[1:], start=1):
            target = idx + offset
            if target < len(self.ip_rows) and not self.ip_rows[target]["var"].get().strip():
                self.ip_rows[target]["var"].set(part)
            else:
                self.add_ip_row(text=part)
        return "break"

    # ---- loading subnet data --------------------------------------------

    def refresh_data(self):
        if self.loading:
            return
        self.loading = True
        self.lookup_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self.status.set("Loading subnets from Azure...")

        def work():
            rows, err = load_subnets()
            self.inbox.put(("loaded", rows, err))

        threading.Thread(target=work, daemon=True).start()

    def _poll_inbox(self):
        try:
            while True:
                msg = self.inbox.get_nowait()
                if msg[0] == "loaded":
                    self._on_loaded(msg[1], msg[2])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_inbox)

    def _on_loaded(self, rows, err):
        self.loading = False
        self.refresh_btn.config(state="normal")
        if err:
            self.status.set("Could not load subnets")
            hint = ""
            low = err.lower()
            if "az login" in low or "please run" in low or "authenticat" in low:
                hint = "\n\nYour Azure login may have expired. Run 'az login' in a terminal, then click Refresh subnets."
            messagebox.showerror("Azure error", err[:1200] + hint)
            if rows:
                self.rows = rows
                self.lookup_btn.config(state="normal")
            return
        self.rows = rows
        self.status.set(f"{len(rows)} subnets loaded")
        self.lookup_btn.config(state="normal")

    # ---- lookup ---------------------------------------------------------

    def lookup_all(self):
        ips = []
        for row in self.ip_rows:
            val = row["var"].get().strip()
            if val and val not in ips:
                ips.append(val)
        if not ips:
            self.status.set("Enter at least one IP")
            return

        self._clear_results()
        found = 0
        for n, ip in enumerate(ips, start=1):
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                self._add_result((n, ip, "Invalid IP", "", "", "", ""), ("notfound",))
                continue
            hit = find(ip, self.rows)
            if not hit:
                self._add_result((n, ip, "Not found", "No known subnet", "", "", ""),
                                 ("notfound",))
                continue
            found += 1
            tags = ("nonsg",) if hit["NSG_Name"] == "NO NSG ATTACHED" else ()
            self._add_result((
                n, ip, hit["SubscriptionName"], hit["ResourceGroup"], hit["VNetName"],
                f"{hit['SubnetName']} ({hit['AddressPrefix']})", hit["NSG_Name"]), tags)
        self._apply_filter()        # keep any active filter applied to the new results
        self._fit_columns()
        self.status.set(f"{found} of {len(ips)} found  |  {len(self.rows)} subnets loaded")

    def _add_result(self, values, tags):
        """Inserts a result row and remembers its full text (display text may get shortened)."""
        iid = self.tree.insert("", "end", values=values, tags=tags)
        self.full[iid] = tuple(str(v) for v in values)
        self.order.append(iid)

    def _clear_results(self):
        # Delete every row, including ones currently hidden by the filter.
        if self.order:
            self.tree.delete(*self.order)
        self.order.clear()
        self.full.clear()
        self.filter_count.set("")

    # ---- filter ---------------------------------------------------------

    def _apply_filter(self):
        """
        Shows only rows that contain every word typed in the filter box
        (case-insensitive, matched against all columns except '#'). Rows keep
        their original order and numbering. Hidden rows are only detached, not
        deleted, so clearing the filter brings them back.
        """
        terms = self.filter_var.get().lower().split()
        self._hide_tip()
        self.tree.detach(*self.tree.get_children())
        shown = 0
        for iid in self.order:
            hay = " ".join(self.full[iid][1:]).lower()
            if all(t in hay for t in terms):
                self.tree.reattach(iid, "", "end")
                shown += 1
        if terms and self.order:
            self.filter_count.set(f"{shown} of {len(self.order)} shown")
        else:
            self.filter_count.set("")

    # ---- keyboard shortcuts ---------------------------------------------

    def _bind_shortcuts(self):
        """
        Filter:      Cmd+F (macOS)  /  Ctrl+F (Windows, Linux)
        Full screen: Cmd+Ctrl+F (macOS)  /  F11 (Windows, Linux)

        Tk matches <Command-f> even when Ctrl is also held, so Cmd+Ctrl+F
        would wrongly open the filter. The handler therefore steps aside when
        Ctrl is down, and Cmd+Ctrl+F gets its own, more specific binding.
        """
        mac = platform.system() == "Darwin"

        def find(event):
            if mac and event.state & 0x4:       # Ctrl held: that's the fullscreen shortcut
                return None
            self._focus_filter()
            return "break"

        def full(event):
            self._toggle_fullscreen()
            return "break"

        self._find_handler, self._full_handler = find, full
        if mac:
            pairs = [("<Command-f>", find), ("<Command-F>", find),
                     ("<Command-Control-f>", full), ("<Command-Control-F>", full)]
        else:
            pairs = [("<Control-f>", find), ("<Control-F>", find), ("<F11>", full)]
        for seq, fn in pairs:
            try:
                self.root.bind_all(seq, fn)
            except tk.TclError:
                pass

    def _toggle_fullscreen(self):
        try:
            self.root.attributes("-fullscreen", not bool(self.root.attributes("-fullscreen")))
        except tk.TclError:
            pass

    def clear_filter(self):
        self.filter_var.set("")
        self.filter_entry.focus_set()

    def _focus_filter(self):
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, "end")

    # ---- column auto-fit ------------------------------------------------

    CELL_PAD = 22   # breathing room around text inside a cell, in pixels

    def _schedule_fit(self, event=None):
        if self._fit_job is not None:
            self.root.after_cancel(self._fit_job)
        self._fit_job = self.root.after(60, self._fit_columns)

    def _ellipsize(self, text, px):
        """Shortens text with '...' so it fits in px pixels."""
        if px <= 0 or self.font.measure(text) <= px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:                      # longest prefix that fits with '...'
            mid = (lo + hi + 1) // 2
            if self.font.measure(text[:mid] + "...") <= px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "..." if lo else "..."

    def _fit_columns(self):
        """
        Sizes each column to its content. Short columns get exactly what they
        need. If everything together is wider than the window, only the widest
        columns are squeezed (and shown with '...'), so there is no sideways
        scrolling. If there is spare room, it is shared out proportionally.
        """
        self._fit_job = None
        avail = self.tree.winfo_width() - 4
        if avail < 150:
            return                          # window not drawn yet

        pad = self.CELL_PAD
        keys = [c[0] for c in COLUMNS]
        rows = list(self.full.values())

        nat, floor = [], []
        for i, (key, title, default) in enumerate(COLUMNS):
            head_w = self.hfont.measure(title) + pad
            floor_w = max(head_w, 36 if key == "n" else 60)   # never cut the heading
            if rows:
                cell_w = max(self.font.measure(r[i]) for r in rows) + pad
            else:
                cell_w = default
            nat.append(max(floor_w, cell_w))
            floor.append(floor_w)

        widths = list(nat)
        # '#' and IP always keep their full width; only the text columns flex.
        flex = [i for i, k in enumerate(keys) if k not in ("n", "ip")]
        flex_avail = avail - sum(nat[i] for i in range(len(keys)) if i not in flex)
        total_nat = sum(nat[i] for i in flex)

        if total_nat <= flex_avail:
            extra = flex_avail - total_nat
            for i in flex:
                widths[i] = nat[i] + int(extra * nat[i] / total_nat)
        else:
            def used(cap):
                return sum(max(floor[i], min(nat[i], cap)) for i in flex)
            if used(0) > flex_avail:
                for i in flex:               # not even headings fit: allow sideways scroll
                    widths[i] = floor[i]
            else:
                lo, hi = 0, max(nat[i] for i in flex)
                while lo < hi:               # largest cap that still fits the window
                    mid = (lo + hi + 1) // 2
                    if used(mid) <= flex_avail:
                        lo = mid
                    else:
                        hi = mid - 1
                for i in flex:
                    widths[i] = max(floor[i], min(nat[i], lo))

        for i, key in enumerate(keys):
            self.tree.column(key, width=widths[i], minwidth=floor[i], stretch=False)

        self._render_text()

    def _render_text(self):
        """
        Re-draws the visible text, shortened to fit each column's CURRENT width.
        Reads the real widths from the table, so it also works after the user
        drags a column divider.
        """
        keys = [c[0] for c in COLUMNS]
        widths = [self.tree.column(k, "width") for k in keys]
        self._rendered_widths = widths
        for iid, vals in self.full.items():
            shown = [vals[i] if keys[i] == "n"
                     else self._ellipsize(vals[i], widths[i] - self.CELL_PAD)
                     for i in range(len(keys))]
            self.tree.item(iid, values=shown)

    def _on_tree_release(self, event):
        """After dragging a column divider, re-fit the text to the new widths."""
        widths = [self.tree.column(c[0], "width") for c in COLUMNS]
        if widths != getattr(self, "_rendered_widths", None):
            self._render_text()

    # ---- tooltip for shortened values -----------------------------------

    def _hide_tip(self):
        if self._tip_job is not None:
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        self._tip_key = None

    def _on_tree_motion(self, event):
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        key = (iid, col)
        if key == self._tip_key:
            return
        self._hide_tip()
        self._tip_key = key
        if not iid or iid not in self.full or not col.startswith("#"):
            return
        # "#3" means the 3rd column ON SCREEN, which differs from the data order
        # if the user has dragged columns around, so ask the table for the real id.
        try:
            col_id = self.tree.column(col, "id")
            ci = [c[0] for c in COLUMNS].index(col_id)
        except (tk.TclError, ValueError):
            return
        if col_id == "n":
            return
        full = self.full[iid][ci]
        if not full:
            return
        self._tip_job = self.root.after(
            350, lambda: self._show_tip(full, event.x_root, event.y_root))

    def _show_tip(self, text, x, y):
        self._tip_job = None
        if self._tip is not None:
            self._tip.destroy()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x + 14}+{y + 18}")
        tk.Label(tip, text=text, background="#2b2b2b", foreground="#f2f2f2",
                 relief="solid", borderwidth=1, padx=6, pady=3).pack()
        self._tip = tip

    # ---- copy / export --------------------------------------------------

    def _cell_at(self, event):
        """Return (row id, column index, column title) under the mouse, or None."""
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not iid or iid not in self.full or not col.startswith("#"):
            return None
        try:
            col_id = self.tree.column(col, "id")
            ci = [c[0] for c in COLUMNS].index(col_id)
        except (tk.TclError, ValueError):
            return None
        return iid, ci, COLUMNS[ci][1]

    def _copy_text(self, text, label):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set(f"Copied {label}")

    def _on_cell_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        cell = self._cell_at(event)
        if cell and cell[2] != "#":
            iid, ci, title = cell
            self._copy_text(str(self.full[iid][ci]), f"{title}: {self.full[iid][ci]}")
        return "break"

    def _on_cell_menu(self, event):
        cell = self._cell_at(event)
        if not cell:
            return None
        iid, ci, title = cell
        self.tree.selection_set(iid)
        value = str(self.full[iid][ci])
        menu = tk.Menu(self.root, tearoff=0)
        if title != "#":
            menu.add_command(label=f"Copy {title}",
                             command=lambda: self._copy_text(value, f"{title}: {value}"))
        menu.add_command(label="Copy row",
                         command=lambda: self._copy_text(
                             "\t".join(str(v) for v in self.full[iid]), "row"))
        if title != "#":
            menu.add_command(label=f"Copy all in {title} column",
                             command=lambda: self._copy_text(
                                 "\n".join(str(self.full[i][ci]) for i in self.tree.get_children()
                                           if i in self.full), f"{title} column"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _result_lines(self, selected_only=False):
        items = self.tree.selection() if selected_only and self.tree.selection() \
            else self.tree.get_children()
        # Full text, never the shortened on-screen version.
        return [self.full[i] for i in items if i in self.full]

    def copy_results(self, selected_only=False):
        data = self._result_lines(selected_only)
        if not data:
            return "break"
        header = "\t".join(c[1] for c in COLUMNS)
        text = header + "\n" + "\n".join("\t".join(str(v) for v in row) for row in data)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set(f"Copied {len(data)} row(s) to clipboard")
        return "break"

    def export_csv(self):
        data = self._result_lines()
        if not data:
            messagebox.showinfo("Nothing to export", "Run a lookup first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV files", "*.csv")],
                                            initialfile="nsg_lookup_results.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in COLUMNS])
            w.writerows(data)
        self.status.set(f"Exported {len(data)} row(s) to {path}")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
