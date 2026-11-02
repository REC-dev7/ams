"""The rest of the guide that makes sense now: stock-check findings (wrong place, unregistered equipment), bulk actions with a preview,
replacing a part, network interfaces, contracts and reminders, scan-by-tag, and a small read-only API for other systems.
"""
import os
import re
import secrets
from datetime import date, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

CONTRACT_KINDS = ["AMC", "Warranty", "Insurance", "Support", "Lease"]
BULK_ACTIONS = {"department": "Issue to a department", "stockroom": "Return to the stockroom", "verify": "Mark as verified today"}
MAX_BULK = 200


def init(A):
    q, db, app, role = A.q, A.db, A.app, A.role

    app.jinja_env.globals["BULK_ACTIONS"] = BULK_ACTIONS

    # ---------- find by code (scan a label, or type any tag / old code) ----------

    def find_asset(code):
        sw, sa = A.scope_sql()
        code = (code or "").strip()
        for cond in ("a.tag=? COLLATE NOCASE", "a.legacy_code=?", "EXISTS (SELECT 1 FROM asset_ids i WHERE i.asset_id=a.id AND i.value=?)"):
            r = q(A.ASSET_SELECT + f" WHERE {cond} AND a.archived_at IS NULL" + sw + " LIMIT 1", [code, *sa], one=True)
            if r:
                return r
        return None

    @app.route("/t/<path:code>")
    def tag_lookup(code):
        """What a scanned QR label or a typed tag opens. Old codes work too."""
        a = find_asset(code)
        if a:
            return redirect(url_for("asset", aid=a["id"]))
        flash(f"Nothing in the register has the code {code}.", "error")
        return redirect(url_for("assets", q=code))

    # ---------- stock check findings ----------

    def record_findings(rid, text, today):
        """Lines typed under 'equipment here that isn't on this list': a tag or old code of something listed elsewhere, or a description."""
        n = 0
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            a = find_asset(line)
            if a and a["room_id"] == rid:
                db().execute("UPDATE assets SET verification='Verified', last_audit=? WHERE id=?", (today, a["id"]))
                A.log(a["id"], "Verified in stock check", etype="Verified", event_date=today)
            elif a:
                db().execute("UPDATE assets SET verification='Wrong location' WHERE id=?", (a["id"],))
                db().execute("INSERT INTO check_findings(kind, room_id, asset_id, found_on, found_by) VALUES ('Wrong location',?,?,?,?)", (rid, a["id"], today, g.user["id"]))
                A.log(a["id"], f"Found in {room_label(rid)}, but listed in {a['room'] or 'no room'}", etype="Wrong location", event_date=today)
                n += 1
            else:
                db().execute("INSERT INTO check_findings(kind, room_id, description, found_on, found_by) VALUES ('Unregistered',?,?,?,?)", (rid, line[:200], today, g.user["id"]))
                n += 1
        return n

    def room_label(rid):
        r = q("SELECT r.name, b.name building FROM rooms r JOIN buildings b ON b.id=r.building_id WHERE r.id=?", (rid,), one=True)
        return f"{r['building']} · {r['name']}" if r else "another room"

    A.record_findings = record_findings

    @app.route("/findings")
    @role("move")
    def findings():
        where = "" if A.dept_ids() is None else " AND f.found_by=?"
        args = [] if A.dept_ids() is None else [g.user["id"]]
        rows = q(f"""SELECT f.*, a.tag, a.name asset_name, r.name room, b.name building, ar.name home_room, u.name by_name FROM check_findings f
                     LEFT JOIN assets a ON a.id=f.asset_id LEFT JOIN rooms r ON r.id=f.room_id LEFT JOIN buildings b ON b.id=r.building_id
                     LEFT JOIN rooms ar ON ar.id=a.room_id LEFT JOIN users u ON u.id=f.found_by WHERE 1=1{where}
                     ORDER BY f.status='Open' DESC, f.id DESC LIMIT 200""", args)
        return render_template("findings.html", rows=rows)

    @app.route("/findings/<int:fid>/resolve", methods=["POST"])
    @role("move")
    def finding_resolve(fid):
        f = q("SELECT * FROM check_findings WHERE id=? AND status='Open'", (fid,), one=True)
        if not f:
            abort(404)
        if A.dept_ids() is not None and f["found_by"] != g.user["id"]:
            abort(403)
        how, note = request.form.get("how"), (request.form.get("note") or "").strip()
        today = date.today().isoformat()
        if not note:
            flash("Say what was decided.", "error")
            return redirect(url_for("findings"))
        if how == "move" and f["asset_id"]:
            a = A.get_asset(f["asset_id"])
            db().execute("UPDATE assets SET room_id=?, location_unresolved=0, verification='Verified', last_audit=? WHERE id=?", (f["room_id"], today, a["id"]))
            db().execute("UPDATE assets SET room_id=? WHERE parent_id=?", (f["room_id"], a["id"]))
            A.log(a["id"], f"Record updated: it is in {room_label(f['room_id'])}", etype="Corrected", event_date=today, reason=note)
        elif how == "keep" and f["asset_id"]:
            db().execute("UPDATE assets SET verification='Verified', last_audit=? WHERE id=?", (today, f["asset_id"]))
            A.log(f["asset_id"], f"Seen in {room_label(f['room_id'])}, record kept as it is", etype="Verified", event_date=today, reason=note)
        elif how != "dismiss":
            flash("Choose what to do.", "error")
            return redirect(url_for("findings"))
        db().execute("UPDATE check_findings SET status=?, resolution=?, resolved_by=?, resolved_at=datetime('now') WHERE id=?",
                     ("Dismissed" if how == "dismiss" else "Resolved", note, g.user["id"], fid))
        db().commit()
        flash("Saved.", "ok")
        return redirect(url_for("findings"))

    # ---------- bulk actions: always a preview first, then a result for every item ----------

    def bulk_apply(a, action, f):
        if action == "verify":
            db().execute("UPDATE assets SET verification='Verified', last_audit=? WHERE id=?", (date.today().isoformat(), a["id"]))
            A.log(a["id"], "Verified (bulk)", etype="Verified", event_date=date.today().isoformat())
            return "Marked verified"
        cur = A.current_custody(a["id"])
        form = {**f, "expected": cur["id"] if cur else 0}
        return A.change_custody(a, form, to="Stockroom" if action == "stockroom" else "Department")

    def bulk_run(ids, action, f, commit):
        results = []
        for aid in ids[:MAX_BULK]:
            try:
                a = A.get_asset(aid)
                if a["archived_at"]:
                    raise ValueError("It is archived.")
                out = bulk_apply(a, action, f)
                if commit:
                    db().commit()
                results.append((a, True, out))
            except ValueError as e:
                db().rollback()
                results.append((q("SELECT id, tag, name FROM assets WHERE id=?", (aid,), one=True), False, str(e)))
            except Exception as e:  # not allowed to see it, or gone
                db().rollback()
                results.append((q("SELECT id, tag, name FROM assets WHERE id=?", (aid,), one=True), False, "Not found or not in your department." if getattr(e, "code", 0) in (403, 404) else "Could not be changed."))
        if not commit:
            db().rollback()  # a preview changes nothing
        return results

    @app.route("/assets/bulk", methods=["POST"])
    @role("move")
    def assets_bulk():
        ids = [i for i in (A.opt_int(x) for x in request.form.getlist("ids")) if i]
        action = request.form.get("action")
        if not ids or action not in BULK_ACTIONS:
            flash("Tick some assets and choose what to do with them.", "error")
            return redirect(request.referrer or url_for("assets"))
        if len(ids) > MAX_BULK:
            flash(f"Up to {MAX_BULK} assets at a time.", "error")
            return redirect(request.referrer or url_for("assets"))
        step = request.form.get("step", "preview")
        fields = {k: request.form.get(k, "") for k in ("department_id", "reason", "event_date", "approval_ref")}
        if action != "verify" and not fields["reason"].strip():
            fields["reason"] = ""
        results = bulk_run(ids, action, fields, commit=(step == "run"))
        return render_template("bulk.html", results=results, action=action, label=BULK_ACTIONS[action], fields=fields, ids=ids, step=step,
                               ok=sum(1 for r in results if r[1]), **A.lookups())

    # ---------- replace a part without overwriting its history ----------

    @app.route("/assets/<int:aid>/replace", methods=["POST"])
    @role("edit")
    def part_replace(aid):
        old = A.get_asset(aid)
        f = request.form
        reason = (f.get("reason") or "").strip()
        if not old["parent_id"] or old["archived_at"]:
            flash("Only a part that is still fitted can be replaced.", "error")
        elif not reason:
            flash("Say why it is being replaced.", "error")
        else:
            tag = A.next_tag()
            cur = db().execute("""INSERT INTO assets(tag, name, category_id, room_id, parent_id, lifecycle_stage, condition, brand, model, serial_no, managing_dept_id,
                                  owning_dept_id, acquisition_basis, location_unresolved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tag, (f.get("name") or old["name"]).strip(), old["category_id"], old["room_id"], old["parent_id"], old["lifecycle_stage"], "Working",
                                (f.get("brand") or "").strip() or None, (f.get("model") or "").strip() or None, (f.get("serial_no") or "").strip() or None,
                                old["managing_dept_id"], old["owning_dept_id"], "Purchase", old["location_unresolved"]))
            db().execute("UPDATE assets SET archived_at=datetime('now'), archive_reason=? WHERE id=?", (f"Replaced by {tag}: {reason}", aid))
            A.sync_status(cur.lastrowid)
            A.sync_status(aid)
            A.log(aid, f"Replaced by {tag}: {reason}", etype="Part replaced", reason=reason)
            A.log(cur.lastrowid, f"Fitted as the replacement for {old['tag']}", etype="Created", reason=reason)
            A.log(old["parent_id"], f"Part replaced: {old['tag']} {old['name']} → {tag}", etype="Part replaced", reason=reason)
            db().commit()
            flash(f"Replaced. The old part {old['tag']} is kept in the history.", "ok")
            return redirect(url_for("asset", aid=old["parent_id"]))
        return redirect(url_for("asset", aid=aid))

    # ---------- network interfaces ----------

    def interfaces_for(aid):
        return q("SELECT * FROM asset_interfaces WHERE asset_id=? ORDER BY id", (aid,))

    app.jinja_env.globals["asset_interfaces"] = interfaces_for

    @app.route("/assets/<int:aid>/interfaces", methods=["POST"])
    @role("edit")
    def interface_add(aid):
        A.get_asset(aid)
        f = request.form
        name = (f.get("name") or "").strip()
        if not name:
            flash("Name the port or interface (for example eth0, Gi1/0/12).", "error")
        else:
            db().execute("INSERT INTO asset_interfaces(asset_id, name, mac, ip, vlan, connected_to, notes, added_by) VALUES (?,?,?,?,?,?,?,?)",
                         (aid, name, *[(f.get(k) or "").strip() or None for k in ("mac", "ip", "vlan", "connected_to", "notes")], g.user["id"]))
            A.log(aid, f"Interface added: {name}", etype="Interface")
            db().commit()
        return redirect(url_for("asset", aid=aid))

    @app.route("/interfaces/<int:iid>/delete", methods=["POST"])
    @role("edit")
    def interface_delete(iid):
        i = q("SELECT * FROM asset_interfaces WHERE id=?", (iid,), one=True)
        if not i:
            abort(404)
        A.get_asset(i["asset_id"])
        db().execute("DELETE FROM asset_interfaces WHERE id=?", (iid,))
        A.log(i["asset_id"], f"Interface removed: {i['name']}", etype="Interface")
        db().commit()
        return redirect(url_for("asset", aid=i["asset_id"]))

    # ---------- contracts (AMC, warranty, insurance, support) ----------

    @app.route("/contracts")
    @role("docs")
    def contracts():
        rows = q("""SELECT c.*, d.name dept, (SELECT group_concat(a.tag, ', ') FROM contract_assets x JOIN assets a ON a.id=x.asset_id WHERE x.contract_id=c.id) tags,
                    (SELECT COUNT(*) FROM contract_assets x WHERE x.contract_id=c.id) n FROM contracts c LEFT JOIN departments d ON d.id=c.department_id
                    WHERE c.archived_at IS NULL ORDER BY c.end_date IS NULL, c.end_date""")
        return render_template("contracts.html", rows=rows, soon=(date.today() + timedelta(days=60)).isoformat(), CONTRACT_KINDS=CONTRACT_KINDS, **A.lookups())

    @app.route("/contracts/add", methods=["POST"])
    @role("purchase")
    def contract_add():
        f = request.form
        try:
            if f.get("kind") not in CONTRACT_KINDS:
                raise ValueError("Choose the kind of contract.")
            dates = []
            for k in ("start_date", "end_date"):
                v = (f.get(k) or "").strip() or None
                try:
                    if v:
                        date.fromisoformat(v)
                except ValueError:
                    raise ValueError("Dates must be year-month-day.")
                dates.append(v)
            if not dates[1]:
                raise ValueError("Give the end date so a reminder can be raised.")
            try:
                cost = float(f["cost"]) if f.get("cost") else None
            except ValueError:
                raise ValueError("Cost must be a number.")
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("contracts"))
        cur = db().execute("INSERT INTO contracts(kind, vendor, reference, start_date, end_date, cost, department_id, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f["kind"], (f.get("vendor") or "").strip() or None, (f.get("reference") or "").strip() or None, *dates, cost,
                            A.opt_int(f.get("department_id")), (f.get("notes") or "").strip() or None, g.user["id"]))
        linked, missing = 0, []
        for code in re.split(r"[,\n;]+", f.get("assets", "")):
            if code.strip():
                a = find_asset(code)
                if a:
                    db().execute("INSERT OR IGNORE INTO contract_assets(contract_id, asset_id) VALUES (?,?)", (cur.lastrowid, a["id"]))
                    A.log(a["id"], f"{f['kind']} contract {f.get('reference') or ''} until {dates[1]}".strip(), etype="Contract")
                    linked += 1
                else:
                    missing.append(code.strip())
        db().commit()
        flash(f"Contract saved with {linked} asset(s)." + (f" Not found: {', '.join(missing[:6])}." if missing else ""), "ok" if not missing else "error")
        return redirect(url_for("contracts"))

    @app.route("/contracts/<int:cid>/archive", methods=["POST"])
    @role("purchase")
    def contract_archive(cid):
        db().execute("UPDATE contracts SET archived_at=datetime('now') WHERE id=?", (cid,))
        db().commit()
        return redirect(url_for("contracts"))

    # ---------- reminders ----------

    @app.route("/reminders")
    def reminders():
        today = date.today()
        d30, d60 = (today + timedelta(days=30)).isoformat(), (today + timedelta(days=60)).isoformat()
        sw, sa = A.scope_sql()
        live = "a.status != 'Scrapped' AND a.archived_at IS NULL"
        docs = A.allowed("docs")
        cut = (today - timedelta(days=A.AUDIT_DAYS)).isoformat()
        stock_scope = "" if A.dept_ids() is None else f" AND (s.department_id IS NULL OR s.department_id IN ({','.join('?' * len(A.dept_ids())) or 'NULL'}))"
        stock_args = [] if A.dept_ids() is None else list(A.dept_ids())
        data = {
            "warranty": q(A.ASSET_SELECT + f" WHERE {live} AND a.parent_id IS NULL AND a.warranty_until BETWEEN ? AND ?{sw} ORDER BY a.warranty_until LIMIT 40", [today.isoformat(), d60, *sa]),
            "service": q(A.ASSET_SELECT + f" WHERE {live} AND a.next_service_due IS NOT NULL AND a.next_service_due <= ?{sw} ORDER BY a.next_service_due LIMIT 40", [d30, *sa]),
            "contracts": q("SELECT c.*, (SELECT COUNT(*) FROM contract_assets x WHERE x.contract_id=c.id) n FROM contracts c WHERE c.archived_at IS NULL AND c.end_date <= ? ORDER BY c.end_date", [d60]) if docs else [],
            "licences": q("SELECT * FROM licences WHERE archived_at IS NULL AND renewal_date IS NOT NULL AND renewal_date <= ? ORDER BY renewal_date", [d60]) if docs else [],
            "low": q(f"""SELECT s.*, (SELECT COALESCE(SUM(m.qty), 0) FROM stock_movements m WHERE m.item_id=s.id) balance FROM stock_items s
                         WHERE s.archived_at IS NULL AND s.min_level > 0{stock_scope} AND balance <= s.min_level ORDER BY s.name""", stock_args),
            "loans": q(f"""SELECT a.id, a.tag, a.name, cu.person, cu.due_date FROM custody cu JOIN assets a ON a.id=cu.asset_id
                           WHERE cu.ended_at IS NULL AND cu.holder_type='Person' AND cu.due_date < ?{sw} ORDER BY cu.due_date""", [today.isoformat(), *sa]),
            "rooms": q(f"""SELECT r.id, r.name, b.name building, COUNT(*) n, MIN(COALESCE(a.last_audit, '')) oldest FROM assets a JOIN rooms r ON r.id=a.room_id
                           JOIN buildings b ON b.id=r.building_id WHERE {live} AND a.parent_id IS NULL AND (a.last_audit IS NULL OR a.last_audit < ?){sw}
                           GROUP BY r.id ORDER BY n DESC LIMIT 25""", [cut, *sa]),
            "retiring": q(A.ASSET_SELECT + f" WHERE a.lifecycle_stage IN ('Retirement proposed','Retirement approved','Awaiting disposal') AND a.archived_at IS NULL{sw} ORDER BY a.lifecycle_stage", sa),
            "findings": q("SELECT COUNT(*) n FROM check_findings WHERE status='Open'", one=True)["n"] if A.allowed("move") else 0,
        }
        return render_template("reminders.html", **data)

    # ---------- read-only API for other systems ----------
    # Off unless AMS_API_TOKEN is set. Sends identity, place and holder only: no costs, documents, keys or people's accounts.

    A.PUBLIC.update({"api_asset", "api_assets"})

    def api_guard():
        token = os.environ.get("AMS_API_TOKEN", "")
        if not token:
            return jsonify(error="The API is not switched on."), 404
        got = request.headers.get("Authorization", "")
        if not got.startswith("Bearer ") or not secrets.compare_digest(got[7:], token):
            return jsonify(error="Missing or wrong token."), 401
        return None

    def api_item(a):
        holder = a["custodian"] or a["using_dept"]
        return {"tag": a["tag"], "name": a["name"], "category": a["cat"], "brand": a["brand"], "model": a["model"], "serial_no": a["serial_no"],
                "lifecycle_stage": a["lifecycle_stage"], "condition": a["condition"], "status": a["status"], "building": a["building"], "room": a["room"],
                "location_known": bool(a["room_id"]) and not a["location_unresolved"], "managing_department": a["managing_dept"], "using_department": a["using_dept"],
                "holder": holder, "old_asset_code": a["legacy_code"], "archived": bool(a["archived_at"])}

    @app.route("/api/v1/assets/<path:code>")
    def api_asset(code):
        if (bad := api_guard()):
            return bad
        a = q(A.ASSET_SELECT + " WHERE (a.tag=? COLLATE NOCASE OR a.legacy_code=?) LIMIT 1", (code.strip(), code.strip()), one=True)
        return jsonify(api_item(a)) if a else (jsonify(error="No asset with that code."), 404)

    @app.route("/api/v1/assets")
    def api_assets():
        if (bad := api_guard()):
            return bad
        term = request.args.get("q", "").strip()
        limit = min(max(A.opt_int(request.args.get("limit")) or 100, 1), 1000)
        sql, args = " WHERE a.archived_at IS NULL AND a.parent_id IS NULL AND a.status != 'Scrapped'", []
        if term:
            sql += " AND (a.tag LIKE ? OR a.name LIKE ? OR a.legacy_code LIKE ?)"
            args = [f"%{term}%"] * 3
        rows = q(A.ASSET_SELECT + sql + " ORDER BY a.tag LIMIT ?", [*args, limit])
        return jsonify(count=len(rows), items=[api_item(a) for a in rows])
