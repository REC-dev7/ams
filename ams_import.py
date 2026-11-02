"""Import wizard: upload -> map columns -> review values -> preview -> approve (and reverse when safe).

Old registers arrive in many shapes (CSV or Excel, odd headings, placeholders such as -No Value-, one row for 20 computers).
Nothing goes into the live register until a person has seen the preview and approved it. The original file is kept untouched.
"""
import csv
import difflib
import hashlib
import io
import json
import os
import re
import secrets
from datetime import date, datetime

from flask import Response, abort, flash, g, redirect, render_template, request, send_file, url_for

PLACEHOLDERS = {"", "-", "--", "---", "n/a", "na", "nil", "null", "none", "-no value-", "no value", "not available", "unknown", "?"}
MAX_IMPORT_ROWS = 20000
TARGETS = [
    ("ignore", "Ignore this column"), ("name", "Asset name"), ("product", "Product / item (used as the name if there is no name)"),
    ("tag", "Asset tag (only if it is already the permanent tag)"), ("legacy_code", "Old asset code"),
    ("source_asset_id", "ID in the old system"), ("product_code", "Product code (a catalogue code, not an asset ID)"),
    ("category", "Category"), ("sub_category", "Sub category (kept in the notes)"), ("department", "Department"),
    ("building", "Building"), ("room", "Room / lab"), ("custodian", "Person holding it"), ("custody_date", "Date it was allotted"),
    ("allot_status", "Allotment status"), ("status", "Status"), ("brand", "Brand"), ("model", "Model"), ("serial_no", "Serial number"),
    ("ip_address", "IP address"), ("mac_address", "MAC address"), ("purchase_date", "Purchase date"), ("warranty_until", "Warranty until"),
    ("cost", "Cost"), ("quantity", "Quantity"), ("vendor", "Vendor"), ("po_number", "PO number"), ("invoice_number", "Invoice number"),
    ("indent_no", "Indent number"), ("cpr_no", "CPR number"), ("grn_no", "GRN number"), ("stock_register_ref", "Stock register (book / page)"),
    ("notes", "Notes"), ("parent_tag", "Parent asset tag (for parts)"),
]
TARGET_KEYS = [k for k, _ in TARGETS]
SYNONYMS = {
    "name": ["name", "asset", "asset name", "item", "item name", "equipment", "device", "device name"],
    "product": ["product", "product name"], "tag": ["tag", "asset tag", "asset tag no"],
    "legacy_code": ["asset code", "old code", "old asset code", "legacy code", "code"], "source_asset_id": ["asset id", "assetid"],
    "product_code": ["product code"], "category": ["category", "asset category", "type", "asset type"],
    "sub_category": ["sub category", "subcategory", "sub type"], "department": ["department", "dept", "allotted to department", "allotted department"],
    "building": ["building", "block"], "room": ["room", "lab", "infrastructure name", "location", "room lab"],
    "custodian": ["allotted to member", "custodian", "assigned to", "holder", "allotted to"],
    "custody_date": ["allotment date", "issue date", "date of allotment"], "allot_status": ["allotment status"], "status": ["status", "condition"],
    "brand": ["brand", "make", "manufacturer"], "model": ["model", "model no", "model number"],
    "serial_no": ["serial", "serial no", "serial number", "sl no", "slno"], "ip_address": ["ip", "ip address"], "mac_address": ["mac", "mac address"],
    "purchase_date": ["purchase date", "date of purchase", "purchased on", "date"], "warranty_until": ["warranty", "warranty until", "warranty end", "warranty end date"],
    "cost": ["cost", "value", "price", "purchase cost", "amount", "unit cost", "unit price"], "quantity": ["quantity", "qty", "nos", "no of units"],
    "vendor": ["vendor", "supplier"], "po_number": ["po", "po no", "po number", "purchase order"], "invoice_number": ["invoice", "invoice no", "invoice number"],
    "indent_no": ["indent", "indent no", "indent number"], "cpr_no": ["cpr", "cpr no"], "grn_no": ["grn", "grn no"],
    "stock_register_ref": ["stock register", "book page", "stock book"], "notes": ["notes", "remarks", "description"], "parent_tag": ["parent", "parent tag"],
}
# first guesses for status words; anything else is put to a person once and remembered
STATUS_GUESS = {"alloted": "Deployed|Working", "allotted": "Deployed|Working", "issued": "Deployed|Working", "working": "Deployed|Working",
                "in use": "Deployed|Working", "faulty": "Deployed|Faulty", "not working": "Deployed|Faulty", "under repair": "Under maintenance|Faulty",
                "under service": "Under maintenance|Faulty", "in store": "In stock|Working", "in stock": "In stock|Working",
                "available": "In stock|Working", "returned": "In stock|Working", "scrapped": "Disposed|Obsolete", "disposed": "Disposed|Obsolete",
                "lost": "Lost / missing|Unknown"}
STATUS_CHOICES = ["Deployed|Working", "Deployed|Faulty", "In stock|Working", "In stock|Faulty", "Under maintenance|Faulty",
                  "Disposed|Obsolete", "Lost / missing|Unknown", "Received (pending inspection)|Unknown"]
DATE_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y"]
DAY_FIRST = ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y"]


def norm_header(h):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (h or "").lower())).strip()


def norm_value(v):
    return re.sub(r"\s+", " ", (v or "").strip()).lower()


def clean(v):
    v = (v if v is not None else "").strip()
    return None if v.lower() in PLACEHOLDERS else v


