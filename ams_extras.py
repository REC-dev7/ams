"""Extra fields per asset type, consumable stock, software licences and reports.

Kept apart from the physical-equipment record on purpose: toner is counted, software is a right to use, a computer is an individual.
"""
import csv
import io
import re
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

DATATYPES = {"text": "Text", "number": "Number", "date": "Date", "choice": "Choice from a list"}
# starting fields so a computer asks for CPU and RAM and a switch for ports. Admins can change them in Settings.
DEFAULT_FIELDS = {
    "Desktop computer": [("CPU", "text", None, None), ("RAM", "number", "GB", None), ("Storage", "number", "GB", None), ("Operating system", "text", None, None)],
    "Laptop": [("CPU", "text", None, None), ("RAM", "number", "GB", None), ("Storage", "number", "GB", None), ("Operating system", "text", None, None)],
    "Server": [("CPU", "text", None, None), ("RAM", "number", "GB", None), ("Storage", "number", "GB", None), ("Operating system", "text", None, None)],
    "Network switch": [("Ports", "number", None, None), ("Managed", "choice", None, "Yes,No"), ("Uplink speed", "choice", None, "1G,10G,25G")],
    "Router": [("Ports", "number", None, None), ("Firmware", "text", None, None)],
    "Wi-Fi access point": [("Wi-Fi standard", "choice", None, "Wi-Fi 5,Wi-Fi 6,Wi-Fi 6E")],
    "Projector": [("Brightness", "number", "lumens", None), ("Inputs", "text", None, None)],
    "Printer": [("Type", "choice", None, "Laser,Inkjet,Dot matrix"), ("Duplex", "choice", None, "Yes,No")],
    "UPS": [("Capacity", "number", "kVA", None), ("Battery count", "number", None, None)],
    "Lab instrument": [("Measurement range", "text", None, None), ("Calibration due", "date", None, None)],
    "Monitor": [("Screen size", "number", "inch", None)],
}
STOCK_KINDS = ["Received", "Issued", "Adjusted"]
LICENCE_TYPES = ["Perpetual", "Subscription", "Enterprise agreement", "Open source", "Academic", "Trial", "Other"]
LICENCE_METRICS = ["Per device", "Per user", "Concurrent user", "Per server", "Site", "Other"]


def seed_attributes(conn):
    """Add the starting fields once (only while no field exists yet)."""
    if conn.execute("SELECT 1 FROM attribute_defs LIMIT 1").fetchone():
        return
    for cat, fields in DEFAULT_FIELDS.items():
        row = conn.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()
        for i, (name, kind, unit, choices) in enumerate(fields):
            if row:
                conn.execute("INSERT OR IGNORE INTO attribute_defs(category_id, name, datatype, unit, choices, sort) VALUES (?,?,?,?,?,?)",
                             (row[0], name, kind, unit, choices, i))


