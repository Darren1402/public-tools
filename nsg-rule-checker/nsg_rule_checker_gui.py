#!/usr/bin/env python3
"""
NSG Rule Checker v1.1.1

Checks an NSG rules sheet or a route table sheet (.xlsx tracker or the
Terraform .csv) for the mistakes that break a Terraform deploy, and saves
a fixed copy.

  1. Pick the .xlsx or .csv file (and the sheet, for .xlsx).
  2. Click Check. Each problem cell is listed with a suggested fix.
       - Auto   : safe fix (stray spaces, "," instead of ";", wrong case ...)
       - Check  : best guess that needs your eyes, e.g. Excel turned
                  80,443,8443 into the number 804,438,443
       - Manual : can't be fixed automatically (bad IP, duplicate priority ...)
       - Note   : worth a look, nothing is changed
  3. Click Save fixed copy. Fixes are written to <name>_fixed.<ext>
     next to the original. The original is never changed.

Run:  python3 nsg_rule_checker_gui.py   (Windows: python nsg_rule_checker_gui.py)
Standard library only. The GUI needs tkinter.
"""

import codecs
import csv
import io
import ipaddress
import json
import os
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

VERSION = "1.1.1"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".nsg_rule_checker_gui.json")

# ---------- reference data ----------

COMMON_PORTS = {
    20, 21, 22, 23, 25, 53, 67, 68, 69, 80, 88, 110, 111, 119, 123, 135, 137, 138,
    139, 143, 161, 162, 179, 389, 443, 445, 464, 465, 500, 514, 515, 587, 593, 636,
    873, 989, 990, 993, 995, 1080, 1194, 1433, 1434, 1514, 1515, 1521, 1701, 1723,
    1801, 1812, 1813, 2049, 2179, 2375, 2376, 2379, 2380, 3000, 3128, 3268, 3269,
    3306, 3389, 3478, 4022, 4369, 4443, 4500, 5000, 5022, 5044, 5060, 5061, 5432,
    5601, 5671, 5672, 5900, 5985, 5986, 6379, 6380, 6443, 7000, 7001, 8000, 8008,
    8009, 8080, 8081, 8082, 8088, 8090, 8140, 8200, 8443, 8444, 8530, 8531, 8834,
    8880, 8888, 9000, 9001, 9090, 9091, 9092, 9100, 9200, 9300, 9389, 9443, 9997,
    9998, 10000, 10050, 10051, 10443, 11211, 15672, 27017,
}

SERVICE_TAGS = [
    "Internet", "VirtualNetwork", "AzureLoadBalancer", "Storage", "Sql", "SqlManagement",
    "AzureMonitor", "GuestAndHybridManagement", "AzureCloud", "AzureActiveDirectory",
    "AzureKeyVault", "AzureBackup", "AzureTrafficManager", "EventHub", "ServiceBus",
    "AppService", "AppServiceManagement", "ApiManagement", "AzureContainerRegistry",
    "AzureCosmosDB", "AzureFrontDoor", "GatewayManager", "AzureBastion",
    "AzureSiteRecovery", "AzureUpdateDelivery", "AzureArcInfrastructure",
    "AzureResourceManager", "AzurePortal", "MicrosoftContainerRegistry",
    "AzureDatabricks", "AzureMachineLearning", "AzureConnectors", "AzureDevOps",
    "AzureIoTHub", "BatchNodeManagement", "DataFactory", "HDInsight", "LogicApps",
    "PowerBI", "PowerPlatformInfra", "WindowsVirtualDesktop", "AzureDevSpaces",
    "ActionGroup", "ApplicationInsightsAvailability", "AzureCognitiveSearch",
    "CognitiveServicesManagement", "AzureSignalR", "AzureOpenDatagen", "M365ManagementActivityApi",
]
TAG_LOWER = {t.lower(): t for t in SERVICE_TAGS}

DIRECTIONS = {"inbound": "Inbound", "outbound": "Outbound"}
ACCESS = {"allow": "Allow", "deny": "Deny"}
PROTOCOLS = {"tcp": "Tcp", "udp": "Udp", "icmp": "Icmp", "esp": "Esp", "ah": "Ah",
             "*": "*", "any": "*"}

NEXT_HOP_TYPES = {"virtualappliance": "VirtualAppliance",
                  "virtualnetworkgateway": "VirtualNetworkGateway",
                  "vnetlocal": "VnetLocal", "internet": "Internet", "none": "None"}
NAME_RE = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,78}[A-Za-z0-9_])?"

AUTO, CHECK, MANUAL, NOTE = "Auto", "Check", "Manual", "Note"
EDITED, SKIPPED = "Edited", "Skipped"
RANK = {NOTE: 0, AUTO: 1, CHECK: 2, MANUAL: 3}


class CheckerError(Exception):
    pass


# ---------- data model ----------

class Cell:
    __slots__ = ("text", "is_number", "thousands", "formula", "ref")

    def __init__(self, text, is_number=False, thousands=False, formula=False, ref=None):
        self.text = text
        self.is_number = is_number
        self.thousands = thousands
        self.formula = formula
        self.ref = ref


class Sheet:
    """One table loaded from a .csv or one sheet of a .xlsx."""

    def __init__(self):
        self.kind = ""            # "csv" or "xlsx"
        self.path = ""
        self.sheet_name = ""
        self.sheet_zip_path = ""
        self.rows = []            # list of (rownum, {colidx: Cell})
        self.header_row = None
        self.headers = {}         # colidx -> header text
        self.roles = {}           # colidx -> role
        self.csv_records = []
        self.csv_raws = []
        self.csv_encoding = "utf-8"
        self.mode = "nsg"         # "nsg" or "rt"


class Finding:
    def __init__(self, rownum, colidx, col_label, rule, issues, current, fixed, status,
                 ref=None, row_level=False):
        self.rownum = rownum
        self.colidx = colidx
        self.col_label = col_label
        self.rule = rule
        self.issues = issues
        self.current = current
        self.fixed = fixed
        self.status = status
        self.suggested = fixed
        self.orig_status = status
        self.ref = ref
        self.row_level = row_level
        self.group = ""
        self.raw = current

    @property
    def issue_text(self):
        return "; ".join(self.issues)