def guess_targets(headers):
    """Best-guess target for each column heading. Never guesses the same single-use target twice."""
    out, used = {}, set()
    for h in headers:
        n = norm_header(h)
        hit = next((t for t, words in SYNONYMS.items() if n in words and t not in used), "ignore")
        out[h] = hit
        if hit != "ignore":
            used.add(hit)
    return out


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_table(path, filename, sheet=None):
    """(sheet names, rows of text). CSV or Excel (.xlsx); the file itself is never changed."""
    if filename.lower().endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            names = wb.sheetnames
            ws = wb[sheet if sheet in names else names[0]]
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append([cell_text(v) for v in r])
                if len(rows) > MAX_IMPORT_ROWS + 50:
                    break
        finally:
            wb.close()
        return names, rows
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return ["(csv)"], [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)][:MAX_IMPORT_ROWS + 50]


def guess_header_row(rows):
    """1-based number of the first row that looks like column headings (several filled, mostly words)."""
    for i, r in enumerate(rows[:15], start=1):
        filled = [c for c in r if c.strip()]
        if len(filled) >= 3 and sum(not re.fullmatch(r"[\d.,\s-]+", c) for c in filled) >= 0.7 * len(filled):
            return i
    return 1


def parse_date_cell(v):
    """(iso date or None, warning or None). Day/month/year is assumed for 03/04/2025 and flagged, never silently trusted."""
    v = v.strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(v, f).date().isoformat(), None
        except ValueError:
            pass
    for f in DAY_FIRST:
        try:
            d = datetime.strptime(v, f).date()
            return d.isoformat(), (f"'{v}' was read as day/month/year" if d.day <= 12 else None)
        except ValueError:
            pass
    return None, f"date '{v}' was not understood and was left blank"


def parse_number(v):
    return float(re.sub(r"[₹,\s]|rs\.?|inr", "", v, flags=re.I))