def init(A):
    q, db, app, role = A.q, A.db, A.app, A.role

    # ---------- fields per asset type ----------

    def defs_json():
        """{category id: [field...]} for the asset form, which shows the fields of the chosen category."""
        out = {}
        for d in q("SELECT * FROM attribute_defs ORDER BY category_id, sort, id"):
            out.setdefault(d["category_id"], []).append({"id": d["id"], "name": d["name"], "type": d["datatype"], "unit": d["unit"],
                                                       "choices": [c.strip() for c in (d["choices"] or "").split(",") if c.strip()], "required": bool(d["required"])})
        return out

    def values_for(aid):
        return {r["attr_id"]: r["value"] for r in q("SELECT attr_id, value FROM attribute_values WHERE asset_id=?", (aid,))}

    def specs_for(aid):
        return q("""SELECT d.name, v.value, d.unit, d.datatype FROM attribute_values v JOIN attribute_defs d ON d.id=v.attr_id
                    WHERE v.asset_id=? ORDER BY d.sort, d.id""", (aid,))

    def save_attributes(aid, category_id, form):
        """Check and store the fields for this asset's type. Raises ValueError with a plain message."""
        defs = q("SELECT * FROM attribute_defs WHERE category_id=?", (category_id,)) if category_id else []
        keep = set()
        for d in defs:
            raw = (form.get(f"attr_{d['id']}") or "").strip()
            if not raw:
                if d["required"]:
                    raise ValueError(f"{d['name']} is required for this kind of asset")
                continue
            if d["datatype"] == "number":
                try:
                    num = float(raw)
                except ValueError:
                    raise ValueError(f"{d['name']} must be a number" + (f" in {d['unit']}" if d["unit"] else ""))
                raw = str(int(num)) if num.is_integer() else str(num)
            elif d["datatype"] == "date":
                try:
                    raw = date.fromisoformat(raw).isoformat()
                except ValueError:
                    raise ValueError(f"{d['name']} must be a date")
            elif d["datatype"] == "choice" and raw not in [c.strip() for c in (d["choices"] or "").split(",")]:
                raise ValueError(f"{d['name']} must be one of: {d['choices']}")
            db().execute("INSERT INTO attribute_values(asset_id, attr_id, value) VALUES (?,?,?) ON CONFLICT(asset_id, attr_id) DO UPDATE SET value=excluded.value",
                         (aid, d["id"], raw))
            keep.add(d["id"])
        db().execute(f"DELETE FROM attribute_values WHERE asset_id=? AND attr_id NOT IN ({','.join('?' * len(keep)) or 'NULL'})", [aid, *keep])

    A.save_attributes = save_attributes
    app.jinja_env.globals.update(attribute_defs=defs_json, attribute_values=values_for, asset_specs=specs_for)

    @app.route("/settings/fields")
    @role("admin")
    def fields():
        cats = q("""SELECT c.id, c.name, (SELECT COUNT(*) FROM assets a WHERE a.category_id=c.id) n FROM categories c ORDER BY c.name""")
        defs = {}
        for d in q("""SELECT d.*, (SELECT COUNT(*) FROM attribute_values v WHERE v.attr_id=d.id) used FROM attribute_defs d ORDER BY d.category_id, d.sort, d.id"""):
            defs.setdefault(d["category_id"], []).append(d)
        return render_template("fields.html", cats=cats, defs=defs, DATATYPES=DATATYPES)

    @app.route("/settings/fields/add", methods=["POST"])
    @role("admin")
    def field_add():
        f = request.form
        cid, name = A.opt_int(f.get("category_id")), f.get("name", "").strip()
        kind = f.get("datatype") if f.get("datatype") in DATATYPES else "text"
        choices = ",".join(c.strip() for c in f.get("choices", "").split(",") if c.strip())
        if not cid or not name or (kind == "choice" and not choices):
            flash("Choose the category, name the field, and list the choices for a choice field.", "error")
        else:
            top = q("SELECT COALESCE(MAX(sort), -1) + 1 n FROM attribute_defs WHERE category_id=?", (cid,), one=True)["n"]
            try:
                db().execute("INSERT INTO attribute_defs(category_id, name, datatype, unit, choices, required, sort) VALUES (?,?,?,?,?,?,?)",
                             (cid, name, kind, f.get("unit", "").strip() or None, choices or None, 1 if f.get("required") else 0, top))
                db().commit()
                flash("Field added.", "ok")
            except A.sqlite3.IntegrityError:
                flash("That category already has a field with this name.", "error")
        return redirect(url_for("fields"))

    @app.route("/settings/fields/<int:did>/delete", methods=["POST"])
    @role("admin")
    def field_delete(did):
        n = q("SELECT COUNT(*) n FROM attribute_values WHERE attr_id=?", (did,), one=True)["n"]
        if n and not request.form.get("sure"):
            flash(f"{n} asset(s) have a value in this field. Tick the box to delete the field and those values.", "error")
        else:
            db().execute("DELETE FROM attribute_defs WHERE id=?", (did,))
            db().commit()
        return redirect(url_for("fields"))

    # ---------- consumable stock ----------

    def stock_scope():
        ids = A.dept_ids()
        if ids is None:
            return "", []
        if not ids:
            return " AND s.department_id IS NULL", []
        return f" AND (s.department_id IS NULL OR s.department_id IN ({','.join('?' * len(ids))}))", [*ids]

    BAL = "(SELECT COALESCE(SUM(m.qty), 0) FROM stock_movements m WHERE m.item_id=s.id)"

    @app.route("/stock")
    def stock():
        sw, sa = stock_scope()
        rows = q(f"""SELECT s.*, d.name dept, r.name room, {BAL} balance FROM stock_items s LEFT JOIN departments d ON d.id=s.department_id
                     LEFT JOIN rooms r ON r.id=s.room_id WHERE s.archived_at IS NULL{sw} ORDER BY (balance <= s.min_level) DESC, s.name""", sa)
        return render_template("stock.html", rows=rows, **A.lookups())

    @app.route("/stock/new", methods=["POST"])
    @role("edit")
    def stock_new():
        f = request.form
        name = f.get("name", "").strip()
        try:
            min_level = float(f.get("min_level") or 0)
        except ValueError:
            min_level = -1
        if not name or min_level < 0:
            flash("Give the item a name and a minimum level of 0 or more.", "error")
        else:
            cur = db().execute("INSERT INTO stock_items(name, unit, department_id, room_id, min_level, notes) VALUES (?,?,?,?,?,?)",
                               (name, f.get("unit", "").strip() or "nos", A.opt_int(f.get("department_id")), A.opt_int(f.get("room_id")), min_level, f.get("notes", "").strip() or None))
            db().commit()
            flash("Item added. Record what you have as 'Received'.", "ok")
            return redirect(url_for("stock_item", iid=cur.lastrowid))
        return redirect(url_for("stock"))

    def get_item(iid):
        sw, sa = stock_scope()
        s = q(f"SELECT s.*, d.name dept, r.name room, {BAL} balance FROM stock_items s LEFT JOIN departments d ON d.id=s.department_id LEFT JOIN rooms r ON r.id=s.room_id WHERE s.id=?{sw}",
              [iid, *sa], one=True)
        if not s:
            abort(404)
        return s

    @app.route("/stock/<int:iid>")
    def stock_item(iid):
        s = get_item(iid)
        moves = q("""SELECT m.*, u.name who, d.name dept FROM stock_movements m LEFT JOIN users u ON u.id=m.user_id LEFT JOIN departments d ON d.id=m.department_id
                     WHERE m.item_id=? ORDER BY m.id DESC LIMIT 100""", (iid,))
        return render_template("stock_item.html", s=s, moves=moves, STOCK_KINDS=STOCK_KINDS, **A.lookups())

    @app.route("/stock/<int:iid>/move", methods=["POST"])
    @role("move")
    def stock_move(iid):
        s = get_item(iid)
        f = request.form
        try:
            kind = f.get("kind")
            if kind not in STOCK_KINDS:
                raise ValueError("Choose Received, Issued or Adjusted.")
            try:
                qty = float(f.get("qty") or 0)
            except ValueError:
                raise ValueError("Quantity must be a number.")
            if qty <= 0:
                raise ValueError("Quantity must be more than 0.")
            reason = (f.get("reason") or "").strip()
            if kind != "Received" and not reason:
                raise ValueError("Say why (who needed it, or what the count found).")
            signed = qty if kind == "Received" or (kind == "Adjusted" and f.get("direction") != "down") else -qty
            db().execute("INSERT INTO stock_movements(item_id, qty, kind, event_date, user_id, department_id, reason, reference) VALUES (?,?,?,?,?,?,?,?)",
                         (iid, signed, kind, A.parse_date(f.get("event_date")), g.user["id"], A.opt_int(f.get("department_id")), reason or None, (f.get("reference") or "").strip() or None))
            if q("SELECT COALESCE(SUM(qty), 0) b FROM stock_movements WHERE item_id=?", (iid,), one=True)["b"] < 0:
                raise ValueError(f"Only {s['balance']:g} {s['unit']} in stock.")  # checked after the write, so two people can't both take the last ones
            db().commit()
            flash(f"{kind}: {qty:g} {s['unit']}.", "ok")
        except ValueError as e:
            db().rollback()
            flash(str(e), "error")
        return redirect(url_for("stock_item", iid=iid))

    # ---------- software licences ----------

    def lic_scope():
        ids = A.dept_ids()
        if ids is None:
            return "", []
        if not ids:
            return " AND l.department_id IS NULL", []
        return f" AND (l.department_id IS NULL OR l.department_id IN ({','.join('?' * len(ids))}))", [*ids]

    @app.route("/licences")
    @role("docs")
    def licences():
        sw, sa = lic_scope()
        soon = (date.today() + timedelta(days=60)).isoformat()
        rows = q(f"""SELECT l.*, d.name dept FROM licences l LEFT JOIN departments d ON d.id=l.department_id WHERE l.archived_at IS NULL{sw}
                     ORDER BY l.renewal_date IS NULL, l.renewal_date, l.product""", sa)
        if request.args.get("csv"):
            show_keys = A.allowed("purchase")
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["product", "vendor", "type", "metric", "quantity", "in_use", "start", "renewal", "support_end", "department", "po"] + (["key"] if show_keys else []))
            for r in rows:
                w.writerow([r["product"], r["vendor"], r["licence_type"], r["metric"], r["quantity"], r["in_use"], r["start_date"], r["renewal_date"], r["support_end"], r["dept"], r["po_number"]]
                           + ([r["key_ref"]] if show_keys else []))
            return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=licences.csv"})
        return render_template("licences.html", rows=rows, soon=soon, LICENCE_TYPES=LICENCE_TYPES, LICENCE_METRICS=LICENCE_METRICS, **A.lookups())

    def licence_values():
        f = request.form
        product = f.get("product", "").strip()
        if not product:
            raise ValueError("Product name is required.")
        qty = A.opt_int(f.get("quantity"))
        if qty is None or qty < 1:
            raise ValueError("Quantity must be 1 or more.")
        dates = []
        for k in ("start_date", "renewal_date", "support_end"):
            v = (f.get(k) or "").strip() or None
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise ValueError("Dates must be year-month-day.")
            dates.append(v)
        try:
            cost = float(f["cost"]) if f.get("cost") else None
        except ValueError:
            raise ValueError("Cost must be a number.")
        return [product, f.get("vendor", "").strip() or None, f.get("licence_type") if f.get("licence_type") in LICENCE_TYPES else "Other",
                f.get("metric") if f.get("metric") in LICENCE_METRICS else "Other", qty, A.opt_int(f.get("in_use")), *dates, cost,
                f.get("po_number", "").strip() or None, A.opt_int(f.get("department_id")), f.get("notes", "").strip() or None]

    @app.route("/licences/add", methods=["POST"])
    @role("purchase")
    def licence_add():
        try:
            v = licence_values()
            db().execute("""INSERT INTO licences(product, vendor, licence_type, metric, quantity, in_use, start_date, renewal_date, support_end, cost, po_number,
                            department_id, notes, key_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [*v, request.form.get("key_ref", "").strip() or None])
            db().commit()
            flash("Licence added.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("licences"))

    @app.route("/licences/<int:lid>/update", methods=["POST"])
    @role("purchase")
    def licence_update(lid):
        if not q("SELECT 1 FROM licences WHERE id=?", (lid,), one=True):
            abort(404)
        try:
            v = licence_values()
            db().execute("""UPDATE licences SET product=?, vendor=?, licence_type=?, metric=?, quantity=?, in_use=?, start_date=?, renewal_date=?, support_end=?, cost=?,
                            po_number=?, department_id=?, notes=? WHERE id=?""", [*v, lid])
            if request.form.get("key_ref", "").strip():
                db().execute("UPDATE licences SET key_ref=? WHERE id=?", (request.form["key_ref"].strip(), lid))
            db().commit()
            flash("Licence updated.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("licences"))

    @app.route("/licences/<int:lid>/archive", methods=["POST"])
    @role("purchase")
    def licence_archive(lid):
        db().execute("UPDATE licences SET archived_at=datetime('now') WHERE id=?", (lid,))
        db().commit()
        return redirect(url_for("licences"))

    # ---------- reports ----------

    def report_data(limit=15):
        sw, sa = A.scope_sql()
        live = "a.status != 'Scrapped' AND a.archived_at IS NULL"

        def count(by, join="", where=""):
            return q(f"""SELECT {by} k, COUNT(*) n FROM assets a {join} WHERE {live}{where}{sw} AND a.parent_id IS NULL GROUP BY k ORDER BY n DESC""", sa)

        cut = (date.today() - timedelta(days=A.AUDIT_DAYS)).isoformat()
        one = lambda cond, *extra: q(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.parent_id IS NULL AND {cond}{sw}", [*extra, *sa], one=True)["n"]
        flow = {"In stock": one("a.lifecycle_stage='In stock'"), "Issued (deployed)": one("a.lifecycle_stage='Deployed'"),
                "Under maintenance": one("a.lifecycle_stage='Under maintenance'"), "Faulty": one("a.condition='Faulty'"),
                "Waiting for retirement or disposal": one("a.lifecycle_stage IN ('Retirement proposed','Retirement approved','Awaiting disposal')"),
                "Not verified in 12 months": one("(a.last_audit IS NULL OR a.last_audit < ?)", cut)}
        quality = {
            "No managing department": ("a.managing_dept_id IS NULL", []), "Location unknown": ("(a.room_id IS NULL OR a.location_unresolved=1)", []),
            "No category": ("a.category_id IS NULL", []), "Acquisition basis unknown": ("a.acquisition_basis='Unknown'", []),
            "Bought but no PO or invoice number": ("a.acquisition_basis='Purchase' AND a.po_number IS NULL AND a.invoice_number IS NULL", []),
            "Same serial number as another asset": ("a.serial_no IS NOT NULL AND a.serial_no IN (SELECT serial_no FROM assets WHERE archived_at IS NULL AND serial_no IS NOT NULL GROUP BY serial_no HAVING COUNT(*) > 1)", []),
            "Same name, room and category as another asset": ("""EXISTS (SELECT 1 FROM assets b WHERE b.id != a.id AND b.archived_at IS NULL AND b.parent_id IS NULL AND b.name=a.name
                                 AND b.room_id IS a.room_id AND b.category_id IS a.category_id)""", [])}
        qrows = {}
        for label, (cond, extra) in quality.items():
            qrows[label] = (one(cond, *extra), q(f"SELECT a.id, a.tag, a.name FROM assets a WHERE {live} AND a.parent_id IS NULL AND {cond}{sw} ORDER BY a.id" + (f" LIMIT {limit}" if limit else ""), [*extra, *sa]))
        vals = q(f"SELECT a.cost, a.purchase_date, c.life_years, a.cost_basis FROM assets a LEFT JOIN categories c ON c.id=a.category_id WHERE {live} AND a.cost IS NOT NULL{sw}", sa)
        imports = q("""SELECT i.id, i.filename, i.status, i.total_rows, i.created_n, i.skipped_n, i.error_n,
                       (SELECT COUNT(*) FROM assets a WHERE a.import_batch_id=i.id AND a.archived_at IS NULL) live_now,
                       (SELECT COALESCE(SUM(p.quantity - p.units_created), 0) FROM purchase_batches p WHERE p.import_batch_id=i.id) units_waiting
                       FROM import_batches i ORDER BY i.id DESC LIMIT 10""") if A.allowed("admin") or A.allowed("purchase") or A.has_role("auditor") else []
        return {"by_dept": count("COALESCE(md.name, 'No department')", "LEFT JOIN departments md ON md.id=a.managing_dept_id"),
                "by_cat": count("COALESCE(c.name, 'No category')", "LEFT JOIN categories c ON c.id=a.category_id"),
                "by_stage": count("a.lifecycle_stage"), "by_cond": count("a.condition"),
                "by_room": q(f"""SELECT COALESCE(b.name || ' · ' || r.name, 'Location unknown') k, COUNT(*) n FROM assets a LEFT JOIN rooms r ON r.id=a.room_id
                                 LEFT JOIN buildings b ON b.id=r.building_id WHERE {live} AND a.parent_id IS NULL{sw} GROUP BY k ORDER BY n DESC LIMIT 15""", sa),
                "flow": flow, "quality": qrows, "imports": imports,
                "cost_total": sum(r["cost"] for r in vals if r["cost_basis"] in (None, "Per unit", "Line total")),
                "cost_n": sum(1 for r in vals if r["cost_basis"] in (None, "Per unit", "Line total")),
                "book_total": sum(A.book_value(r["cost"], r["purchase_date"], r["life_years"]) or 0 for r in vals if r["cost_basis"] in (None, "Per unit", "Line total")),
                "by_holder": q(f"""SELECT COALESCE(ud.name, 'Stockroom / nobody') k, COUNT(*) n FROM assets a LEFT JOIN departments ud ON ud.id=a.using_dept_id
                                   WHERE {live} AND a.parent_id IS NULL{sw} GROUP BY k ORDER BY n DESC""", sa)}

    @app.route("/reports")
    def reports():
        data = report_data(None if request.args.get("csv") else 15)
        if request.args.get("csv") == "quality":
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["problem", "tag", "asset"])
            sw, sa = A.scope_sql()
            for label, (n, rows) in data["quality"].items():
                for r in rows:
                    w.writerow([label, r["tag"], r["name"]])
            return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=data-quality.csv"})
        return render_template("reports.html", **data)
