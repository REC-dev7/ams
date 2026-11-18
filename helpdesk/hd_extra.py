"""Helpdesk extras: notifications (email + Google Chat), analytics, planned maintenance and the AMS write-back.

Loaded at the bottom of app.py with init(app_module), the same way the AMS loads its modules, so it can share q(), db() and role().
Everything that sends something outside is OFF until it is configured with environment variables (see README)."""
import json
import os
import re
import smtplib
import sqlite3
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from flask import abort, flash, g, redirect, render_template, request, url_for

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_log (
  id INTEGER PRIMARY KEY, at TEXT, channel TEXT, recipient TEXT, subject TEXT, ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS maintenance (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, category TEXT, priority TEXT DEFAULT 'Low',
  location_id INTEGER REFERENCES locations(id), location TEXT, asset_tag TEXT, every_days INTEGER NOT NULL,
  next_due TEXT NOT NULL, active INTEGER DEFAULT 1, created_by INTEGER REFERENCES users(id), last_ticket_id INTEGER, last_run TEXT);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
"""
pending = []  # sends still running; wait() lets tests collect them
last_check = {"at": 0.0}  # when the due-jobs check last ran (module level so tests can reset it)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
EVERY = {7: "every week", 14: "every 2 weeks", 30: "every month", 90: "every 3 months", 180: "every 6 months", 365: "every year"}


# ---------- the two ways out: swapped for fakes in tests ----------

def send_chat(url, text):
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json; charset=UTF-8"})
    urllib.request.urlopen(req, timeout=8).read()


def send_mail(msg):
    host, port = os.environ["HD_SMTP_HOST"], int(os.environ.get("HD_SMTP_PORT", "587"))
    smtp = smtplib.SMTP_SSL(host, port, timeout=10) if port == 465 else smtplib.SMTP(host, port, timeout=10)
    with smtp:
        if port != 465:
            smtp.starttls()
        if os.environ.get("HD_SMTP_USER"):
            smtp.login(os.environ["HD_SMTP_USER"], os.environ.get("HD_SMTP_PASS", ""))
        smtp.send_message(msg)


def send_ams(path, payload):
    req = urllib.request.Request(os.environ["AMS_URL"].rstrip("/") + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AMS_API_TOKEN"]})
    urllib.request.urlopen(req, timeout=8).read()


def wait():
    for p in list(pending):
        p.join()


def chat_on():
    return os.environ.get("HD_CHAT_WEBHOOK", "").startswith("https://")


def mail_on():
    return bool(os.environ.get("HD_SMTP_HOST") and os.environ.get("HD_SMTP_FROM"))


def writeback_on():
    return bool(os.environ.get("HD_AMS_WRITEBACK") == "1" and os.environ.get("AMS_URL") and os.environ.get("AMS_API_TOKEN"))


def init(A):
    app, q, db, role = A.app, A.q, A.db, A.role

    def ref(t):
        return f"HD-{t['id']:04d}"

    def link(t):
        return (os.environ.get("HD_BASE_URL") or request.url_root).rstrip("/") + url_for("ticket", tid=t["id"])

    def background(channel, to, subject, fn, *args):
        """Run one send off the request thread so a slow mail server never slows a ticket. The result goes in notify_log."""
        def run():
            err = None
            try:
                fn(*args)
            except Exception as e:  # never let a notification break anything
                err = str(e)[:200]
            try:
                with sqlite3.connect(A.DB) as c:
                    c.execute("INSERT INTO notify_log(at, channel, recipient, subject, ok, error) VALUES (?,?,?,?,?,?)",
                              (A.now(), channel, to, subject[:200], int(err is None), err))
            except sqlite3.Error:
                pass
        pending[:] = [p for p in pending if p.is_alive()]
        pending.append(threading.Thread(target=run, daemon=True))
        pending[-1].start()

    def chat(text, subject, channel="chat"):
        if chat_on():
            background(channel, "Google Chat space", subject, send_chat, os.environ["HD_CHAT_WEBHOOK"], text)

    def mail(to, subject, body, channel="email"):
        if mail_on() and to and EMAIL.fullmatch(to):
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = os.environ["HD_SMTP_FROM"], to, " ".join(subject.split())
            msg.set_content(body)
            background(channel, to, subject, send_mail, msg)

    def ams_event(t, event, **extra):
        if writeback_on() and t["asset_tag"]:
            payload = {"event": event, "ticket": ref(t), "title": t["title"], **extra}
            background("ams", t["asset_tag"], f"{event} {ref(t)}", send_ams, "/api/v1/assets/" + urllib.parse.quote(t["asset_tag"], safe="") + "/ticket-event", payload)

    def fresh(tid):
        return q(A.TICKET_SQL + " WHERE t.id=?", (tid,), one=True)

    # ---------- events: wrap the three functions every route goes through ----------

    new_ticket, set_status, note = A.new_ticket, A.set_status, A.note

    def on_new(t):
        mark = "🚨 CRITICAL" if t["priority"] == "Critical" else "New ticket"
        chat(f"{mark} {ref(t)} [{t['priority']}] {t['title']}\n{t['location'] or 'no place given'} · {t['team']}\n{link(t)}", f"New {ref(t)}")
        body = f"{t['title']}\n\n{t['description'] or ''}\nPlace: {t['location'] or '-'}\nPriority: {t['priority']}\nTeam: {t['team']}\n\n{link(t)}\n"
        for u in q("SELECT email FROM users WHERE role='technician' AND team=? AND email IS NOT NULL AND email != ''", (t["team"],)):
            mail(u["email"], f"[Helpdesk] New {ref(t)}: {t['title']}", body)
        if t["request_type"] == "Fault":
            ams_event(t, "opened")

    def auto_assign(tid):
        """When switched on: give a new ticket to the technician in its team with the fewest unfinished tickets."""
        t = fresh(tid)
        if A.app_settings_cache.get("auto_assign") != "1" or t["assigned_to"]:
            return
        tech = q("""SELECT u.id, u.name FROM users u WHERE u.role='technician' AND u.team=?
                    ORDER BY (SELECT COUNT(*) FROM tickets x WHERE x.assigned_to=u.id AND x.status IN ('Open','In progress','Waiting on requester')), u.id
                    LIMIT 1""", (t["team"],), one=True)
        if tech:
            db().execute("UPDATE tickets SET assigned_to=? WHERE id=?", (tech["id"], tid))
            A.note(tid, f"Assigned to {tech['name']} (automatically: fewest open tickets in {t['team']})", response=False)
            db().commit()

    def after_create(tid):
        auto_assign(tid)
        on_new(fresh(tid))

    def wrapped_new(f, logged=False):
        r = new_ticket(f, logged)
        if r and not logged:
            after_create(r[0])
        return r

    def wrapped_status(t, new, text=None, **cols):
        set_status(t, new, text, **cols)
        if new == "Resolved":
            now_t = fresh(t["id"])
            who = q("SELECT * FROM users WHERE id=?", (now_t["raised_by"],), one=True)
            if who and who["id"] != g.user["id"]:
                mail(who["email"], f"[Helpdesk] {ref(now_t)} resolved: please confirm",
                     f"Your ticket \"{now_t['title']}\" was resolved.\n\nWhat was done: {now_t['resolution']}\n\nIf it is fixed, close it; if not, reopen it:\n{link(now_t)}\n")
            if now_t["asset_tag"]:
                others = q("SELECT COUNT(*) n FROM tickets WHERE asset_tag=? AND id!=? AND status IN ('Open','In progress','Waiting on requester') "
                           "AND COALESCE(parent_id,0)!=?", (now_t["asset_tag"], now_t["id"], now_t["id"]), one=True)["n"]
                ams_event(now_t, "resolved", resolution=now_t["resolution"] or "", mark_working=others == 0)

    def wrapped_note(tid, text, system=True, internal=False, response=True):
        note(tid, text, system, internal, response)
        if system and text.startswith("Assigned to "):
            t = fresh(tid)
            who = q("SELECT * FROM users WHERE id=?", (t["assigned_to"],), one=True)
            if who and who["id"] != g.user["id"]:
                mail(who["email"], f"[Helpdesk] {ref(t)} assigned to you: {t['title']}",
                     f"{t['title']}\n\nPlace: {t['location'] or '-'}\nPriority: {t['priority']}\n\n{link(t)}\n")

    A.new_ticket, A.set_status, A.note = wrapped_new, wrapped_status, wrapped_note

    # ---------- service rules: fix-by hours, quiet limit, auto-assign (admin edits them; no code change needed) ----------

    defaults = {**{f"sla_{p}": str(h) for p, h in A.SLA_HOURS.items()}, "stale_hours": str(A.STALE_HOURS), "auto_assign": "0", "auto_close_days": "0"}
    A.app_settings_cache = dict(defaults)

    @app.before_request
    def load_rules():
        # read each request so every worker sees an admin's change at once; it is one tiny query
        try:
            saved = dict(db().execute("SELECT key, value FROM app_settings").fetchall())
        except sqlite3.Error:
            saved = {}
        A.app_settings_cache = {**defaults, **saved}
        for p in A.SLA_HOURS:
            A.SLA_HOURS[p] = int(A.app_settings_cache[f"sla_{p}"])
        A.STALE_HOURS = int(A.app_settings_cache["stale_hours"])

    @app.context_processor
    def rules_ctx():
        return {"rules": A.app_settings_cache}

    @app.route("/settings/rules", methods=["POST"])
    @role("admin")
    def rules_save():
        f, new = request.form, {}
        for key in [f"sla_{p}" for p in A.PRIORITIES] + ["stale_hours"]:
            v = A.opt_int(f.get(key))
            if not v or v > 2000:
                flash("Hours must be whole numbers from 1 to 2000.", "error")
                return redirect(url_for("settings"))
            new[key] = str(v)
        days = A.opt_int(f.get("auto_close_days")) if f.get("auto_close_days") not in (None, "") else 0
        if days is None or days > 60:
            flash("Auto-close days must be a whole number from 0 (never) to 60.", "error")
            return redirect(url_for("settings"))
        new["auto_close_days"] = str(days)
        new["auto_assign"] = "1" if f.get("auto_assign") else "0"
        db().executemany("INSERT INTO app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(new.items()))
        db().commit()
        flash("Service rules saved. New fix-by times apply to tickets raised from now on.", "ok")
        return redirect(url_for("settings"))

    # ---------- notifications page (admin) ----------

    @app.route("/settings/notifications", methods=["GET", "POST"])
    @role("admin")
    def notifications():
        if request.method == "POST":
            if chat_on():
                chat("Test message from REC Helpdesk: notifications are working.", "Test")
            if mail_on() and EMAIL.fullmatch(g.user["email"] or ""):
                mail(g.user["email"], "[Helpdesk] Test message", "Test message from REC Helpdesk: email is working.")
            if not (chat_on() or mail_on()):
                flash("Nothing is set up yet. Add the settings on the server first.", "error")
            else:
                flash("Test sent. The result shows in the list below in a few seconds.", "ok")
            return redirect(url_for("notifications"))
        return render_template("notifications.html", chat_on=chat_on(), mail_on=mail_on(), writeback=writeback_on(),
                               rows=q("SELECT * FROM notify_log ORDER BY id DESC LIMIT 50"),
                               without_email=q("SELECT COUNT(*) n FROM users WHERE role IN ('technician','staff','hod','coordinator') AND COALESCE(email,'')=''", one=True)["n"])

    # ---------- analytics ----------

    def hours(a, b):
        return (datetime.strptime(b, A.FMT) - datetime.strptime(a, A.FMT)).total_seconds() / 3600

    def ranked(counter, limit=10):
        top = counter.most_common(limit)
        peak = top[0][1] if top else 1
        return [{"label": k, "n": n, "pct": max(4, round(100 * n / peak))} for k, n in top]

    @app.route("/analytics")
    def analytics():
        if g.user["role"] == "staff":
            abort(403)
        days = int(request.args["days"]) if request.args.get("days") in ("30", "90", "365") else 0
        where, args = A.scope()
        if days:
            where += " AND t.created_at >= ?"
            args = [*args, (datetime.now() - timedelta(days=days)).strftime(A.FMT)]
        rows = q("SELECT t.*, u.name tech FROM tickets t LEFT JOIN users u ON u.id=t.assigned_to WHERE 1=1" + where, args)
        stamp = A.now()
        real = [t for t in rows if t["source"] != "Logged by technician"]  # work logged after the fact says nothing about response time
        done = [t for t in real if t["resolved_at"]]
        timed = [t for t in done if t["due_at"]]
        met = sum(1 for t in timed if t["resolved_at"] <= t["due_at"])
        first = [hours(t["created_at"], t["first_response_at"]) for t in real if t["first_response_at"]]
        fix = [hours(t["created_at"], t["resolved_at"]) for t in done]
        months = Counter(t["created_at"][:7] for t in rows)
        closed = Counter(t["resolved_at"][:7] for t in rows if t["resolved_at"])
        keys = sorted(set(months) | set(closed))[-6:]
        top_month = max([months[k] for k in keys] + [closed[k] for k in keys] + [1])
        per_tech = {}
        for t in rows:
            if t["tech"]:
                d = per_tech.setdefault(t["tech"], {"name": t["tech"], "assigned": 0, "fixed": 0, "hours": []})
                d["assigned"] += 1
                if t["resolved_at"]:
                    d["fixed"] += 1
                    d["hours"].append(hours(t["created_at"], t["resolved_at"]))
        stats = {
            "total": len(rows), "live": sum(1 for t in rows if t["status"] in A.LIVE),
            "overdue": sum(1 for t in rows if t["status"] in ("Open", "In progress") and t["due_at"] and t["due_at"] < stamp),
            "fixed": len(done), "sla": round(100 * met / len(timed)) if timed else None, "sla_n": len(timed),
            "first": round(statistics.median(first), 1) if first else None, "fix": round(statistics.median(fix), 1) if fix else None,
            "rating": round(statistics.mean(rated), 1) if (rated := [t["rating"] for t in rows if t["rating"]]) else None, "rated_n": len(rated),
        }
        return render_template("analytics.html", days=days, stats=stats,
                               by_month=[{"label": k, "raised": months[k], "closed": closed[k], "r": round(100 * months[k] / top_month), "c": round(100 * closed[k] / top_month)} for k in keys],
                               by_category=ranked(Counter(t["category"] or "Other" for t in rows), 12), by_team=ranked(Counter(t["team"] or "Other" for t in rows)),
                               by_dept=ranked(Counter(t["department"] or "Not set" for t in rows)), by_prio=ranked(Counter(t["priority"] for t in rows)),
                               places=[(k, n) for k, n in Counter(t["location"] for t in rows if t["location"]).most_common(8) if n > 1],
                               assets=[(k, n) for k, n in Counter(t["asset_tag"] for t in rows if t["asset_tag"]).most_common(8) if n > 1],
                               techs=sorted(({**d, "avg": round(sum(d["hours"]) / len(d["hours"]), 1) if d["hours"] else None} for d in per_tech.values()), key=lambda d: -d["fixed"]))

    # ---------- planned maintenance ----------

    def make_ticket(m):
        """Raise the ticket for one schedule, as the person who set it up. Returns the ticket id or None."""
        creator = q("SELECT * FROM users WHERE id=?", (m["created_by"],), one=True) or q("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1", one=True)
        if not creator:
            return None
        before = g.get("user")
        g.user = creator
        try:
            r = new_ticket({"title": m["title"], "description": (m["description"] or "") + "\n\nPlanned maintenance, raised automatically.",
                            "category": m["category"], "priority": m["priority"], "request_type": "Service request",
                            "location_id": str(m["location_id"] or ""), "location": m["location"] or "", "asset_tag": m["asset_tag"] or ""})
        finally:
            g.user = before
        if r:
            db().execute("UPDATE tickets SET source='Planned maintenance' WHERE id=?", (r[0],))
            after_create(r[0])
        return r[0] if r else None

    def run_due():
        today = date.today()
        for m in q("SELECT * FROM maintenance WHERE active=1 AND next_due<=?", (today.isoformat(),)):
            nxt = date.fromisoformat(m["next_due"])
            while nxt <= today:  # a long gap raises one ticket, not a pile
                nxt += timedelta(days=m["every_days"])
            # claim the job first: with several server workers only the one whose update lands raises the ticket
            claimed = db().execute("UPDATE maintenance SET next_due=?, last_run=? WHERE id=? AND next_due=?",
                                   (nxt.isoformat(), today.isoformat(), m["id"], m["next_due"])).rowcount
            db().commit()
            if claimed:
                tid = make_ticket(m)
                db().execute("UPDATE maintenance SET last_ticket_id=COALESCE(?, last_ticket_id) WHERE id=?", (tid, m["id"]))
                db().commit()

    @app.before_request
    def maintenance_tick():
        # after the login guard (registered earlier), so g.user is set. Once every few minutes is plenty for jobs counted in days.
        if g.get("user") and request.method == "GET" and time.time() - last_check["at"] > 300:
            last_check["at"] = time.time()
            run_due()
            auto_close()
            send_reminders()

    def auto_close():
        """When switched on: a resolved ticket nobody confirmed or reopened within N days is closed, with a note saying so."""
        days = int(A.app_settings_cache.get("auto_close_days") or 0)
        if not days:
            return
        stamp, cut = A.now(), (datetime.now() - timedelta(days=days)).strftime(A.FMT)
        for t in q("SELECT id FROM tickets WHERE status='Resolved' AND resolved_at < ?", (cut,)):
            # only this update may close it, so two server workers can't both write the note
            if db().execute("UPDATE tickets SET status='Closed', updated_at=? WHERE id=? AND status='Resolved'", (stamp, t["id"])).rowcount:
                db().execute("INSERT INTO status_log(ticket_id, user_id, from_status, to_status, at) VALUES (?, NULL, 'Resolved', 'Closed', ?)", (t["id"], stamp))
                db().execute("INSERT INTO comments(ticket_id, user_id, body, system, at) VALUES (?, NULL, ?, 1, ?)",
                             (t["id"], f"Closed automatically: no reply {days} day(s) after it was resolved", stamp))
        db().commit()

    def send_reminders():
        """Once a day: overdue and quiet tickets to the Chat space, and each technician their own by email.
        The date of the last one is claimed in app_settings first, so a restart or a second server worker doesn't send it twice."""
        if not (chat_on() or mail_on()):
            return
        today = date.today().isoformat()
        db().execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES ('reminder_sent', '')")
        if not db().execute("UPDATE app_settings SET value=? WHERE key='reminder_sent' AND value!=?", (today, today)).rowcount:
            return
        db().commit()
        cut = (datetime.now() - timedelta(hours=A.STALE_HOURS)).strftime(A.FMT)
        rows = q(A.TICKET_SQL + f" WHERE {A.OPEN} AND (t.due_at < ? OR t.updated_at < ?) ORDER BY t.due_at IS NULL, t.due_at", (A.now(), cut))
        if not rows:
            return
        late = lambda t: "overdue" if t["due_at"] and t["due_at"] < A.now() else f"no update {A.STALE_HOURS}h"
        line = lambda t: f"{ref(t)} [{t['priority']}] {t['title']} ({late(t)}, {t['assignee'] or 'unassigned'})"
        n_over = sum(1 for t in rows if late(t) == "overdue")
        chat(f"Daily reminder: {n_over} overdue, {len(rows) - n_over} with no update\n" + "\n".join(line(t) for t in rows[:15])
             + (f"\n...and {len(rows) - 15} more" if len(rows) > 15 else ""), f"Reminder {date.today()}", channel="reminder-chat")
        by_tech = {}
        for t in rows:
            if t["assigned_to"]:
                by_tech.setdefault(t["assigned_to"], []).append(t)
        for uid, mine in by_tech.items():
            who = q("SELECT email FROM users WHERE id=?", (uid,), one=True)
            if who:
                mail(who["email"], f"[Helpdesk] Reminder: {len(mine)} ticket(s) need you",
                     "\n".join(f"{line(t)}\n  {link(t)}" for t in mine) + "\n", channel="reminder-email")

    @app.route("/maintenance")
    @role(*A.MANAGE)
    def maintenance():
        return render_template("maintenance.html", rows=q("SELECT m.*, l.block, l.floor, l.room FROM maintenance m LEFT JOIN locations l ON l.id=m.location_id ORDER BY m.next_due"),
                               locations=q("SELECT * FROM locations ORDER BY block, floor, room"), EVERY=EVERY)

    @app.route("/maintenance/add", methods=["POST"])
    @role(*A.MANAGE)
    def maintenance_add():
        f = request.form
        every = A.opt_int(f.get("every_days"))
        try:
            first = date.fromisoformat(f.get("next_due", "")) if f.get("next_due") else date.today()
        except ValueError:
            first = None
        loc_id, loc = A.location_from_form(f)
        if not f.get("title", "").strip() or every not in EVERY or not first or not (loc or f.get("asset_tag", "").strip()):
            flash("Give a title, how often, a first date and a place or asset tag.", "error")
        else:
            cat = f.get("category") if f.get("category") in A.CATEGORIES else "Other"
            prio = f.get("priority") if f.get("priority") in A.PRIORITIES else "Low"
            db().execute("""INSERT INTO maintenance(title, description, category, priority, location_id, location, asset_tag, every_days, next_due, created_by)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", (f["title"].strip(), f.get("description", "").strip(), cat, prio, loc_id, loc,
                                                              f.get("asset_tag", "").strip() or None, every, first.isoformat(), g.user["id"]))
            db().commit()
            flash(f"Scheduled: {EVERY[every]}.", "ok")
        return redirect(url_for("maintenance"))

    @app.route("/maintenance/<int:mid>/<action>", methods=["POST"])
    @role(*A.MANAGE)
    def maintenance_act(mid, action):
        m = q("SELECT * FROM maintenance WHERE id=?", (mid,), one=True)
        if not m or action not in ("pause", "resume", "delete", "now"):
            abort(404)
        if action == "delete":
            db().execute("DELETE FROM maintenance WHERE id=?", (mid,))
        elif action == "now":
            tid = make_ticket(m)
            db().execute("UPDATE maintenance SET last_ticket_id=COALESCE(?, last_ticket_id), last_run=? WHERE id=?", (tid, date.today().isoformat(), mid))
            flash("Ticket raised." if tid else "Could not raise it: add a place or asset tag.", "ok" if tid else "error")
        else:
            db().execute("UPDATE maintenance SET active=? WHERE id=?", (int(action == "resume"), mid))
        db().commit()
        return redirect(url_for("maintenance"))