class Acc:
    """Collects issues for one cell and remembers the worst status."""

    def __init__(self):
        self.issues = []
        self.status = None

    def add(self, text, status):
        if text not in self.issues:
            self.issues.append(text)
        if self.status is None or RANK[status] > RANK[self.status]:
            self.status = status


# ---------- helpers ----------

def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def visible(s):
    """Make hidden characters visible for display."""
    if s is None:
        return ""
    s = (s.replace("\r\n", "⏎").replace("\n", "⏎").replace("\r", "⏎")
         .replace("\t", "⇥").replace(" ", "⍽")
         .replace("​", "[ZW]").replace("﻿", "[ZW]"))
    lead = len(s) - len(s.lstrip(" "))
    trail = len(s) - len(s.rstrip(" "))
    if lead or trail:
        core = s.strip(" ")
        s = "␣" * lead + core + "␣" * trail
    return s


def classify_header(h):
    k = re.sub(r"[^a-z]", "", (h or "").lower())
    if not k:
        return "other"
    if "port" in k:
        return "port"
    if k.startswith("source") or k.startswith("src") or k.startswith("dest") \
            or k.startswith("dst") or "addressprefix" in k:
        return "addr"
    if k.startswith("prior"):
        return "priority"
    if k.startswith("direction"):
        return "direction"
    if k.startswith("acces"):
        return "access"
    if k.startswith("protoc"):
        return "protocol"
    if k in ("name", "rulename", "nsgrulename", "securityrulename"):
        return "name"
    if k in ("nsg", "nsgname", "networksecuritygroup", "networksecuritygroupname"):
        return "nsg"
    return "other"


# ---------- reading .xlsx (standard library only) ----------

NS_M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "{%s}" % NS_M
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
THOUSANDS_BUILTIN = {3, 4, 37, 38, 39, 40}


def _xlsx_open(path):
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head == OLE_MAGIC:
        raise CheckerError(
            "This Excel file can't be read directly. It is either encrypted "
            "(for example by a sensitivity label or password) or an old .xls file.\n\n"
            "Save a copy as CSV (File > Save As > CSV UTF-8) and check that instead.")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise CheckerError("This is not a valid .xlsx file.")