def init(A):
    """Attach the import routes to the app module A (called once from app.py)."""
    q, db, app, role = A.q, A.db, A.app, A.role
    IMPORT_DIR = lambda: os.path.join(A.UPLOADS, "imports")

    def get_batch(bid):
        b = q("SELECT * FROM import_batches WHERE id=?", (bid,), one=True)
        if not b:
            abort(404)
        return b

    def settings_of(b):
        return json.loads(b["settings_json"] or "{}")

    def save_settings(bid, cfg):
        db().execute("UPDATE import_batches SET settings_json=? WHERE id=?", (json.dumps(cfg), bid))

    def table_of(b):
        names, rows = read_table(os.path.join(IMPORT_DIR(), b["stored_file"]), b["filename"], b["sheet"])
        return names, rows

    def split(b, rows):
        h = max(b["header_row"] or 1, 1)
        headers = [c.strip() or f"(column {i + 1})" for i, c in enumerate(rows[h - 1])] if len(rows) >= h else []
        return headers, rows[h:], h

    # ---------- value dictionary: categories, departments, statuses ----------

    def known(kind):
        if kind == "category":
            return {norm_value(r["name"]): r["name"] for r in q("SELECT name FROM categories")}
        return {norm_value(r["name"]): r["name"] for r in q("SELECT name FROM departments")}

    def saved(kind):
        return {r["source"]: r["target"] for r in q("SELECT source, target FROM value_maps WHERE kind=?", (kind,))}

    def resolve_value(kind, raw):
        """('ok', target) when settled, else ('review', suggestion or None)."""
        n = norm_value(raw)
        if n in saved(kind):
            return "ok", saved(kind)[n]
        if kind == "status":
            return ("ok", STATUS_GUESS[n]) if n in STATUS_GUESS else ("review", None)
        names = known(kind)
        if n in names:
            return "ok", names[n]
        close = difflib.get_close_matches(n, list(names), n=1, cutoff=0.72)
        return "review", names[close[0]] if close else None

    def column_values(b, rows_data, headers, cfg, target):
        cols = [h for h in headers if cfg["columns"].get(h) == target]
        counts = {}
        for r in rows_data:
            for h in cols:
                v = clean(r[headers.index(h)]) if headers.index(h) < len(r) else None
                if v:
                    counts[v] = counts.get(v, 0) + 1
        return counts

    def unresolved(b):
        cfg = settings_of(b)
        _, rows = table_of(b)
        headers, data, _ = split(b, rows)
        out = []
        for kind, targets in (("category", ["category"]), ("department", ["department"]), ("status", ["allot_status", "status"])):
            seen = {}
            for t in targets:
                for v, n in column_values(b, data, headers, cfg, t).items():
                    seen[v] = seen.get(v, 0) + n
            for v, n in sorted(seen.items(), key=lambda x: -x[1]):
                state, sug = resolve_value(kind, v)
                if state == "review":
                    out.append({"kind": kind, "value": v, "count": n, "suggestion": sug})
        return out

    # ---------- staging ----------

    def stage(b):
        """Read the file again with the chosen mapping and rebuild the preview rows. Nothing touches the register."""
        cfg = settings_of(b)
        _, rows = table_of(b)
        headers, data, hrow = split(b, rows)
        cols = cfg["columns"]
        db().execute("DELETE FROM staged_rows WHERE batch_id=?", (b["id"],))
        seen_keys, total, empty = {}, 0, 0
        default_stage = cfg.get("default_stage", "In stock")
        for offset, cells in enumerate(data):
            row_no = hrow + 1 + offset
            if not any(c.strip() for c in cells):
                empty += 1
                continue
            if total >= MAX_IMPORT_ROWS:
                break
            total += 1
            raw = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(headers)}
            d, errors, warnings, notes = {}, [], [], []
            for h in headers:
                t = cols.get(h, "ignore")
                if t != "ignore" and t not in d:
                    v = clean(raw[h])
                    if v is not None:
                        d[t] = v
            first = next((c for c in cells if c.strip()), "").strip().lower()
            if re.match(r"(grand |sub )?total\b", first) or first.startswith("total"):
                warnings.append("Looks like a total line, not equipment")
                skip_reason = "Total or summary line"
            else:
                skip_reason = None
            name = d.get("name") or d.get("product")
            out = {"name": name}
            # category / department / status go through the reviewed dictionary
            for kind, key, fields in (("category", "category", ["category"]), ("department", "department", ["department"])):
                if d.get(key):
                    state, tgt = resolve_value(kind, d[key])
                    if state == "review":
                        errors.append(f"{kind} '{d[key]}' has not been reviewed")
                    elif tgt == "__skip__":
                        skip_reason = skip_reason or f"{kind} '{d[key]}' is not equipment"
                    elif tgt.startswith("__new__:"):
                        out[key], out[key + "_new"] = tgt[8:], True
                    else:
                        out[key] = tgt
            status_raw = d.get("allot_status") or d.get("status")
            stage_name, condition = None, "Working"
            if status_raw:
                state, tgt = resolve_value("status", status_raw)
                if state == "review":
                    errors.append(f"status '{status_raw}' has not been reviewed")
                elif tgt == "__skip__":
                    skip_reason = skip_reason or f"status '{status_raw}' means skip"
                else:
                    stage_name, condition = tgt.split("|")
            if d.get("sub_category"):
                notes.append(f"Sub category: {d['sub_category']}")
            if d.get("product_code"):
                notes.append(f"Product code: {d['product_code']}")
            if d.get("product") and d.get("name") and d["product"] != d["name"]:
                notes.append(f"Product: {d['product']}")
            if d.get("notes"):
                notes.append(d["notes"])
            for f in ("tag", "legacy_code", "source_asset_id", "serial_no", "brand", "model", "ip_address", "mac_address", "vendor", "po_number",
                      "invoice_number", "indent_no", "cpr_no", "grn_no", "stock_register_ref", "custodian", "room", "building", "parent_tag"):
                if d.get(f):
                    out[f] = d[f]
            for f in ("purchase_date", "warranty_until", "custody_date"):
                if d.get(f):
                    iso, warn = parse_date_cell(d[f])
                    out[f] = iso
                    if warn:
                        warnings.append(f"{f.replace('_', ' ')}: {warn}")
            qty = 1
            if d.get("quantity"):
                try:
                    qty = int(parse_number(d["quantity"]))
                    if qty < 1:
                        raise ValueError
                except ValueError:
                    errors.append(f"quantity '{d['quantity']}' is not a whole number")
                    qty = 1
            cost = None
            if d.get("cost"):
                try:
                    cost = parse_number(d["cost"])
                except ValueError:
                    errors.append(f"cost '{d['cost']}' is not a number")
            basis = cfg.get("cost_basis") or "Unknown"
            if cost is not None and basis == "Whole purchase":
                notes.append(f"Whole-purchase cost of {cost:,.0f} was not copied to each asset")
                warnings.append("Whole-purchase cost is not copied onto each asset")
                out["cost_total"] = cost
                cost = None
            out["cost"], out["quantity"], out["notes"] = cost, qty, " · ".join(notes) or None
            if not name:
                errors.append("no name or product")
            if not out.get("department") and not cfg.get("department_id"):
                warnings.append("no department: the default will be missing, so only admins can see it")
            if out.get("parent_tag") and not q("SELECT 1 FROM assets WHERE tag=?", (out["parent_tag"],), one=True):
                errors.append(f"parent {out['parent_tag']} not found")
            identity = [out.get(k) for k in ("tag", "legacy_code", "source_asset_id", "serial_no")]
            kind = "Batch" if (qty > 1 and not any(identity)) else "Asset"
            if kind == "Asset" and qty > 1:
                warnings.append(f"quantity {qty} but it has its own code or serial: treated as one asset")
            if stage_name is None:
                stage_name = default_stage
            if stage_name == "Deployed" and not (out.get("department") or cfg.get("department_id") or out.get("custodian")):
                stage_name = "In stock"
                warnings.append("no department or person given, so it is recorded as In stock")
            out["stage"], out["condition"] = stage_name, condition
            dup = None
            if kind == "Asset" and not errors:
                dup, why = find_duplicate(out, cfg, seen_keys, row_no)
                if why:
                    warnings.append(why)
                if dup == 0:  # the same code or serial appeared earlier in this very file
                    dup, skip_reason = None, skip_reason or "Repeated inside this file"
            decision = "Skip" if (errors or skip_reason or (dup and kind == "Asset")) else "Create"
            if skip_reason and skip_reason not in warnings:
                warnings.append(skip_reason)
            db().execute("""INSERT INTO staged_rows(batch_id, row_no, raw_json, data_json, kind, errors_json, warnings_json, dup_asset_id, decision)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (b["id"], row_no, json.dumps(raw), json.dumps(out), kind, json.dumps(errors), json.dumps(warnings), dup, decision))
        db().execute("UPDATE import_batches SET status='Previewed', total_rows=? WHERE id=?", (total, b["id"]))
        db().commit()

    def find_duplicate(out, cfg, seen, row_no):
        """(existing asset id or None, message). Matches on tag, old code, old-system ID, serial; name+room is only a hint."""
        src = cfg.get("source_system", "")
        checks = []
        if out.get("tag"):
            checks.append(("tag", out["tag"], "SELECT id FROM assets WHERE tag=? AND archived_at IS NULL", (out["tag"],)))
        if out.get("legacy_code"):
            checks.append(("old code", out["legacy_code"],
                           """SELECT id FROM assets WHERE legacy_code=? AND archived_at IS NULL UNION SELECT a.id FROM asset_ids i JOIN assets a ON a.id=i.asset_id
                              WHERE i.kind='legacy_code' AND i.value=? AND a.archived_at IS NULL""", (out["legacy_code"],) * 2))
        if out.get("source_asset_id"):
            checks.append(("old-system ID", out["source_asset_id"],
                           """SELECT a.id FROM asset_ids i JOIN assets a ON a.id=i.asset_id WHERE i.kind='external' AND i.value=? AND i.source=?
                              AND a.archived_at IS NULL""", (out["source_asset_id"], src)))
        if out.get("serial_no"):
            checks.append(("serial number", out["serial_no"], "SELECT id FROM assets WHERE serial_no=? AND archived_at IS NULL", (out["serial_no"],)))
        for label, value, sql, args in checks:
            hit = q(sql, args, one=True)
            if hit:
                a = q("SELECT tag, name FROM assets WHERE id=?", (hit["id"],), one=True)
                return hit["id"], f"already in the register: same {label} as {a['tag']} {a['name']}"
            key = (label, value)
            if key in seen:
                return 0, f"same {label} as row {seen[key]} in this file"
            seen[key] = row_no
        weak = q("""SELECT a.tag FROM assets a LEFT JOIN rooms r ON r.id=a.room_id WHERE a.archived_at IS NULL AND a.name=? AND r.name IS ?
                    LIMIT 1""", (out.get("name"), out.get("room")), one=True)
        return None, (f"possible overlap: {weak['tag']} has the same name in the same room" if weak else None)

    # ---------- pages ----------

    def mapped(b):
        return bool(settings_of(b).get("columns"))

    @app.route("/imports")
    @role("admin")
    def imports():
        return render_template("imports.html", rows=q("""SELECT i.*, u.name by_name FROM import_batches i LEFT JOIN users u ON u.id=i.uploaded_by ORDER BY i.id DESC"""))

    @app.route("/imports/new", methods=["GET", "POST"])
    @role("admin")
    def import_new():
        if request.method == "POST":
            fh = request.files.get("file")
            name = (fh.filename or "") if fh else ""
            if not fh or not name:
                flash("Choose a CSV or Excel (.xlsx) file.", "error")
            elif not name.lower().endswith((".csv", ".xlsx")):
                flash("Only .csv and .xlsx files can be imported. Save older .xls files as .xlsx first.", "error")
            else:
                data = fh.read()
                if name.lower().endswith(".xlsx") and not data.startswith(b"PK"):
                    flash("That file isn't a real Excel (.xlsx) workbook.", "error")
                    return render_template("import_new.html")
                os.makedirs(IMPORT_DIR(), exist_ok=True)
                stored = secrets.token_hex(16) + (".xlsx" if name.lower().endswith(".xlsx") else ".csv")
                with open(os.path.join(IMPORT_DIR(), stored), "wb") as out:
                    out.write(data)
                digest = hashlib.sha256(data).hexdigest()
                same = q("SELECT id, uploaded_at FROM import_batches WHERE sha256=? AND status!='Reversed' ORDER BY id DESC LIMIT 1", (digest,), one=True)
                try:
                    names, rows = read_table(os.path.join(IMPORT_DIR(), stored), name)
                except Exception:
                    os.remove(os.path.join(IMPORT_DIR(), stored))
                    flash("That file couldn't be read. Check it opens in Excel, then try again.", "error")
                    return render_template("import_new.html")
                cur = db().execute("INSERT INTO import_batches(filename, stored_file, sha256, size, uploaded_by, header_row, sheet) VALUES (?,?,?,?,?,?,?)",
                                   (name, stored, digest, len(data), g.user["id"], guess_header_row(rows), names[0]))
                db().commit()
                if same:
                    flash(f"This exact file was already uploaded on {same['uploaded_at'][:10]} (import #{same['id']}). Anything already in the register will show as a duplicate.", "error")
                return redirect(url_for("import_map", bid=cur.lastrowid))
        return render_template("import_new.html")

    @app.route("/imports/<int:bid>/map", methods=["GET", "POST"])
    @role("admin")
    def import_map(bid):
        b = get_batch(bid)
        if b["status"] in ("Approved", "Reversed"):
            return redirect(url_for("import_preview", bid=bid))
        f = request.form
        if request.method == "POST":
            sheet = f.get("sheet") or b["sheet"]
            header_row = opt_int(f.get("header_row")) or b["header_row"]
            db().execute("UPDATE import_batches SET sheet=?, header_row=? WHERE id=?", (sheet, header_row, bid))
            b = get_batch(bid)
        names, rows = table_of(b)
        headers, data, hrow = split(b, rows)
        cfg = settings_of(b)
        reload_only = request.method == "POST" and f.get("action") == "reload"
        sig = "|".join(sorted(norm_header(h) for h in headers))
        known_map = q("SELECT * FROM import_mappings WHERE signature=?", (sig,), one=True)
        if request.method == "POST" and not reload_only:
            cols = {h: (f.get(f"col_{i}") if f.get(f"col_{i}") in TARGET_KEYS else "ignore") for i, h in enumerate(headers)}
            if not any(t in ("name", "product") for t in cols.values()):
                flash("Map one column to Asset name (or Product) so every row has a name.", "error")
            else:
                cfg = {"columns": cols, "department_id": opt_int(f.get("department_id")), "building_id": opt_int(f.get("building_id")),
                       "source_system": f.get("source_system", "").strip(), "acquisition_basis": f.get("acquisition_basis") or "Unknown",
                       "cost_basis": f.get("cost_basis") or "Unknown", "tax_included": {"1": 1, "0": 0}.get(f.get("tax_included")),
                       "default_stage": f.get("default_stage") if f.get("default_stage") in ("In stock", "Deployed") else "In stock",
                       "create_rooms": bool(f.get("create_rooms")), "expected_rows": opt_int(f.get("expected_rows")),
                       "expected_cost": f.get("expected_cost", "").strip()}
                save_settings(bid, cfg)
                if f.get("save_mapping", "").strip():
                    db().execute("INSERT INTO import_mappings(name, signature, columns_json, created_by) VALUES (?,?,?,?) "
                                 "ON CONFLICT(name) DO UPDATE SET signature=excluded.signature, columns_json=excluded.columns_json",
                                 (f["save_mapping"].strip(), sig, json.dumps(cols), g.user["id"]))
                db().execute("UPDATE import_batches SET status='Mapped' WHERE id=?", (bid,))
                db().commit()
                if unresolved(get_batch(bid)):
                    return redirect(url_for("import_values", bid=bid))
                stage(get_batch(bid))
                return redirect(url_for("import_preview", bid=bid))
        if cfg.get("columns") and not reload_only:
            current = {h: cfg["columns"].get(h, "ignore") for h in headers}
        elif known_map:
            saved_cols = json.loads(known_map["columns_json"])
            current = {h: saved_cols.get(h, "ignore") for h in headers}
            flash(f"Used your saved mapping '{known_map['name']}' for this layout.", "ok")
        else:
            current = guess_targets(headers)
        return render_template("import_map.html", b=b, step=2, sheets=names, headers=headers, current=current, preview=data[:8], hrow=hrow,
                               raw_head=rows[:12], cfg=cfg, targets=TARGETS, known_map=known_map, **A.lookups(),
                               ACQ_BASIS=A.ACQ_BASIS, COST_BASIS=A.COST_BASIS)

    @app.route("/imports/<int:bid>/values", methods=["GET", "POST"])
    @role("admin")
    def import_values(bid):
        b = get_batch(bid)
        if not mapped(b):
            return redirect(url_for("import_map", bid=bid))
        todo = unresolved(b)
        if request.method == "POST":
            for i, item in enumerate(todo):
                pick = request.form.get(f"v_{i}")
                if not pick:
                    continue
                if pick == "__new__":
                    pick = "__new__:" + item["value"]
                db().execute("INSERT INTO value_maps(kind, source, target) VALUES (?,?,?) ON CONFLICT(kind, source) DO UPDATE SET target=excluded.target",
                             (item["kind"], norm_value(item["value"]), pick))
            db().commit()
            if unresolved(b):
                flash("Some values still need an answer.", "error")
            else:
                stage(b)
                return redirect(url_for("import_preview", bid=bid))
            todo = unresolved(b)
        if not todo and request.method == "GET":
            stage(b)
            return redirect(url_for("import_preview", bid=bid))
        return render_template("import_values.html", b=b, step=3, todo=todo, categories=q("SELECT name FROM categories ORDER BY name"),
                               departments=q("SELECT name FROM departments ORDER BY name"), STATUS_CHOICES=STATUS_CHOICES)

    def summarize(b):
        rows = q("SELECT * FROM staged_rows WHERE batch_id=?", (b["id"],))
        s = {"read": len(rows), "create_assets": 0, "create_batches": 0, "skip": 0, "errors": 0, "dups": 0, "update": 0, "warnings": 0,
             "units": 0, "cost": 0.0, "batch_units": 0}
        for r in rows:
            d = json.loads(r["data_json"])
            if json.loads(r["errors_json"]):
                s["errors"] += 1
            if r["dup_asset_id"]:
                s["dups"] += 1
            if json.loads(r["warnings_json"]):
                s["warnings"] += 1
            if r["decision"] == "Create" and r["kind"] == "Asset":
                s["create_assets"] += 1
                s["cost"] += d.get("cost") or 0
            elif r["decision"] == "Create" and r["kind"] == "Batch":
                s["create_batches"] += 1
                s["batch_units"] += d["quantity"]
                s["cost"] += (d["cost"] or 0) * (d["quantity"] if settings_of(b).get("cost_basis") == "Per unit" else 1) + (d.get("cost_total") or 0)
            elif r["decision"] == "Update":
                s["update"] += 1
            else:
                s["skip"] += 1
        s["units"] = s["create_assets"] + s["batch_units"]
        return s

    @app.route("/imports/<int:bid>/preview", methods=["GET", "POST"])
    @role("admin")
    def import_preview(bid):
        b = get_batch(bid)
        if b["status"] in ("Uploaded", "Mapped"):
            return redirect(url_for("import_map", bid=bid))
        if request.method == "POST" and b["status"] == "Previewed":
            for r in q("SELECT id, kind, errors_json, dup_asset_id FROM staged_rows WHERE batch_id=?", (bid,)):
                pick = request.form.get(f"d_{r['id']}")
                if pick and not json.loads(r["errors_json"]) and pick in ("Create", "Skip", "Update"):
                    if pick == "Update" and not r["dup_asset_id"]:
                        continue
                    db().execute("UPDATE staged_rows SET decision=? WHERE id=?", (pick, r["id"]))
            db().commit()
            return redirect(url_for("import_preview", bid=bid, **{k: v for k, v in request.args.items()}))
        view = request.args.get("view", "all")
        sql = "SELECT * FROM staged_rows WHERE batch_id=?"
        if view == "errors":
            sql += " AND errors_json!='[]'"
        elif view == "dups":
            sql += " AND dup_asset_id IS NOT NULL"
        elif view == "warn":
            sql += " AND warnings_json!='[]'"
        elif view == "create":
            sql += " AND decision='Create'"
        elif view == "skip":
            sql += " AND decision='Skip'"
        sql += " ORDER BY row_no"
        rows, pg = A.paginate(sql, (bid,))
        shown = [{"r": r, "d": json.loads(r["data_json"]), "errors": json.loads(r["errors_json"]), "warnings": json.loads(r["warnings_json"]),
                  "dup": q("SELECT id, tag, name FROM assets WHERE id=?", (r["dup_asset_id"],), one=True) if r["dup_asset_id"] else None} for r in rows]
        cfg = settings_of(b)
        s = summarize(b)
        checks = []
        if cfg.get("expected_rows"):
            checks.append(("Units to be created", cfg["expected_rows"], s["units"]))
        if cfg.get("expected_cost"):
            try:
                checks.append(("Total cost", parse_number(cfg["expected_cost"]), s["cost"]))
            except ValueError:
                pass
        return render_template("import_preview.html", b=b, step=4, rows=shown, pg=pg, s=s, view=view, cfg=cfg, checks=checks,
                               created=q("SELECT COUNT(*) n FROM assets WHERE import_batch_id=?", (bid,), one=True)["n"])

    @app.route("/imports/<int:bid>/errors.csv")
    @role("admin")
    def import_errors(bid):
        b = get_batch(bid)
        _, rows = table_of(b)
        headers, _, _ = split(b, rows)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["row", "decision", "problems", *headers])
        for r in q("SELECT * FROM staged_rows WHERE batch_id=? AND (errors_json!='[]' OR warnings_json!='[]' OR decision!='Create') ORDER BY row_no", (bid,)):
            raw = json.loads(r["raw_json"])
            w.writerow([r["row_no"], r["decision"], "; ".join(json.loads(r["errors_json"]) + json.loads(r["warnings_json"])), *[raw.get(h, "") for h in headers]])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=import-{bid}-to-review.csv"})

    @app.route("/imports/<int:bid>/original")
    @role("admin")
    def import_original(bid):
        b = get_batch(bid)
        return send_file(os.path.join(IMPORT_DIR(), b["stored_file"]), as_attachment=True, download_name=b["filename"])

    # ---------- approve ----------

    def get_or_create_room(default_building_id, building_name, room_name, create):
        if not room_name:
            return None
        if building_name:
            if create:
                db().execute("INSERT OR IGNORE INTO buildings(name) VALUES (?)", (building_name,))
            b_ = q("SELECT id FROM buildings WHERE name=?", (building_name,), one=True)
            bid_ = b_["id"] if b_ else None
        else:
            bid_ = default_building_id
            if not bid_ and create:
                db().execute("INSERT OR IGNORE INTO buildings(name) VALUES ('Imported (building not given)')")
                bid_ = q("SELECT id FROM buildings WHERE name='Imported (building not given)'", one=True)["id"]
        if not bid_:
            return None
        if create:
            db().execute("INSERT OR IGNORE INTO rooms(building_id, name, type) VALUES (?,?, 'Other')", (bid_, room_name))
        r_ = q("SELECT id FROM rooms WHERE building_id=? AND name=?", (bid_, room_name), one=True)
        return r_["id"] if r_ else None

    def dept_id_for(name, new):
        if not name:
            return None
        if new:
            db().execute("INSERT OR IGNORE INTO departments(name) VALUES (?)", (name,))
        r = q("SELECT id FROM departments WHERE name=?", (name,), one=True)
        return r["id"] if r else None

    def category_id_for(name, new):
        if not name:
            return None
        if new:
            db().execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        r = q("SELECT id FROM categories WHERE name=?", (name,), one=True)
        return r["id"] if r else None

    def apply_row(b, cfg, sr):
        d = json.loads(sr["data_json"])
        dept = dept_id_for(d.get("department"), d.get("department_new")) or cfg.get("department_id")
        cat = category_id_for(d.get("category"), d.get("category_new"))
        room = get_or_create_room(cfg.get("building_id"), d.get("building"), d.get("room"), cfg.get("create_rooms", True))
        raw = sr["raw_json"]
        if sr["kind"] == "Batch":
            cost, qty = d.get("cost"), d["quantity"]
            unit = cost if cfg.get("cost_basis") == "Per unit" else (cost / qty if cost is not None and cfg.get("cost_basis") == "Line total" else None)
            total = (cost * qty if cfg.get("cost_basis") == "Per unit" else cost) if cost is not None else d.get("cost_total")
            cur = db().execute("""INSERT INTO purchase_batches(import_batch_id, description, category_id, department_id, quantity, unit_cost, total_cost, cost_basis,
                                  tax_included, vendor, po_number, invoice_number, purchase_date, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (b["id"], d["name"], cat, dept, qty, unit, total, cfg.get("cost_basis"), cfg.get("tax_included"), d.get("vendor"),
                                d.get("po_number"), d.get("invoice_number"), d.get("purchase_date"), d.get("notes")))
            db().execute("UPDATE staged_rows SET purchase_batch_id=? WHERE id=?", (cur.lastrowid, sr["id"]))
            return "batch"
        if sr["decision"] == "Update" and sr["dup_asset_id"]:
            a = q("SELECT * FROM assets WHERE id=?", (sr["dup_asset_id"],), one=True)
            fill = {"category_id": cat, "room_id": room, "brand": d.get("brand"), "model": d.get("model"), "serial_no": d.get("serial_no"),
                    "ip_address": d.get("ip_address"), "mac_address": d.get("mac_address"), "purchase_date": d.get("purchase_date"),
                    "warranty_until": d.get("warranty_until"), "cost": d.get("cost"), "vendor": d.get("vendor"), "managing_dept_id": dept,
                    "owning_dept_id": dept, "po_number": d.get("po_number"), "invoice_number": d.get("invoice_number"), "indent_no": d.get("indent_no"),
                    "cpr_no": d.get("cpr_no"), "grn_no": d.get("grn_no"), "stock_register_ref": d.get("stock_register_ref"), "legacy_code": d.get("legacy_code")}
            changed = [k for k, v in fill.items() if v is not None and (a[k] is None or a[k] == "")]
            for k in changed:
                db().execute(f"UPDATE assets SET {k}=? WHERE id=?", (fill[k], a["id"]))
            if d.get("source_asset_id"):
                db().execute("INSERT OR IGNORE INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)",
                             (a["id"], "external", d["source_asset_id"], cfg.get("source_system", ""), g.user["id"]))
            A.log(a["id"], f"Blank fields filled from import #{b['id']} (row {sr['row_no']}): {', '.join(changed) or 'nothing new'}", etype="Imported",
                  event_date=date.today().isoformat())
            return "update"
        parent = q("SELECT * FROM assets WHERE tag=?", (d["parent_tag"],), one=True) if d.get("parent_tag") else None
        if parent:
            room, dept = parent["room_id"], parent["managing_dept_id"]
        tag = d.get("tag") or A.next_tag()
        stage_name = d["stage"]
        cur = db().execute(
            """INSERT INTO assets(tag, name, category_id, room_id, parent_id, lifecycle_stage, condition, brand, model, serial_no, ip_address, mac_address,
               purchase_date, warranty_until, cost, vendor, notes, managing_dept_id, owning_dept_id, acquisition_basis, po_number, invoice_number,
               indent_no, cpr_no, grn_no, stock_register_ref, cost_basis, tax_included, legacy_code, location_unresolved, import_batch_id, source_row, source_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tag, d["name"], cat, room, parent["id"] if parent else None, stage_name, d["condition"], d.get("brand"), d.get("model"), d.get("serial_no"),
             d.get("ip_address"), d.get("mac_address"), d.get("purchase_date"), d.get("warranty_until"), d.get("cost"), d.get("vendor"), d.get("notes"),
             dept, dept, cfg.get("acquisition_basis", "Unknown"), d.get("po_number"), d.get("invoice_number"), d.get("indent_no"), d.get("cpr_no"),
             d.get("grn_no"), d.get("stock_register_ref"), cfg.get("cost_basis") if d.get("cost") is not None else None,
             cfg.get("tax_included") if d.get("cost") is not None else None, d.get("legacy_code"), 0 if room else 1, b["id"], sr["row_no"], raw))
        aid = cur.lastrowid
        if d.get("legacy_code"):
            db().execute("INSERT OR IGNORE INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)",
                         (aid, "legacy_code", d["legacy_code"], cfg.get("source_system", ""), g.user["id"]))
        if d.get("source_asset_id"):
            db().execute("INSERT OR IGNORE INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)",
                         (aid, "external", d["source_asset_id"], cfg.get("source_system", ""), g.user["id"]))
        started = d.get("custody_date") or date.today().isoformat()
        if stage_name == "Deployed" and not parent:
            holder = "Person" if d.get("custodian") else "Department"
            db().execute("""INSERT INTO custody(asset_id, holder_type, department_id, person, started_on, reason, started_by) VALUES (?,?,?,?,?,?,?)""",
                         (aid, holder, dept, d.get("custodian"), started, f"Imported allotment (import #{b['id']})", g.user["id"]))
            db().execute("UPDATE assets SET using_dept_id=?, custodian=? WHERE id=?", (dept, d.get("custodian"), aid))
        elif stage_name == "In stock" and not parent:
            db().execute("INSERT INTO custody(asset_id, holder_type, started_on, reason, started_by) VALUES (?,?,?,?,?)",
                         (aid, "Stockroom", started, f"Imported (import #{b['id']})", g.user["id"]))
        A.sync_status(aid)
        A.log(aid, f"Imported from {b['filename']} (import #{b['id']}, row {sr['row_no']})", etype="Imported", event_date=d.get("custody_date"))
        db().execute("UPDATE staged_rows SET asset_id=? WHERE id=?", (aid, sr["id"]))
        return "asset"

    @app.route("/imports/<int:bid>/approve", methods=["POST"])
    @role("admin")
    def import_approve(bid):
        b = get_batch(bid)
        if b["status"] != "Previewed":
            flash("This import was already approved or reversed.", "error")
            return redirect(url_for("import_preview", bid=bid))
        if not request.form.get("reviewed"):
            flash("Tick the box to confirm you reviewed the exceptions.", "error")
            return redirect(url_for("import_preview", bid=bid))
        cfg = settings_of(b)
        n = {"asset": 0, "batch": 0, "update": 0}
        try:
            for sr in q("SELECT * FROM staged_rows WHERE batch_id=? AND decision IN ('Create','Update') ORDER BY row_no", (bid,)):
                n[apply_row(b, cfg, sr)] += 1
            db().execute("""UPDATE import_batches SET status='Approved', created_n=?, skipped_n=(SELECT COUNT(*) FROM staged_rows WHERE batch_id=? AND decision='Skip'),
                            error_n=(SELECT COUNT(*) FROM staged_rows WHERE batch_id=? AND errors_json!='[]'), approved_by=?, approved_at=datetime('now') WHERE id=?""",
                         (n["asset"] + n["batch"], bid, bid, g.user["id"], bid))
            db().commit()
        except Exception:
            db().rollback()
            raise
        flash(f"Imported: {n['asset']} asset(s), {n['batch']} purchase batch(es) waiting for units to be confirmed, {n['update']} updated.", "ok")
        return redirect(url_for("import_preview", bid=bid))

    @app.route("/imports/<int:bid>/reverse", methods=["POST"])
    @role("admin")
    def import_reverse(bid):
        """Undo an import only if nothing has happened to its assets since. Otherwise archive them one by one with a reason."""
        b = get_batch(bid)
        if b["status"] != "Approved":
            abort(404)
        assets = q("SELECT * FROM assets WHERE import_batch_id=? AND archived_at IS NULL", (bid,))
        busy = []
        for a in assets:
            extra = q("""SELECT (SELECT COUNT(*) FROM activity WHERE asset_id=? AND (event_type IS NULL OR event_type NOT IN ('Imported'))) +
                         (SELECT COUNT(*) FROM issues WHERE asset_id=?) + (SELECT COUNT(*) FROM services WHERE asset_id=?) +
                         (SELECT COUNT(*) FROM photos WHERE asset_id=?) + (SELECT COUNT(*) FROM assets WHERE parent_id=?) +
                         (SELECT COUNT(*) FROM custody WHERE asset_id=? AND ended_at IS NOT NULL) n""", (a["id"],) * 6, one=True)["n"]
            if extra:
                busy.append(a["tag"])
        if busy:
            flash(f"Can't reverse: {len(busy)} imported asset(s) have been used since ({', '.join(busy[:5])}). Archive them one by one with a reason instead.", "error")
            return redirect(url_for("import_preview", bid=bid))
        for a in assets:
            note = f"Old code was {a['legacy_code']}. " if a["legacy_code"] else ""
            db().execute("DELETE FROM asset_ids WHERE asset_id=?", (a["id"],))
            db().execute("""UPDATE assets SET archived_at=datetime('now'), archive_reason=?, legacy_code=NULL, serial_no=NULL, using_dept_id=NULL, custodian=NULL,
                            notes=? WHERE id=?""", (f"Import #{bid} reversed", (note + (a["notes"] or "")).strip() or None, a["id"]))
            db().execute("UPDATE custody SET ended_on=date('now'), ended_at=datetime('now'), ended_by=?, end_reason='Import reversed' WHERE asset_id=? AND ended_at IS NULL",
                         (g.user["id"], a["id"]))
            A.sync_status(a["id"])
            A.log(a["id"], f"Archived: import #{bid} was reversed", etype="Archived")
        db().execute("DELETE FROM purchase_batches WHERE import_batch_id=? AND units_created=0", (bid,))
        db().execute("UPDATE import_batches SET status='Reversed', reversed_at=datetime('now') WHERE id=?", (bid,))
        db().commit()
        flash("Import reversed. The assets are archived (their tags stay used) and the file can be corrected and uploaded again.", "ok")
        return redirect(url_for("imports"))

    # ---------- purchase batches: "20 computers" until the units are confirmed ----------

    @app.route("/batches")
    @role("admin", "purchase")
    def batches():
        return render_template("batches.html", rows=q("""SELECT p.*, c.name cat, d.name dept FROM purchase_batches p LEFT JOIN categories c ON c.id=p.category_id
                                                        LEFT JOIN departments d ON d.id=p.department_id ORDER BY p.id DESC"""), **A.lookups())

    @app.route("/batches/<int:pid>/units", methods=["POST"])
    @role("admin")
    def batch_units(pid):
        p = q("SELECT * FROM purchase_batches WHERE id=?", (pid,), one=True)
        if not p:
            abort(404)
        left = p["quantity"] - p["units_created"]
        n = opt_int(request.form.get("count")) or 0
        room = opt_int(request.form.get("room_id"))
        if not 1 <= n <= left:
            flash(f"Enter how many units have really arrived and been accepted (1 to {left}).", "error")
        elif not p["department_id"] and not opt_int(request.form.get("department_id")):
            flash("Choose the managing department.", "error")
        else:
            dept = p["department_id"] or opt_int(request.form.get("department_id"))
            for i in range(n):
                tag = A.next_tag()
                cur = db().execute("""INSERT INTO assets(tag, name, category_id, room_id, lifecycle_stage, condition, managing_dept_id, owning_dept_id, cost, cost_basis, tax_included,
                                      vendor, po_number, invoice_number, purchase_date, acquisition_basis, location_unresolved, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (tag, p["description"], p["category_id"], room, "In stock", "Working", dept, dept, p["unit_cost"], "Per unit" if p["unit_cost"] is not None else None,
                                    p["tax_included"] if p["unit_cost"] is not None else None, p["vendor"], p["po_number"], p["invoice_number"], p["purchase_date"],
                                    "Purchase", 0 if room else 1, f"Created from purchase batch #{pid}"))
                db().execute("INSERT INTO custody(asset_id, holder_type, started_on, reason, started_by) VALUES (?,?,date('now'),?,?)",
                             (cur.lastrowid, "Stockroom", f"Accepted from purchase batch #{pid}", g.user["id"]))
                A.sync_status(cur.lastrowid)
                A.log(cur.lastrowid, f"Created from purchase batch #{pid}: unit {p['units_created'] + i + 1} of {p['quantity']}", etype="Created")
            done = p["units_created"] + n
            db().execute("UPDATE purchase_batches SET units_created=?, status=? WHERE id=?", (done, "Units created" if done >= p["quantity"] else "Partly received", pid))
            db().commit()
            flash(f"{n} unit(s) created with their own tags. {p['quantity'] - done} still to come.", "ok")
        return redirect(url_for("batches"))

    @app.route("/assets/import", methods=["GET", "POST"])
    @role("admin")
    def asset_import():
        """The old one-step CSV import is now the wizard; the CSV template for our own format stays."""
        if request.args.get("template"):
            return A.csv_response("asset-import-template.csv", A.IMPORT_COLS, [
                ["", "L3-PC-01", "Desktop computer", "CSE Block", "Computer Lab 3", "Working", "Dell", "OptiPlex 7090", "SN123", "10.10.3.11", "",
                 "2024-06-01", "2027-06-01", "52000", "Vendor name", "", "", "CSE"],
                ["", "8 GB RAM", "RAM", "CSE Block", "Computer Lab 3", "Working", "Crucial", "", "", "", "", "", "", "", "", "", "", "CSE"]])
        return redirect(url_for("import_new"))


def opt_int(v):
    return int(v) if v and str(v).isdigit() else None