def xlsx_sheet_list(path):
    with _xlsx_open(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        relmap = {r.get("Id"): r.get("Target") for r in rels}
        out = []
        for s in wb.iter(M + "sheet"):
            target = relmap.get(s.get("{%s}id" % NS_R), "")
            if target.startswith("/"):
                p = target[1:]
            else:
                p = posixpath.normpath(posixpath.join("xl", target))
            out.append((s.get("name"), p))
        return out


def _fmt_has_thousands(fid, custom):
    if fid in THOUSANDS_BUILTIN:
        return True
    code = custom.get(fid)
    if not code:
        return False
    code = re.sub(r'"[^"]*"', "", code)
    code = re.sub(r"\\.", "", code)
    code = code.split(";")[0]
    return bool(re.search(r"[#0],[#0]", code))


def _shared_text(si):
    parts = []
    for child in si:
        if child.tag == M + "t":
            parts.append(child.text or "")
        elif child.tag == M + "r":
            t = child.find(M + "t")
            if t is not None:
                parts.append(t.text or "")
    return "".join(parts)


def _num_text(v):
    try:
        f = float(v)
    except ValueError:
        return v
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return v


def read_xlsx(path, sheet_name):
    sheets = xlsx_sheet_list(path)
    match = [p for n, p in sheets if n == sheet_name]
    if not match:
        raise CheckerError("Sheet '%s' not found." % sheet_name)
    sheet_path = match[0]
    with _xlsx_open(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_shared_text(si) for si in sroot.findall(M + "si")]
        thousands_styles = set()
        if "xl/styles.xml" in names:
            st = ET.fromstring(z.read("xl/styles.xml"))
            custom = {}
            nf = st.find(M + "numFmts")
            if nf is not None:
                for n in nf.findall(M + "numFmt"):
                    custom[int(n.get("numFmtId"))] = n.get("formatCode", "")
            xfs = st.find(M + "cellXfs")
            if xfs is not None:
                for i, xf in enumerate(xfs.findall(M + "xf")):
                    if _fmt_has_thousands(int(xf.get("numFmtId", "0")), custom):
                        thousands_styles.add(i)
        root = ET.fromstring(z.read(sheet_path))

    rows = {}
    last_r = 0
    for r in root.iter(M + "row"):
        rn = int(r.get("r")) if r.get("r") else last_r + 1
        last_r = rn
        last_c = -1
        for c in r.findall(M + "c"):
            ref = c.get("r")
            if ref:
                m = re.match(r"([A-Z]+)(\d+)$", ref)
                ci = col_index(m.group(1))
            else:
                ci = last_c + 1
                ref = col_letter(ci) + str(rn)
            last_c = ci
            t = c.get("t", "n")
            s = int(c.get("s", "0"))
            formula = c.find(M + "f") is not None
            v = c.find(M + "v")
            vt = v.text if v is not None else None
            is_num = thousands = False
            if t == "s":
                text = shared[int(vt)] if vt is not None else ""
            elif t == "inlineStr":
                is_ = c.find(M + "is")
                text = _shared_text(is_) if is_ is not None else ""
            elif t in ("str", "e"):
                text = vt or ""
            elif t == "b":
                text = "TRUE" if vt == "1" else "FALSE"
            else:
                if vt is None:
                    continue
                text = _num_text(vt)
                is_num = True
                thousands = s in thousands_styles
            rows.setdefault(rn, {})[ci] = Cell(text, is_num, thousands, formula, ref)

    sh = Sheet()
    sh.kind = "xlsx"
    sh.path = path
    sh.sheet_name = sheet_name
    sh.sheet_zip_path = sheet_path
    sh.rows = sorted(rows.items())
    return sh


# ---------- reading .csv ----------

def read_csv(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(codecs.BOM_UTF8)
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8-sig" if bom else "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
        enc = "cp1252"
    consumed = []

    def lines():
        for ln in io.StringIO(text, newline=""):
            consumed.append(ln)
            yield ln

    records, raws = [], []
    for rec in csv.reader(lines()):
        records.append(rec)
        raws.append("".join(consumed))
        consumed.clear()
    sh = Sheet()
    sh.kind = "csv"
    sh.path = path
    sh.csv_records = records
    sh.csv_raws = raws
    sh.csv_encoding = enc
    sh.rows = [(i + 1, {ci: Cell(v) for ci, v in enumerate(rec)}) for i, rec in enumerate(records)]
    return sh


def classify_rt_header(h):
    k = re.sub(r"[^a-z]", "", (h or "").lower())
    if not k:
        return "other"
    if "nexthop" in k or ("next" in k and ("hop" in k or "type" in k)):
        return "nexthoptype" if "type" in k else "nexthopip"
    if "routetable" in k or k in ("rt", "rtname", "udr", "udrname"):
        return "rtname"
    if "prefix" in k or k in ("address", "destination", "dest", "cidr"):
        return "prefix"
    if k in ("name", "routename"):
        return "name"
    return "other"


def find_header(sh):
    for rn, cells in sh.rows[:15]:
        roles = {ci: classify_header(c.text) for ci, c in cells.items()}
        vals = set(roles.values())
        if "port" in vals and ("addr" in vals or "name" in vals):
            sh.mode = "nsg"
        else:
            roles = {ci: classify_rt_header(c.text) for ci, c in cells.items()}
            vals = set(roles.values())
            if "nexthoptype" not in vals:
                continue
            sh.mode = "rt"
        sh.header_row = rn
        sh.headers = {ci: c.text.strip() for ci, c in cells.items()}
        sh.roles = roles
        return
    raise CheckerError(
        "Couldn't find the header row in the first 15 rows.\n\n"
        "NSG sheets need columns such as name, port, source, dest.\n"
        "Route table sheets need a next hop type column.")


def load(path, sheet_name=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        sh = read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        sh = read_xlsx(path, sheet_name)
    else:
        raise CheckerError("Pick a .xlsx or .csv file.")
    find_header(sh)
    return sh


# ---------- checks ----------

def clean_ws(s, acc):
    t = s
    if re.search("[   ]", t):
        acc.add("Hidden non-breaking space", AUTO)
        t = re.sub("[   ]", " ", t)
    if re.search("[​‌‍﻿]", t):
        acc.add("Hidden zero-width character", AUTO)
        t = re.sub("[​‌‍﻿]", "", t)
    if "\n" in t.strip() or "\r" in t.strip():
        acc.add("Line break inside the cell", AUTO)
    if "\t" in t.strip():
        acc.add("Tab inside the cell", AUTO)
    if t[:1].isspace():
        acc.add("Space in front", AUTO)
    if t[-1:].isspace():
        acc.add("Space at the end", AUTO)
    return t.strip()


def split_list(t, acc):
    if "；" in t or "，" in t:
        acc.add("Full-width ; or , (Chinese keyboard)", AUTO)
        t = t.replace("；", ";").replace("，", ",")
    if "," in t:
        acc.add('Comma used instead of ";"', AUTO)
    t2 = re.sub(r"\s*([;,])\s*", r"\1", t)
    if t2 != t:
        acc.add('Space next to ";"', AUTO)
    if re.search(r"\s", t2):
        acc.add('Space or line break used instead of ";"', AUTO)
    parts = re.split(r"[;,\s]+", t2)
    if any(p == "" for p in parts):
        acc.add('Extra ";" (empty entry)', AUTO)
        parts = [p for p in parts if p]
    return parts


def dedupe(parts, acc):
    seen, out = set(), []
    for p in parts:
        k = p.lower()
        if k in seen:
            acc.add("Duplicate entry %s removed" % p, AUTO)
            continue
        seen.add(k)
        out.append(p)
    return out


def split_merged(digits, known):
    """All ways to cut a digit string into valid ports, best guess first."""
    results = []

    def rec(i, parts):
        if i == len(digits):
            results.append(list(parts))
            return
        if len(parts) >= 8:
            return
        for j in range(i + 1, min(i + 5, len(digits)) + 1):
            p = digits[i:j]
            if p[0] == "0" or int(p) > 65535:
                break
            parts.append(p)
            rec(j, parts)
            parts.pop()

    rec(0, [])

    def cost(parts):
        return (sum(1 if int(p) in known else 10 for p in parts), len(parts))

    results.sort(key=cost)
    return [";".join(p) for p in results]


def merged_port_issue(digits, display, acc, known):
    """Excel turned a list like 80,443,8443 into one number."""
    cands = split_merged(digits, known)
    naive = display.replace(",", ";")
    if not cands:
        acc.add("%s is not a valid port list" % display, MANUAL)
        return None
    best = cands[0]
    if best == digits:
        acc.add("Number format shows %s with a thousands comma. A CSV export writes it as "
                "%s, saved as plain %s" % (display, display, digits), AUTO)
        return best
    others = [c for c in cands[1:4] if c != best]
    if naive != best and naive in cands and naive not in others:
        others.insert(0, naive)
    msg = "Excel joined the ports into one number (%s)" % display
    if naive == best:
        acc.add(msg, AUTO)
    else:
        acc.add(msg + ". Best guess %s, other options: %s" % (best, ", ".join(others[:3])), CHECK)
    return best


def check_port_cell(cell, acc, known):
    if cell.is_number:
        t = cell.text
        if not re.fullmatch(r"\d+", t):
            acc.add("Port is not a whole number: %s" % t, MANUAL)
            return t
        n = int(t)
        if cell.thousands or n > 65535:
            return merged_port_issue(t, "{:,}".format(n), acc, known) or t
        return t
    t = clean_ws(cell.text, acc)
    if t == "":
        acc.add("Port is empty", MANUAL)
        return t
    t = t.replace("，", ",").replace("；", ";") if re.fullmatch(r"[\d，]+", t) else t
    if re.fullmatch(r"\d{1,3}(,\d{3})+", t):
        return merged_port_issue(t.replace(",", ""), t, acc, known) or t
    if re.search(r"\s+-\s*|\s*-\s+", t):
        acc.add("Space inside a port range", AUTO)
        t = re.sub(r"\s*-\s*", "-", t)
    parts = split_list(t, acc)
    out = []
    for p in parts:
        if p.lower() == "any":
            acc.add('"Any" should be *', AUTO)
            p = "*"
        if p == "*":
            out.append(p)
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", p)
        if not m:
            acc.add("Not a valid port: '%s'" % p, MANUAL)
            out.append(p)
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else None
        norm = str(a) if b is None else "%d-%d" % (a, b)
        if norm != p:
            acc.add("Leading zero in port %s" % p, AUTO)
        if a > 65535 or (b is not None and b > 65535):
            acc.add("Port %s is out of range (0-65535)" % p, MANUAL)
        elif b is not None and a > b:
            acc.add("Port range %s starts higher than it ends" % p, MANUAL)
        out.append(norm)
    out = dedupe(out, acc)
    if "*" in out and len(out) > 1:
        acc.add("* (any port) mixed with other ports", MANUAL)
    return ";".join(out)


def check_addr_cell(cell, acc):
    t = clean_ws(cell.text, acc)
    if t == "":
        acc.add("Address is empty", MANUAL)
        return t
    parts = split_list(t, acc)
    out, kinds = [], []
    for p in parts:
        if p.lower() == "any":
            acc.add('"Any" should be *', AUTO)
            p = "*"
        if p == "*":
            out.append(p)
            kinds.append("any")
            continue
        if re.fullmatch(r"(?i)asg[A-Za-z0-9._-]*", p):
            out.append(p)  # application security group
            kinds.append("asg")
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", p):
            base = p.split(".")[0]
            canon = TAG_LOWER.get(base.lower())
            if canon:
                if canon != base:
                    acc.add("Service tag spelling %s should be %s" % (base, canon), AUTO)
                p = canon + p[len(base):]
            else:
                acc.add("'%s' is not a known service tag, check the spelling" % p, NOTE)
            out.append(p)
            kinds.append("tag")
            continue
        ip_part = p.split("/")[0]
        if re.search(r"(^|\.)0\d", ip_part):
            acc.add("Leading zero in IP %s" % p, MANUAL)
            out.append(p)
            kinds.append("bad")
            continue
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            acc.add("Not a valid IP or CIDR: '%s'" % p, MANUAL)
            out.append(p)
            kinds.append("bad")
            continue
        if "/" in p:
            try:
                ipaddress.ip_network(p, strict=True)
            except ValueError:
                acc.add("%s has host bits set, the network is %s" % (p, net), NOTE)
        out.append(p)
        kinds.append("ip")
    out = dedupe(out, acc)
    if len(out) > 1 and ("tag" in kinds or "any" in kinds):
        acc.add("A service tag or * can't be mixed with other addresses in one rule", MANUAL)
    elif "asg" in kinds and any(k != "asg" for k in kinds):
        acc.add("An application security group can't be mixed with IPs in one rule", MANUAL)
    return ";".join(out)


def check_enum_cell(cell, acc, mapping, what):
    t = clean_ws(cell.text, acc)
    if t == "":
        acc.add("%s is empty" % what, MANUAL)
        return t
    canon = mapping.get(t.lower())
    if canon is None:
        acc.add("'%s' is not a valid %s (%s)" % (
            t, what.lower(), ", ".join(sorted(set(mapping.values())))), MANUAL)
        return t
    if canon != t:
        acc.add("%s should be %s" % (t, canon), AUTO)
    return canon


def check_priority_cell(cell, acc):
    if cell.is_number:
        t = cell.text
        if cell.thousands and re.fullmatch(r"\d+", t) and int(t) >= 1000:
            acc.add("Shown as {:,} with a thousands comma, a CSV export writes the comma"
                    .format(int(t)), AUTO)
    else:
        t = clean_ws(cell.text, acc)
        if re.fullmatch(r"\d{1,3}(,\d{3})+", t):
            acc.add("Thousands comma in priority %s" % t, AUTO)
            t = t.replace(",", "")
    if t == "":
        acc.add("Priority is empty", MANUAL)
    elif not re.fullmatch(r"\d+", t):
        acc.add("Priority is not a whole number: %s" % t, MANUAL)
    elif not 100 <= int(t) <= 4096:
        acc.add("Priority %s is out of range (100-4096)" % t, MANUAL)
    return t


def check_next_hop_type(cell, acc):
    t = clean_ws(cell.text, acc)
    if t == "":
        acc.add("Next hop type is empty", MANUAL)
        return t
    canon = NEXT_HOP_TYPES.get(re.sub(r"[^a-z]", "", t.lower()))
    if canon is None:
        acc.add("'%s' is not a valid next hop type (%s)" % (
            t, ", ".join(NEXT_HOP_TYPES.values())), MANUAL)
        return t
    if canon != t:
        acc.add("%s should be %s" % (t, canon), AUTO)
    return canon


def check_next_hop_ip(cell, acc, hop_type):
    t = clean_ws(cell.text, acc)
    if hop_type == "VirtualAppliance":
        if t == "":
            acc.add("VirtualAppliance needs a next hop IP", MANUAL)
            return t
        try:
            ipaddress.ip_address(t)
        except ValueError:
            acc.add("Not a valid next hop IP: '%s' (one IP, no /mask)" % t, MANUAL)
    elif t and hop_type in NEXT_HOP_TYPES.values():
        acc.add("Next hop IP is only allowed with VirtualAppliance, not %s" % hop_type, MANUAL)
    return t


def check_name(cell, acc, what):
    t = clean_ws(cell.text, acc)
    if t == "":
        acc.add("%s is empty" % what, MANUAL)
    elif not re.fullmatch(NAME_RE, t):
        acc.add("Azure names allow letters, numbers, . - _ only, must start with a letter "
                "or number and end with a letter, number or _ (max 80)", MANUAL)
    return t


def check_sheet(sh):
    findings = []
    data = [(rn, cells) for rn, cells in sh.rows if rn > sh.header_row]
    role_cols = {ci: r for ci, r in sh.roles.items() if sh.headers.get(ci)}

    def col_of(role):
        return next((ci for ci, r in role_cols.items() if r == role), None)

    name_col = col_of("name")
    group_col = col_of("nsg") if sh.mode == "nsg" else col_of("rtname")
    dir_col, pri_col = col_of("direction"), col_of("priority")
    hop_col, prefix_col = col_of("nexthoptype"), col_of("prefix")
    # next hop type is checked first so the next hop IP check can use it
    order = sorted(role_cols, key=lambda ci: (role_cols[ci] != "nexthoptype", ci))

    known = set(COMMON_PORTS)
    for rn, cells in data:
        for ci, r in role_cols.items():
            c = cells.get(ci)
            if r == "port" and c is not None and not c.thousands:
                for p in c.text.strip().split(";"):
                    if re.fullmatch(r"\d{1,5}", p) and 0 < int(p) <= 65535:
                        known.add(int(p))

    def label(ci):
        return "%s · %s" % (col_letter(ci), sh.headers.get(ci, ""))

    clean_vals = {}
    used_rows = []
    for rn, cells in data:
        if not any(cells.get(ci) is not None and cells[ci].text.strip()
                   for ci in role_cols if role_cols[ci] != "other"):
            continue
        used_rows.append(rn)
        rule = cells[name_col].text.strip() if name_col in cells else ""
        group = cells[group_col].text.strip() if group_col in cells else ""
        for ci in order:
            role = role_cols[ci]
            cell = cells.get(ci)
            if cell is None:
                cell = Cell("", ref=col_letter(ci) + str(rn))
            if cell.formula:
                continue
            acc = Acc()
            if role == "port":
                fixed = check_port_cell(cell, acc, known)
            elif role == "addr":
                fixed = check_addr_cell(cell, acc)
            elif role == "direction":
                fixed = check_enum_cell(cell, acc, DIRECTIONS, "Direction")
            elif role == "access":
                fixed = check_enum_cell(cell, acc, ACCESS, "Access")
            elif role == "protocol":
                fixed = check_enum_cell(cell, acc, PROTOCOLS, "Protocol")
            elif role == "priority":
                fixed = check_priority_cell(cell, acc)
            elif role == "name":
                fixed = check_name(cell, acc, "Route name" if sh.mode == "rt" else "Rule name")
            elif role == "rtname":
                fixed = check_name(cell, acc, "Route table name")
            elif role == "nexthoptype":
                fixed = check_next_hop_type(cell, acc)
            elif role == "nexthopip":
                fixed = check_next_hop_ip(cell, acc, clean_vals.get((rn, hop_col), ""))
            elif role == "prefix":
                fixed = check_addr_cell(cell, acc)
                if ";" in fixed:
                    acc.add("A route takes one address prefix, this has several", MANUAL)
            else:
                fixed = clean_ws(cell.text, acc) if cell.text and not cell.is_number else cell.text
            clean_vals[(rn, ci)] = fixed
            if acc.status:
                display_cur = cell.text
                if cell.is_number and cell.thousands and re.fullmatch(r"\d+", cell.text):
                    display_cur = "{:,} (number {})".format(int(cell.text), cell.text)
                elif cell.is_number:
                    display_cur = cell.text + " (number)"
                f = Finding(rn, ci, label(ci), rule, acc.issues, display_cur, fixed, acc.status,
                            ref=cell.ref or (col_letter(ci) + str(rn)))
                f.raw = cell.text
                f.group = group
                findings.append(f)

    def key_of(rn, ci):
        return (clean_vals.get((rn, ci), "") or "").lower() if ci is not None else ""

    def dup_check(col, key_cols, text):
        if col is None:
            return
        groups = {}
        for rn in used_rows:
            v = key_of(rn, col)
            if v:
                groups.setdefault(tuple(key_of(rn, k) for k in key_cols) + (v,), []).append(rn)
        for key, rns in groups.items():
            if len(rns) < 2:
                continue
            for rn in rns:
                others = ", ".join(str(x) for x in rns if x != rn)
                f = Finding(rn, col, label(col), clean_vals.get((rn, name_col), ""),
                            [text(key, others)], clean_vals.get((rn, col), ""), "", MANUAL,
                            ref=col_letter(col) + str(rn), row_level=True)
                f.raw = f.current
                f.group = clean_vals.get((rn, group_col), "")
                findings.append(f)

    if sh.mode == "nsg":
        dup_check(pri_col, [group_col, dir_col], lambda k, o: "Priority %s also used by row %s (same NSG, %s)"
                  % (k[-1], o, k[1] or "same direction"))
        dup_check(name_col, [group_col], lambda k, o: "Rule name also used by row %s (same NSG)" % o)
    else:
        dup_check(name_col, [group_col], lambda k, o: "Route name also used by row %s (same route table)" % o)
        dup_check(prefix_col, [group_col], lambda k, o: "Address prefix %s also used by row %s (same route table)"
                  % (k[-1], o))

    findings.sort(key=lambda f: (f.rownum, f.colidx, f.row_level))
    return findings, len(used_rows)


# ---------- saving ----------

def collect_fixes(findings, include_check):
    fixes = {}
    for f in findings:
        if f.row_level and f.status != EDITED:
            continue
        if f.status == AUTO or f.status == EDITED or (f.status == CHECK and include_check):
            fixes[(f.rownum, f.colidx)] = (f.ref, f.fixed)
    return fixes


def output_path(path):
    stem, ext = os.path.splitext(path)
    return stem + "_fixed" + ext


def _patch_sheet_xml(xml, by_ref):
    done = set()
    cell_re = re.compile(r"<c\b([^>]*?)(/>|>(.*?)</c>)", re.S)

    def repl(m):
        attrs = m.group(1)
        rm = re.search(r'\br="([A-Z]+\d+)"', attrs)
        if not rm or rm.group(1) not in by_ref:
            return m.group(0)
        ref = rm.group(1)
        sm = re.search(r'\bs="(\d+)"', attrs)
        s_attr = ' s="%s"' % sm.group(1) if sm else ""
        done.add(ref)
        val = by_ref[ref]
        if val == "":
            return '<c r="%s"%s/>' % (ref, s_attr)
        return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
            ref, s_attr, escape(val))

    out = cell_re.sub(repl, xml)
    return out, done


def save_fixed(sh, fixes, dst):
    tmp = dst + ".tmp"
    if sh.kind == "xlsx":
        by_ref = {ref: val for (ref, val) in fixes.values()}
        with zipfile.ZipFile(sh.path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == sh.sheet_zip_path:
                    xml, _ = _patch_sheet_xml(data.decode("utf-8"), by_ref)
                    data = xml.encode("utf-8")
                zout.writestr(item, data)
    else:
        out = []
        for i, (rec, raw) in enumerate(zip(sh.csv_records, sh.csv_raws)):
            rn = i + 1
            changes = {ci: v for (r, ci), (_, v) in fixes.items() if r == rn}
            if not changes:
                out.append(raw)
                continue
            rec2 = list(rec)
            for ci, v in changes.items():
                while len(rec2) <= ci:
                    rec2.append("")
                rec2[ci] = v
            m = re.search(r"(\r\n|\n|\r)$", raw)
            end = m.group(1) if m else ""
            b = io.StringIO()
            csv.writer(b, quoting=csv.QUOTE_ALL, lineterminator="").writerow(rec)
            all_quoted = b.getvalue() == raw[:len(raw) - len(end)]
            b = io.StringIO()
            csv.writer(b, quoting=csv.QUOTE_ALL if all_quoted else csv.QUOTE_MINIMAL,
                       lineterminator="").writerow(rec2)
            out.append(b.getvalue() + end)
        with open(tmp, "wb") as fh:
            fh.write("".join(out).encode(sh.csv_encoding))
    os.replace(tmp, dst)


def verify_saved(sh, fixes, dst):
    """Re-read the saved file and return the cells that don't hold the expected value."""
    if sh.kind == "xlsx":
        sh2 = read_xlsx(dst, sh.sheet_name)
    else:
        sh2 = read_csv(dst)
    rows = dict(sh2.rows)
    bad = []
    for (rn, ci), (ref, val) in fixes.items():
        c = rows.get(rn, {}).get(ci)
        got = c.text if c is not None else ""
        if got != val:
            bad.append((ref, val, got))
    if len(sh2.rows) != len(sh.rows) and sh.kind == "csv":
        bad.append(("row count", str(len(sh.rows)), str(len(sh2.rows))))
    return bad


# ---------- GUI ----------

def gui_main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    is_mac = sys.platform == "darwin"
    mod = "Command" if is_mac else "Control"
    mod_name = "Cmd" if is_mac else "Ctrl"

    def load_settings():
        try:
            with open(SETTINGS_FILE) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_settings(d):
        try:
            with open(SETTINGS_FILE, "w") as fh:
                json.dump(d, fh)
        except Exception:
            pass

    root = tk.Tk()
    root.title("NSG Rule Checker v%s" % VERSION)
    root.geometry("1280x720")
    root.minsize(900, 500)

    settings = load_settings()
    state = {"sheet": None, "findings": [], "rules": 0}

    file_var = tk.StringVar(value=settings.get("last_file", ""))
    sheet_var = tk.StringVar(value=settings.get("last_sheet", ""))
    inc_check_var = tk.BooleanVar(value=False)
    filter_var = tk.StringVar()
    status_var = tk.StringVar(value="Pick a .xlsx or .csv file, then click Check.")
    summary_var = tk.StringVar(value="")

    outer = ttk.Frame(root, padding=10)
    outer.pack(fill="both", expand=True)

    top = ttk.Frame(outer)
    top.pack(fill="x")
    ttk.Label(top, text="File:").grid(row=0, column=0, sticky="w")
    file_entry = ttk.Entry(top, textvariable=file_var)
    file_entry.grid(row=0, column=1, sticky="ew", padx=6)
    top.columnconfigure(1, weight=1)

    ttk.Label(top, text="Sheet:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    sheet_combo = ttk.Combobox(top, textvariable=sheet_var, state="readonly", width=30)
    sheet_combo.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

    def refresh_sheets(*_):
        path = file_var.get().strip()
        if path.lower().endswith((".xlsx", ".xlsm")) and os.path.isfile(path):
            try:
                names = [n for n, _ in xlsx_sheet_list(path)]
            except Exception:
                names = []
            sheet_combo.configure(values=names, state="readonly" if names else "disabled")
            if names and sheet_var.get() not in names:
                pick = next((n for n in names if n.lower() == "nsg"), names[0])
                sheet_var.set(pick)
        else:
            sheet_combo.configure(values=[], state="disabled")
            sheet_var.set("")

    def browse():
        start = os.path.dirname(file_var.get()) if file_var.get() else os.path.expanduser("~")
        p = filedialog.askopenfilename(
            title="Pick the NSG rules file", initialdir=start,
            filetypes=[("Excel or CSV", "*.xlsx *.xlsm *.csv"), ("Excel", "*.xlsx *.xlsm"),
                       ("CSV", "*.csv"), ("All files", "*.*")])
        if p:
            file_var.set(p)
            refresh_sheets()

    ttk.Button(top, text="Browse…", command=browse).grid(row=0, column=2)

    btns = ttk.Frame(outer)
    btns.pack(fill="x", pady=(10, 4))
    check_btn = ttk.Button(btns, text="Check")
    check_btn.pack(side="left")
    save_btn = ttk.Button(btns, text="Save fixed copy")
    save_btn.pack(side="left", padx=(6, 0))
    ttk.Checkbutton(btns, text="Also save orange 'Check' fixes",
                    variable=inc_check_var).pack(side="left", padx=(10, 0))
    export_btn = ttk.Button(btns, text="Export report")
    export_btn.pack(side="right")
    copy_btn = ttk.Button(btns, text="Copy results")
    copy_btn.pack(side="right", padx=(0, 6))

    frow = ttk.Frame(outer)
    frow.pack(fill="x", pady=(4, 4))
    ttk.Label(frow, text="Filter:").pack(side="left")
    filter_entry = ttk.Entry(frow, textvariable=filter_var, width=40)
    filter_entry.pack(side="left", padx=6)
    ttk.Button(frow, text="x", width=2, command=lambda: filter_var.set("")).pack(side="left")
    ttk.Label(frow, textvariable=summary_var).pack(side="right")

    panes = ttk.PanedWindow(outer, orient="vertical")
    panes.pack(fill="both", expand=True)

    tframe = ttk.Frame(panes)
    cols = ("n", "row", "col", "rule", "group", "issue", "current", "fixed", "fix")
    heads = ["#", "Row", "Column", "Rule name", "NSG", "Issue", "Current value", "Fixed value", "Fix"]
    widths = (36, 44, 100, 190, 270, 250, 130, 130, 60)
    tree = ttk.Treeview(tframe, columns=cols, show="headings", selectmode="extended")
    for c, h, w in zip(cols, heads, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, minwidth=30, stretch=c in ("issue", "rule"),
                    anchor="center" if c in ("n", "row", "fix") else "w")
    vs = ttk.Scrollbar(tframe, orient="vertical", command=tree.yview)
    hs = ttk.Scrollbar(tframe, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.grid(row=0, column=1, sticky="ns")
    hs.grid(row=1, column=0, sticky="ew")
    tframe.rowconfigure(0, weight=1)
    tframe.columnconfigure(0, weight=1)
    panes.add(tframe, weight=4)

    dframe = ttk.Frame(panes)
    details = tk.Text(dframe, height=5, wrap="word", relief="flat",
                      background=root.cget("background"))
    details.pack(fill="both", expand=True, pady=(4, 0))
    details.configure(state="disabled")
    panes.add(dframe, weight=1)

    tree.tag_configure(MANUAL, background="#f8d7da")
    tree.tag_configure(CHECK, background="#ffe0b2")
    tree.tag_configure(NOTE, background="#fff8d6")
    tree.tag_configure(AUTO, background="#ffffff")
    tree.tag_configure(EDITED, background="#d4edda")
    tree.tag_configure(SKIPPED, background="#eeeeee", foreground="#888888")

    legend = ttk.Label(outer, text=(
        "Auto = safe fix, saved   |   Orange Check = best guess, check it (saved only if ticked)   |   "
        "Red Manual = fix by hand   |   Yellow Note = worth a look\n"
        "␣ = space, ⍽ = non-breaking space, ⏎ = line break   |   "
        "Right-click a row to edit or skip a fix"), foreground="#555555")
    legend.pack(fill="x", pady=(4, 0))

    status = ttk.Label(outer, textvariable=status_var, anchor="w", relief="sunken", padding=(6, 2))
    status.pack(fill="x", pady=(6, 0))

    def row_values(i, f):
        return (i + 1, f.rownum, f.col_label, f.rule, f.group, f.issue_text, visible(f.current),
                visible(f.fixed) if f.status not in (MANUAL, NOTE, SKIPPED) or f.fixed != f.raw
                else "", f.status)

    def populate(*_):
        tree.delete(*tree.get_children())
        q = filter_var.get().strip().lower()
        shown = 0
        for i, f in enumerate(state["findings"]):
            vals = row_values(i, f)
            if q and q not in " ".join(str(v) for v in vals).lower():
                continue
            tree.insert("", "end", iid=str(i), values=vals, tags=(f.status,))
            shown += 1
        fs = state["findings"]
        counts = {s: sum(1 for f in fs if f.status == s) for s in (AUTO, CHECK, MANUAL, NOTE, EDITED, SKIPPED)}
        parts = ["%d %s" % (counts[s], s) for s in (AUTO, CHECK, MANUAL, NOTE, EDITED, SKIPPED) if counts[s]]
        summary_var.set(("Showing %d of %d   " % (shown, len(fs)) if q else "") + "   ".join(parts))

    filter_var.trace_add("write", populate)

    def show_details(*_):
        sel = tree.selection()
        details.configure(state="normal")
        details.delete("1.0", "end")
        if sel:
            f = state["findings"][int(sel[0])]
            details.insert("end", "Row %s, %s   [%s]   %s: %s\n" % (f.rownum, f.col_label, f.status, heads[4], f.group))
            for s in f.issues:
                details.insert("end", "  • " + s + "\n")
            details.insert("end", "Current: %s\n" % visible(f.current))
            if not f.row_level or f.status == EDITED:
                details.insert("end", "Fixed:   %s\n" % visible(f.fixed))
        details.configure(state="disabled")

    tree.bind("<<TreeviewSelect>>", show_details)

    def do_check(*_):
        path = file_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("NSG Rule Checker", "Pick a .xlsx or .csv file first.")
            return
        refresh_sheets()
        try:
            sh = load(path, sheet_var.get() or None)
            findings, nrules = check_sheet(sh)
        except CheckerError as e:
            messagebox.showerror("NSG Rule Checker", str(e))
            return
        except Exception as e:
            messagebox.showerror("NSG Rule Checker", "Couldn't read the file:\n%s" % e)
            return
        state.update(sheet=sh, findings=findings, rules=nrules)
        heads[3], heads[4] = (("Route name", "Route table") if sh.mode == "rt" else ("Rule name", "NSG"))
        tree.heading("rule", text=heads[3])
        tree.heading("group", text=heads[4])
        settings.update(last_file=path, last_sheet=sheet_var.get())
        save_settings(settings)
        populate()
        found = [sh.headers[ci] for ci in sorted(sh.roles) if sh.roles[ci] != "other"]
        what = "%d %s" % (nrules, "routes (route table sheet)" if sh.mode == "rt" else "rules (NSG sheet)")
        if not findings:
            status_var.set("Checked %s: no problems found. Columns checked: %s"
                           % (what, ", ".join(found)))
        else:
            status_var.set("Checked %s, %d cells need attention. Columns checked: %s"
                           % (what, len(findings), ", ".join(found)))

    def do_save():
        sh = state["sheet"]
        if sh is None:
            messagebox.showinfo("NSG Rule Checker", "Run Check first.")
            return
        fixes = collect_fixes(state["findings"], inc_check_var.get())
        if not fixes:
            messagebox.showinfo("NSG Rule Checker", "There are no fixes to save.")
            return
        dst = output_path(sh.path)
        try:
            save_fixed(sh, fixes, dst)
        except PermissionError:
            messagebox.showerror("NSG Rule Checker",
                                 "Couldn't write:\n%s\n\nIf it is open in Excel, close it and try again." % dst)
            return
        except Exception as e:
            messagebox.showerror("NSG Rule Checker", "Saving failed:\n%s" % e)
            return
        bad = verify_saved(sh, fixes, dst)
        left_check = sum(1 for f in state["findings"] if f.status == CHECK) if not inc_check_var.get() else 0
        manual = sum(1 for f in state["findings"] if f.status == MANUAL)
        msg = "Saved %d cell fixes to:\n%s\n\nThe original file was not changed." % (len(fixes), dst)
        if bad:
            msg += "\n\nSELF-CHECK FAILED for %d cells:\n" % len(bad) + "\n".join(
                "  %s: expected '%s', file has '%s'" % b for b in bad[:10])
            messagebox.showerror("NSG Rule Checker", msg)
            status_var.set("Saved %s, but the self-check found %d problems." % (os.path.basename(dst), len(bad)))
            return
        msg += "\nSelf-check: all %d fixes confirmed in the saved file." % len(fixes)
        if left_check:
            msg += "\n\n%d orange 'Check' fixes were NOT saved. Tick the box or edit them to include." % left_check
        if manual:
            msg += "\n%d red 'Manual' cells still need fixing by hand." % manual
        messagebox.showinfo("NSG Rule Checker", msg)
        status_var.set("Saved %d fixes to %s (self-check passed)." % (len(fixes), os.path.basename(dst)))

    def as_tsv(iids, with_header=True):
        lines = ["\t".join(heads)] if with_header else []
        for iid in iids:
            lines.append("\t".join(str(v) for v in tree.item(iid, "values")))
        return "\n".join(lines)

    def to_clipboard(text, what):
        root.clipboard_clear()
        root.clipboard_append(text)
        status_var.set("Copied %s." % what)

    def copy_results():
        iids = tree.get_children()
        if not iids:
            status_var.set("Nothing to copy.")
            return
        to_clipboard(as_tsv(iids), "%d rows" % len(iids))

    def copy_selected(*_):
        if root.focus_get() in (file_entry, filter_entry):
            return
        sel = tree.selection()
        if sel:
            to_clipboard(as_tsv(sel, with_header=len(sel) > 1), "%d row(s)" % len(sel))
            return "break"

    def cell_at(event):
        iid = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not iid or not col:
            return None, None
        return iid, int(col[1:]) - 1

    def on_double(event):
        iid, ci = cell_at(event)
        if iid is not None:
            to_clipboard(str(tree.item(iid, "values")[ci]), "'%s'" % heads[ci])

    def export_report():
        if not state["findings"]:
            messagebox.showinfo("NSG Rule Checker", "Nothing to export, run Check first.")
            return
        sh = state["sheet"]
        init = os.path.splitext(os.path.basename(sh.path))[0] + "_check_report.csv"
        p = filedialog.asksaveasfilename(title="Save report", initialfile=init,
                                         initialdir=os.path.dirname(sh.path),
                                         defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        try:
            with open(p, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(heads)
                for i, f in enumerate(state["findings"]):
                    w.writerow(row_values(i, f))
        except PermissionError:
            messagebox.showerror("NSG Rule Checker", "Couldn't write the report. Is it open in Excel?")
            return
        status_var.set("Report saved to %s" % p)

    def selected_findings():
        return [state["findings"][int(i)] for i in tree.selection()]

    def edit_fix():
        fs = selected_findings()
        if len(fs) != 1:
            messagebox.showinfo("NSG Rule Checker", "Select one row to edit.")
            return
        f = fs[0]
        start = f.fixed if f.fixed else f.current
        v = simpledialog.askstring("Edit fixed value",
                                   "Row %s, %s\n\nValue to save:" % (f.rownum, f.col_label),
                                   initialvalue=start, parent=root)
        if v is None:
            return
        f.fixed = v
        f.status = EDITED
        populate()
        status_var.set("Row %s will be saved as '%s'." % (f.rownum, v))

    def skip_fix():
        for f in selected_findings():
            f.status = SKIPPED
        populate()

    def restore_fix():
        for f in selected_findings():
            f.status = f.orig_status
            f.fixed = f.suggested
        populate()

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Copy cell", command=lambda: None)
    menu.add_command(label="Copy selected rows", command=copy_selected)
    menu.add_separator()
    menu.add_command(label="Edit fixed value…", command=edit_fix)
    menu.add_command(label="Don't fix this", command=skip_fix)
    menu.add_command(label="Restore suggested fix", command=restore_fix)

    def on_right(event):
        iid, ci = cell_at(event)
        if iid is None:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
        menu.entryconfigure(0, command=lambda: to_clipboard(
            str(tree.item(iid, "values")[ci]), "'%s'" % heads[ci]))
        menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Double-1>", on_double)
    tree.bind("<Button-3>", on_right)
    if is_mac:
        tree.bind("<Button-2>", on_right)
        tree.bind("<Control-Button-1>", on_right)
    root.bind("<%s-c>" % mod, copy_selected)
    root.bind("<%s-f>" % mod, lambda e: (filter_entry.focus_set(), "break"))
    filter_entry.bind("<Escape>", lambda e: filter_var.set(""))
    file_entry.bind("<Return>", do_check)
    file_entry.bind("<FocusOut>", refresh_sheets)
    root.bind("<F5>", do_check)

    check_btn.configure(command=do_check)
    save_btn.configure(command=do_save)
    copy_btn.configure(command=copy_results)
    export_btn.configure(command=export_report)

    refresh_sheets()
    if settings.get("last_sheet") and settings.get("last_sheet") in (sheet_combo.cget("values") or ()):
        sheet_var.set(settings["last_sheet"])
    status_var.set(status_var.get() + "   (%s+C copy, %s+F filter, F5 check)" % (mod_name, mod_name))
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
