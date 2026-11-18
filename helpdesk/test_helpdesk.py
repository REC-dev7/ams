"""Smoke test: python test_helpdesk.py"""
import io
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

import app as m


def csrf(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, page="/"):
    return c.post(path, data={**data, "csrf": csrf(c, page)})


def login(c, user, pw):
    c.post("/logout", data={"csrf": csrf(c, "/login")})
    with sqlite3.connect(m.DB) as d:  # the first-password change has its own test
        d.execute("UPDATE users SET must_change=0 WHERE username=?", (user,))
    return post(c, "/login", {"username": user, "password": pw}, "/login")


def fake_ams(path):
    """Minimal AMS database: one building, one room, one PC."""
    with sqlite3.connect(path) as a:
        a.executescript("""
          CREATE TABLE buildings (id INTEGER PRIMARY KEY, name TEXT);
          CREATE TABLE rooms (id INTEGER PRIMARY KEY, building_id INT, name TEXT);
          CREATE TABLE assets (id INTEGER PRIMARY KEY, tag TEXT, name TEXT, room_id INT, parent_id INT, status TEXT);
          INSERT INTO buildings VALUES (1, 'CSE Block');
          INSERT INTO rooms VALUES (1, 1, 'Computer Lab 1');
          INSERT INTO assets VALUES (1, 'AMS-00013', 'L1-PC-04', 1, NULL, 'Faulty');""")


def raise_ticket(c, title="Wi-Fi not working", **kw):
    data = {"title": title, "category": "Wi-Fi / Internet", "priority": "Medium", "location": "Test room", **kw}
    r = post(c, "/tickets/new", data, "/tickets/new")
    return int(r.location.rsplit("/", 1)[1]) if r.status_code == 302 else r


def row(sql, *args):
    with m.app.app_context():
        return m.q(sql, args, one=True)


def test_flow():
    tmp = tempfile.mkdtemp()
    m.DB = os.path.join(tmp, "h.db")
    m.AMS_DB = os.path.join(tmp, "ams.db")
    m.UPLOADS = os.path.join(tmp, "uploads")
    fake_ams(m.AMS_DB)
    m.init_db()
    c = m.app.test_client()

    assert c.get("/").location.endswith("/setup")
    assert m.app.config["SESSION_COOKIE_NAME"] == "helpdesk_session"
    r = c.post("/login", data={"username": "a", "password": "b"})  # no csrf
    assert r.status_code == 400 and b"form expired" in r.data.lower()
    post(c, "/setup", {"name": "Admin", "username": "admin", "password": "adminpass1"}, "/setup")
    assert login(c, "admin", "wrongpass").status_code == 200
    assert login(c, "admin", "adminpass1").status_code == 302
    for u, role, dept, team in [("tech", "technician", "", "IT Network"), ("staff", "staff", "CSE", ""), ("staff2", "staff", "Mechanical", ""),
                                ("coord", "coordinator", "CSE", ""), ("hod", "hod", "CSE", ""), ("auditor", "auditor", "", "")]:
        post(c, "/users/add", {"name": u.title(), "username": u, "role": role, "department": dept, "team": team, "password": u + "pass1234"}, "/users")
    pw = lambda u: u + "pass1234"
    assert b"Auditor (read only)" in c.get("/users").data and c.get("/users?q=coord").status_code == 200

    # places: add one, paste many (a bad line is reported, duplicates ignored)
    post(c, "/settings/locations/add", {"block": "B Block", "floor": "2nd floor", "room": "B206 Lab"}, "/settings")
    post(c, "/settings/locations/add", {"bulk": "B Block, 2nd floor, B207 Lab\nB Block, 3rd floor, B306\nnonsense\nB Block, 2nd floor, B206 Lab"}, "/settings")
    assert row("SELECT COUNT(*) n FROM locations")["n"] == 3
    page = c.get("/tickets/new").get_data(as_text=True)
    assert "loc-picker" in page and "B207 Lab" in page and "helpdesk.js" in page
    assert c.get("/settings").status_code == 200

    # staff raises a ticket against an AMS asset; place comes from the AMS; critical = 4 h; category decides the team
    login(c, "staff", pw("staff"))
    assert c.get("/users").status_code == 403 and c.get("/settings").status_code == 403 and c.get("/tickets/log").status_code == 403
    r = post(c, "/tickets/new", {"title": "System not working", "category": "Computer / hardware", "priority": "Critical", "asset_tag": "AMS-00013"}, "/tickets/new")
    tid = int(r.location.rsplit("/", 1)[1])
    page = c.get(f"/tickets/{tid}").get_data(as_text=True)
    assert "CSE Block · Computer Lab 1" in page and "L1-PC-04" in page
    t = row("SELECT * FROM tickets WHERE id=?", tid)
    assert t["team"] == "IT Systems" and t["department"] == "CSE" and t["status"] == "Open"
    assert datetime.strptime(t["due_at"], m.FMT) - datetime.strptime(t["created_at"], m.FMT) == timedelta(hours=4)
    assert b"Describe the problem" in post(c, "/tickets/new", {"title": " "}, "/tickets/new").data
    assert b"Say where it is" in post(c, "/tickets/new", {"title": "x"}, "/tickets/new").data

    # an exam in progress lifts a Low ticket to Critical
    urgent = raise_ticket(c, "Exam page not loading", priority="Low", urgent_reason="Exam in progress")
    assert row("SELECT priority FROM tickets WHERE id=?", urgent)["priority"] == "Critical"

    # staff can't work tickets; the ticket is private to its requester, their department's HOD/coordinator, and IT
    assert post(c, f"/tickets/{tid}/assign", {"take": "1"}, f"/tickets/{tid}").status_code == 403
    assert post(c, f"/tickets/{tid}/resolve", {"resolution": "x"}, f"/tickets/{tid}").status_code == 403
    login(c, "staff2", pw("staff2"))
    assert c.get(f"/tickets/{tid}").status_code == 404 and f"HD-{tid:04d}".encode() not in c.get("/tickets?scope=all").data
    for who in ("coord", "hod", "auditor", "tech", "admin"):
        login(c, who, pw(who) if who != "admin" else "adminpass1")
        assert c.get(f"/tickets/{tid}").status_code == 200, who
    login(c, "auditor", pw("auditor"))
    assert post(c, "/tickets/new", {"title": "x"}, "/").status_code == 403  # read only
    assert post(c, f"/tickets/{tid}/comment", {"body": "hi"}, f"/tickets/{tid}").status_code == 403

    # workflow: take it, ask the requester, requester answers, clock restarts; internal notes stay private
    login(c, "tech", pw("tech"))
    post(c, f"/tickets/{tid}/assign", {"take": "1"}, f"/tickets/{tid}")
    t = row("SELECT * FROM tickets WHERE id=?", tid)
    assert t["status"] == "In progress" and t["assigned_to"] and t["first_response_at"]
    due0 = t["due_at"]
    post(c, f"/tickets/{tid}/status", {"to": "Waiting on requester", "reason": " "}, f"/tickets/{tid}")
    assert row("SELECT status FROM tickets WHERE id=?", tid)["status"] == "In progress"  # reason required
    post(c, f"/tickets/{tid}/status", {"to": "Waiting on requester", "reason": "Which PC name?"}, f"/tickets/{tid}")
    assert row("SELECT status FROM tickets WHERE id=?", tid)["status"] == "Waiting on requester"
    assert b"Which PC name?" in c.get(f"/tickets/{tid}").data and b"clock paused" in c.get("/tickets?scope=all").data
    post(c, f"/tickets/{tid}/comment", {"body": "Check the cable first", "internal": "1"}, f"/tickets/{tid}")
    assert b"internal note" in c.get(f"/tickets/{tid}").data
    with m.app.app_context():  # pretend they waited an hour
        m.db().execute("UPDATE status_log SET at=? WHERE ticket_id=? AND to_status='Waiting on requester'",
                       ((datetime.now() - timedelta(hours=1)).strftime(m.FMT), tid))
        m.db().commit()
    login(c, "staff", pw("staff"))
    assert b"Check the cable first" not in c.get(f"/tickets/{tid}").data
    post(c, f"/tickets/{tid}/comment", {"body": "It is PC-04", "internal": "1"}, f"/tickets/{tid}")  # staff can't make internal notes
    assert row("SELECT internal FROM comments WHERE body='It is PC-04'")["internal"] == 0
    t = row("SELECT * FROM tickets WHERE id=?", tid)
    assert t["status"] == "In progress" and t["due_at"] > due0  # clock restarted, pushed out by the wait
    assert row("SELECT COUNT(*) n FROM status_log WHERE ticket_id=?", tid)["n"] == 4  # Open, In progress, Waiting, In progress

    # resolve needs a fix; parts are recorded; requester reopens once, then confirms
    login(c, "tech", pw("tech"))
    assert b"Write what was done" in c.get(post(c, f"/tickets/{tid}/resolve", {"resolution": " "}, f"/tickets/{tid}").location, follow_redirects=True).data
    post(c, f"/tickets/{tid}/priority", {"priority": "Low"}, f"/tickets/{tid}")
    t = row("SELECT * FROM tickets WHERE id=?", tid)
    assert datetime.strptime(t["due_at"], m.FMT) - datetime.strptime(t["created_at"], m.FMT) == timedelta(hours=168)
    post(c, f"/tickets/{tid}/team", {"team": "Electrical (EB)"}, f"/tickets/{tid}")
    assert row("SELECT team FROM tickets WHERE id=?", tid)["team"] == "Electrical (EB)"
    post(c, f"/tickets/{tid}/resolve", {"resolution": "Replaced power cable", "parts_used": "1 x cable"}, f"/tickets/{tid}")
    t = row("SELECT * FROM tickets WHERE id=?", tid)
    assert t["status"] == "Resolved" and t["parts_used"] == "1 x cable"
    login(c, "staff", pw("staff"))
    post(c, f"/tickets/{tid}/reopen", {}, f"/tickets/{tid}")
    assert row("SELECT status FROM tickets WHERE id=?", tid)["status"] == "Open"
    login(c, "tech", pw("tech"))
    post(c, f"/tickets/{tid}/resolve", {"resolution": "Replaced PSU"}, f"/tickets/{tid}")
    login(c, "staff2", pw("staff2"))
    post(c, f"/tickets/{tid}/close", {}, f"/tickets/{tid}")  # not theirs: ignored (and invisible)
    assert row("SELECT status FROM tickets WHERE id=?", tid)["status"] == "Resolved"
    login(c, "coord", pw("coord"))  # the department coordinator may confirm for their department
    post(c, f"/tickets/{tid}/close", {}, f"/tickets/{tid}")
    assert row("SELECT status FROM tickets WHERE id=?", tid)["status"] == "Closed"

    # new requirement: no deadline, goes to the procurement list, closed when delivered
    login(c, "hod", pw("hod"))
    req = raise_ticket(c, "Smart board for CAD lab", request_type="New requirement", category="Smart board / projector / AV")
    assert row("SELECT due_at, request_type FROM tickets WHERE id=?", req)["due_at"] is None
    login(c, "tech", pw("tech"))
    post(c, f"/tickets/{req}/status", {"to": "Planned", "reason": "Next purchase"}, f"/tickets/{req}")
    assert row("SELECT status FROM tickets WHERE id=?", req)["status"] == "Planned"
    assert f"HD-{req:04d}".encode() in c.get("/tickets?scope=planned").data
    post(c, f"/tickets/{req}/close", {}, f"/tickets/{req}")
    assert row("SELECT status FROM tickets WHERE id=?", req)["status"] == "Closed"

    # repeat detection: the third report from one place in 30 days says so
    login(c, "staff", pw("staff"))
    raise_ticket(c, "Wi-Fi down", location="A106 HOD room")
    raise_ticket(c, "Wi-Fi down again", location="A106 HOD room")
    r = post(c, "/tickets/new", {"title": "Wi-Fi down a third time", "category": "Wi-Fi / Internet", "location": "A106 HOD room"}, "/tickets/new")
    third = int(r.location.rsplit("/", 1)[1])
    assert "3rd Wi-Fi / Internet report" in c.get(r.location).get_data(as_text=True)
    assert "3 in 30 days" in c.get("/tickets").get_data(as_text=True)

    # one outage, several reports: link them, resolve once
    login(c, "staff2", pw("staff2"))
    pid = raise_ticket(c, "No internet in B206", location_id="1")
    kids = [raise_ticket(c, "No internet in B207", location_id="2"), raise_ticket(c, "No internet B306", location_id="3")]
    login(c, "tech", pw("tech"))
    page = c.get(f"/tickets/{pid}").get_data(as_text=True)
    assert "Possibly the same problem" in page and f"HD-{kids[0]:04d}" in page
    for k in kids:
        post(c, f"/tickets/{k}/link", {"parent": str(pid)}, f"/tickets/{k}")
    assert row("SELECT COUNT(*) n FROM tickets WHERE parent_id=?", pid)["n"] == 2
    assert b"outage" in c.get("/tickets").data and b"Active by team" in c.get("/").data
    assert b"Pick another ticket" in c.get(post(c, f"/tickets/{pid}/link", {"parent": str(kids[0])}, f"/tickets/{pid}").location, follow_redirects=True).data
    post(c, f"/tickets/{pid}/resolve", {"resolution": "Switch rebooted", "also_linked": "1"}, f"/tickets/{pid}")
    assert row("SELECT COUNT(*) n FROM tickets WHERE parent_id=? AND status='Resolved'", pid)["n"] == 2

    # technician logs finished work: closed, in their name, first response counted
    r = post(c, "/tickets/log", {"title": "Replaced RAM", "category": "Computer / hardware", "location": "Lab 3", "resolution": "Fitted 8 GB", "parts_used": "8 GB RAM",
                                 "requester_name": "Dr. X"}, "/tickets/log")
    logged = row("SELECT * FROM tickets WHERE id=?", int(r.location.rsplit("/", 1)[1]))
    assert logged["status"] == "Closed" and logged["source"] == "Logged by technician" and logged["first_response_at"] and logged["requester_name"] == "Dr. X"
    assert b"Write what was done" in post(c, "/tickets/log", {"title": "x", "location": "y"}, "/tickets/log").data

    # quiet tickets are flagged
    with m.app.app_context():
        m.db().execute("UPDATE tickets SET updated_at='2020-01-01 00:00:00' WHERE id=?", (third,))
        m.db().commit()
    assert f"HD-{third:04d}".encode() in c.get("/tickets?stale=1").data and b"Waiting on requester" in c.get("/").data

    # search by HD number; CSV export is for admin and auditor only
    assert f"HD-{tid:04d}".encode() in c.get(f"/tickets?scope=all&q=HD-{tid:04d}").data
    assert c.get("/tickets?export=csv").status_code == 403
    login(c, "admin", "adminpass1")
    csv_ = c.get("/tickets?scope=all&export=csv")
    assert csv_.status_code == 200 and csv_.data.startswith(b"ticket,type,title") and b"Logged by technician" in csv_.data
    login(c, "auditor", pw("auditor"))
    assert c.get("/tickets?export=csv").status_code == 200

    # photos on a ticket and a comment; fake images refused; only people who can see the ticket can open them
    login(c, "staff", pw("staff"))
    png = bytes.fromhex("89504e470d0a1a0a") + b"0" * 20
    r = c.post("/tickets/new", data={"csrf": csrf(c, "/tickets/new"), "title": "Screen cracked", "category": "Computer / hardware", "location": "Lab 1",
                                     "photos": [(io.BytesIO(png), "a.png"), (io.BytesIO(b"<html>"), "b.png")]}, content_type="multipart/form-data")
    pid2 = int(r.location.rsplit("/", 1)[1])
    page = c.get(f"/tickets/{pid2}").get_data(as_text=True)
    name = re.search(r"/photos/([0-9a-f]{32}[.]png)", page).group(1)
    assert "skipped" in page and c.get(f"/photos/{name}").data == png and c.get("/photos/..%2Fapp.py").status_code == 404
    assert m.app.test_client().get(f"/photos/{name}").status_code == 302
    c.post(f"/tickets/{pid2}/comment", data={"csrf": csrf(c, f"/tickets/{pid2}"), "photos": (io.BytesIO(png), "c.png")}, content_type="multipart/form-data")
    page = c.get(f"/tickets/{pid2}").get_data(as_text=True)
    assert page.count("<img") >= 2 and "Added 1 photo" in page
    big = io.BytesIO(b"x" * (13 * 1024 * 1024))
    r = c.post(f"/tickets/{pid2}/comment", data={"csrf": csrf(c, f"/tickets/{pid2}"), "photos": (big, "big.png")}, content_type="multipart/form-data")
    assert r.status_code == 413 and b"too big" in r.data
    login(c, "staff2", pw("staff2"))
    assert c.get(f"/photos/{name}").status_code == 404

    # pagination + every page renders for every role
    login(c, "admin", "adminpass1")
    m.PER_PAGE = 2
    assert "Page 2 of" in c.get("/tickets?scope=all&page=2").data.decode()
    assert c.get("/tickets?scope=all&page=999").status_code == 200
    m.PER_PAGE = 50
    for who in ("admin", "tech", "staff", "coord", "hod", "auditor"):
        login(c, who, "adminpass1" if who == "admin" else pw(who))
        for page in ["/", "/tickets", "/tickets?scope=all&mine=1&unassigned=1&overdue=1&stale=1", "/tickets/new" if who != "auditor" else "/account", f"/tickets/{tid}" if who != "staff2" else "/", "/account"]:
            assert c.get(page).status_code == 200, (who, page)
    assert c.get("/tickets/9999").status_code == 404

    # works with no AMS present
    m.AMS_DB = os.path.join(tmp, "missing.db")
    login(c, "admin", "adminpass1")
    assert c.get("/tickets/new").status_code == 200


def test_old_database_upgrades():
    """A database from the first release (3 roles, old categories) is upgraded in place."""
    old = os.path.join(tempfile.mkdtemp(), "old.db")
    with sqlite3.connect(old) as c:
        c.executescript("""
          CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','technician','staff')));
          CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, category TEXT, priority TEXT,
            status TEXT NOT NULL DEFAULT 'Open', location TEXT, asset_tag TEXT, raised_by INTEGER REFERENCES users(id),
            assigned_to INTEGER REFERENCES users(id), created_at TEXT, due_at TEXT, resolved_at TEXT, resolution TEXT);
          CREATE TABLE comments (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id), body TEXT NOT NULL, system INTEGER DEFAULT 0, at TEXT);
          INSERT INTO users VALUES (1, 'admin', 'Admin', 'x', 'admin');
          INSERT INTO tickets(id, title, category, priority, status, raised_by, created_at, due_at) VALUES (1, 'old one', 'Network', 'High', 'Open', 1, '2026-01-01 09:00:00', '2026-01-02 09:00:00');""")
    m.DB = old
    m.init_db()
    m.init_db()  # idempotent
    with sqlite3.connect(old) as c:
        c.row_factory = sqlite3.Row
        t = c.execute("SELECT * FROM tickets WHERE id=1").fetchone()
        assert (t["category"], t["team"], t["request_type"], t["updated_at"]) == ("Wi-Fi / Internet", "IT Network", "Fault", "2026-01-01 09:00:00")
        c.execute("INSERT INTO users(username, name, password_hash, role) VALUES ('h', 'H', 'x', 'hod')")  # new roles allowed now
        assert c.execute("SELECT COUNT(*) FROM departments").fetchone()[0] > 10
        assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_demo_data():
    m.DB = os.path.join(tempfile.mkdtemp(), "demo.db")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    for u, _, role, _, _ in m.DEMO_USERS:
        assert login(c, u, f"demo-{u}-2026").status_code == 302, u
        for page in ["/", "/tickets?scope=all", "/tickets/1", "/tickets/new" if role != "auditor" else "/account"]:
            assert c.get(page).status_code in (200, 404), (u, page)
    login(c, "admin", "demo-admin-2026")
    assert b"in 30 days" in c.get("/tickets?scope=all").data and b"Active by priority" in c.get("/").data
    assert b"procurement" in c.get("/tickets?scope=planned").data.lower()


def test_ams_over_the_web():
    """On separate servers the asset details come from the AMS's read-only API; if it is asleep, tickets still work."""
    m.DB = os.path.join(tempfile.mkdtemp(), "w.db")
    m.AMS_DB = os.path.join(tempfile.mkdtemp(), "none.db")
    m.init_db()
    c = m.app.test_client()
    post(c, "/setup", {"name": "Admin", "username": "admin", "password": "adminpass1"}, "/setup")
    login(c, "admin", "adminpass1")
    fake = {"AMS-00013": {"tag": "AMS-00013", "name": "L1-PC-04", "status": "Faulty", "building": "CSE Block", "room": "Computer Lab 1"}}
    asked = []

    def ams_http(path):
        asked.append(path)
        if path.startswith("/api/v1/assets?"):
            return {"count": 1, "items": list(fake.values())}
        return fake.get(path.rsplit("/", 1)[1])

    real = (m.AMS_URL, m.AMS_TOKEN, m.ams_http)
    m.AMS_URL, m.AMS_TOKEN, m.ams_http = "https://ams.example", "tok", ams_http
    m._choices_cache.update(at=0, rows=[])
    try:
        assert b"AMS-00013" in c.get("/tickets/new").data and any(p.startswith("/api/v1/assets?") for p in asked)
        r = post(c, "/tickets/new", {"title": "System not working", "asset_tag": "AMS-00013"}, "/tickets/new")
        tid = int(r.location.rsplit("/", 1)[1])
        page = c.get(f"/tickets/{tid}").get_data(as_text=True)
        assert "CSE Block · Computer Lab 1" in page and "L1-PC-04" in page
        assert b"not found in the AMS" in c.get(f"/tickets/{int(post(c, '/tickets/new', {'title': 'x', 'asset_tag': 'NOPE', 'location': 'Lab'}, '/tickets/new').location.rsplit('/', 1)[1])}").data
        m.ams_http = lambda path: None  # the AMS is asleep
        m._choices_cache.update(at=0, rows=[])
        assert c.get("/tickets/new").status_code == 200
        assert post(c, "/tickets/new", {"title": "Still works", "asset_tag": "AMS-00013", "location": "Lab 1"}, "/tickets/new").status_code == 302
    finally:
        m.AMS_URL, m.AMS_TOKEN, m.ams_http = real


def test_notifications_analytics_maintenance_writeback():
    import hd_extra as x
    m.DB = os.path.join(tempfile.mkdtemp(), "x.db")
    m.init_db()
    m.seed_demo()
    x.last_check["at"] = time.time()  # keep the due-jobs check out of this test
    c = m.app.test_client()
    login(c, "admin", "demo-admin-2026")

    # nothing is configured: nothing is sent, and the page says so
    for k in ("HD_CHAT_WEBHOOK", "HD_SMTP_HOST", "HD_SMTP_FROM", "HD_AMS_WRITEBACK", "AMS_URL", "AMS_API_TOKEN"):
        os.environ.pop(k, None)
    assert b"Google Chat space" in c.get("/settings/notifications").data
    assert b"Nothing is set up" in c.get(post(c, "/settings/notifications", {}, "/settings/notifications").location, follow_redirects=True).data

    # configured: a new ticket reaches the chat space and the team's technicians; assign and resolve email the right people
    sent = {"chat": [], "mail": [], "ams": []}
    real = (x.send_chat, x.send_mail, x.send_ams)
    x.send_chat = lambda url, text: sent["chat"].append((url, text))
    x.send_mail = lambda msg: sent["mail"].append(msg)
    x.send_ams = lambda path, payload: sent["ams"].append((path, payload))
    os.environ.update(HD_CHAT_WEBHOOK="https://chat.example/hook", HD_SMTP_HOST="smtp.example", HD_SMTP_FROM="helpdesk@example.edu",
                      HD_AMS_WRITEBACK="1", AMS_URL="https://ams.example", AMS_API_TOKEN="tok")
    try:
        ids = {u: i for i, u in sqlite3.connect(m.DB).execute("SELECT id, username FROM users")}
        post(c, f"/users/{ids['tech']}/update", {"role": "technician", "team": "IT Network", "department": "", "email": "ravi@example.edu"}, "/users")
        post(c, f"/users/{ids['staff']}/update", {"role": "staff", "department": "CSE", "email": "priya@example.edu"}, "/users")
        assert row("SELECT email FROM users WHERE username='tech'")["email"] == "ravi@example.edu"
        login(c, "staff", "demo-staff-2026")
        tid = raise_ticket(c, "Lab Wi-Fi down", priority="Critical", asset_tag="AMS-00013", request_type="Fault", category="Wi-Fi / Internet")
        x.wait()
        assert len(sent["chat"]) == 1 and "CRITICAL" in sent["chat"][0][1] and f"HD-{tid:04d}" in sent["chat"][0][1]
        assert [msg["To"] for msg in sent["mail"]] == ["ravi@example.edu"] and "Lab Wi-Fi down" in sent["mail"][0]["Subject"]
        assert sent["ams"][0][0] == "/api/v1/assets/AMS-00013/ticket-event" and sent["ams"][0][1]["event"] == "opened"
        login(c, "admin", "demo-admin-2026")
        post(c, f"/tickets/{tid}/assign", {"assigned_to": str(ids["tech"])}, f"/tickets/{tid}")
        x.wait()
        assert sent["mail"][-1]["To"] == "ravi@example.edu" and "assigned to you" in sent["mail"][-1]["Subject"]
        n = len(sent["mail"])
        post(c, f"/tickets/{tid}/resolve", {"resolution": "Access point rebooted"}, f"/tickets/{tid}")
        x.wait()
        assert len(sent["mail"]) == n + 1 and sent["mail"][-1]["To"] == "priya@example.edu" and "please confirm" in sent["mail"][-1]["Subject"]
        assert sent["ams"][-1][1]["event"] == "resolved" and sent["ams"][-1][1]["mark_working"] is True
        assert row("SELECT COUNT(*) n FROM notify_log WHERE ok=1")["n"] >= 4
        # a failing mail server is logged and never breaks the ticket
        x.send_mail = lambda msg: (_ for _ in ()).throw(OSError("smtp down"))
        login(c, "staff", "demo-staff-2026")
        assert isinstance(raise_ticket(c, "Another one"), int)
        x.wait()
        assert row("SELECT error FROM notify_log WHERE ok=0")["error"] == "smtp down"
        login(c, "admin", "demo-admin-2026")
        assert b"smtp down" in c.get("/settings/notifications").data
        # a newline in a title can't add a mail header
        x.send_mail = lambda msg: sent["mail"].append(msg)
        raise_ticket(c, "Bad\nBcc: evil@example.com")
        x.wait()
        assert all("Bcc" not in msg and "\n" not in msg["Subject"] for msg in sent["mail"])
    finally:
        x.send_chat, x.send_mail, x.send_ams = real
        for k in ("HD_CHAT_WEBHOOK", "HD_SMTP_HOST", "HD_SMTP_FROM", "HD_AMS_WRITEBACK", "AMS_URL", "AMS_API_TOKEN"):
            os.environ.pop(k, None)

    # two open tickets on one asset: fixing the first must not mark the asset working
    os.environ.update(HD_AMS_WRITEBACK="1", AMS_URL="https://ams.example", AMS_API_TOKEN="tok")
    x.send_ams = lambda path, payload: sent["ams"].append((path, payload))
    try:
        a = raise_ticket(c, "Screen flickers", asset_tag="AMS-00099", request_type="Fault")
        b = raise_ticket(c, "Keyboard dead", asset_tag="AMS-00099", request_type="Fault")
        post(c, f"/tickets/{a}/resolve", {"resolution": "Cable"}, f"/tickets/{a}")
        x.wait()
        assert sent["ams"][-1][1]["mark_working"] is False
        post(c, f"/tickets/{b}/resolve", {"resolution": "Keyboard"}, f"/tickets/{b}")
        x.wait()
        assert sent["ams"][-1][1]["mark_working"] is True
    finally:
        x.send_ams = real[2]
        for k in ("HD_AMS_WRITEBACK", "AMS_URL", "AMS_API_TOKEN"):
            os.environ.pop(k, None)

    # analytics: IT and admins see it, the department head sees only the department, staff are refused
    page = c.get("/analytics?days=90").get_data(as_text=True)
    assert "Fixed on time" in page and "By technician" in page and "Places with repeat" in page
    login(c, "hod", "demo-hod-2026")
    assert c.get("/analytics").status_code == 200 and b"By technician" not in c.get("/analytics").data
    login(c, "staff", "demo-staff-2026")
    assert c.get("/analytics").status_code == 403 and c.get("/maintenance").status_code == 403 and c.get("/settings/notifications").status_code == 403

    # planned maintenance: add, raise now, pause, delete
    login(c, "tech", "demo-tech-2026")
    assert b"Add a repeating job" in c.get("/maintenance").data
    past = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    assert b"Give a title" in c.get(post(c, "/maintenance/add", {"title": "", "every_days": "90"}, "/maintenance").location, follow_redirects=True).data
    post(c, "/maintenance/add", {"title": "Check UPS batteries", "every_days": "90", "next_due": past, "category": "Power / UPS", "location": "A001 Server room"}, "/maintenance")
    post(c, "/maintenance/add", {"title": "Clean filters", "every_days": "30", "location": "B101"}, "/maintenance")
    before = row("SELECT COUNT(*) n FROM tickets")["n"]
    mid = row("SELECT id FROM maintenance WHERE title='Check UPS batteries'")["id"]
    post(c, f"/maintenance/{mid}/now", {}, "/maintenance")
    assert row("SELECT COUNT(*) n FROM tickets")["n"] == before + 1
    t = row("SELECT * FROM tickets ORDER BY id DESC LIMIT 1")
    assert t["source"] == "Planned maintenance" and t["request_type"] == "Service request" and t["title"] == "Check UPS batteries"
    assert row("SELECT last_ticket_id FROM maintenance WHERE id=?", mid)["last_ticket_id"] == t["id"]
    other = row("SELECT id FROM maintenance WHERE title='Clean filters'")["id"]
    post(c, f"/maintenance/{other}/pause", {}, "/maintenance")
    assert row("SELECT active FROM maintenance WHERE id=?", other)["active"] == 0
    post(c, f"/maintenance/{other}/delete", {}, "/maintenance")
    assert row("SELECT COUNT(*) n FROM maintenance")["n"] == 1
    assert c.post(f"/maintenance/{mid}/bogus", data={"csrf": csrf(c, "/maintenance")}).status_code == 404


def test_due_maintenance_runs_by_itself():
    import hd_extra as x
    m.DB = os.path.join(tempfile.mkdtemp(), "mt.db")
    m.init_db()
    m.seed_demo()
    x.last_check["at"] = time.time()
    c = m.app.test_client()
    login(c, "admin", "demo-admin-2026")
    past = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    post(c, "/maintenance/add", {"title": "Check UPS", "every_days": "90", "next_due": past, "location": "A001 Server room"}, "/maintenance")
    n = row("SELECT COUNT(*) n FROM tickets")["n"]
    c.get("/")  # inside the throttle: nothing yet
    assert row("SELECT COUNT(*) n FROM tickets")["n"] == n
    x.last_check["at"] = 0
    c.get("/")
    c.get("/")  # a second visit right after: no second ticket
    assert row("SELECT COUNT(*) n FROM tickets")["n"] == n + 1  # 200 days overdue is one ticket, not two
    t = row("SELECT * FROM tickets ORDER BY id DESC LIMIT 1")
    assert t["title"] == "Check UPS" and t["source"] == "Planned maintenance"
    assert row("SELECT next_due FROM maintenance")["next_due"] > datetime.now().strftime("%Y-%m-%d")


def test_daily_reminder():
    import hd_extra as x
    m.DB = os.path.join(tempfile.mkdtemp(), "r.db")
    m.init_db()
    m.seed_demo()
    with sqlite3.connect(m.DB) as db:
        db.execute("UPDATE users SET email='kumar@example.edu' WHERE username='tech2'")
    c = m.app.test_client()
    x.last_check["at"] = time.time()
    login(c, "admin", "demo-admin-2026")
    sent = {"chat": [], "mail": []}
    real = (x.send_chat, x.send_mail)
    x.send_chat = lambda url, text: sent["chat"].append(text)
    x.send_mail = lambda msg: sent["mail"].append(msg)
    try:
        x.last_check["at"] = 0
        c.get("/")  # nothing configured: nothing sent
        x.wait()
        assert sent == {"chat": [], "mail": []}
        os.environ.update(HD_CHAT_WEBHOOK="https://chat.example/hook", HD_SMTP_HOST="smtp.example", HD_SMTP_FROM="helpdesk@example.edu")
        x.last_check["at"] = 0
        c.get("/")
        x.wait()
        assert len(sent["chat"]) == 1 and "Daily reminder" in sent["chat"][0] and "overdue" in sent["chat"][0]
        assert [msg["To"] for msg in sent["mail"]] == ["kumar@example.edu"] and "need you" in sent["mail"][0]["Subject"]
        assert row("SELECT COUNT(*) n FROM notify_log WHERE channel LIKE 'reminder%'")["n"] == 2
        x.last_check["at"] = 0
        c.get("/")  # the same day again: not sent twice
        x.wait()
        assert len(sent["chat"]) == 1 and len(sent["mail"]) == 1
    finally:
        x.send_chat, x.send_mail = real
        for k in ("HD_CHAT_WEBHOOK", "HD_SMTP_HOST", "HD_SMTP_FROM"):
            os.environ.pop(k, None)


def test_service_rules_and_auto_assign():
    import hd_extra as x
    m.DB = os.path.join(tempfile.mkdtemp(), "rules.db")
    m.init_db()
    m.seed_demo()
    x.last_check["at"] = time.time()
    c = m.app.test_client()
    login(c, "admin", "demo-admin-2026")
    page = c.get("/settings").get_data(as_text=True)
    assert "Service rules" in page and 'name="sla_Critical"' in page and 'value="4"' in page

    # bad values are refused and nothing changes
    bad = post(c, "/settings/rules", {"sla_Critical": "0", "sla_High": "24", "sla_Medium": "72", "sla_Low": "168", "stale_hours": "24"}, "/settings")
    assert b"whole numbers" in c.get(bad.location, follow_redirects=True).data
    assert row("SELECT COUNT(*) n FROM app_settings")["n"] == 0

    # new fix-by hours apply to new tickets; the quiet limit shows in the filters
    post(c, "/settings/rules", {"sla_Critical": "2", "sla_High": "12", "sla_Medium": "48", "sla_Low": "120", "stale_hours": "8"}, "/settings")
    tid = raise_ticket(c, "Projector dead", priority="High", location="Lab 3")
    t = row("SELECT created_at, due_at FROM tickets WHERE id=?", tid)
    hours = (datetime.strptime(t["due_at"], m.FMT) - datetime.strptime(t["created_at"], m.FMT)).total_seconds() / 3600
    assert round(hours) == 12
    assert b"No update 8h" in c.get("/tickets").data

    # auto-assign is off by default, then picks the least busy technician of the ticket's team
    assert row("SELECT assigned_to FROM tickets WHERE id=?", tid)["assigned_to"] is None
    post(c, "/settings/rules", {"sla_Critical": "4", "sla_High": "24", "sla_Medium": "72", "sla_Low": "168", "stale_hours": "24", "auto_assign": "1"}, "/settings")
    login(c, "staff", "demo-staff-2026")
    tid = raise_ticket(c, "Wi-Fi down in lab", category="Wi-Fi / Internet", location="Lab 3")
    t = row("SELECT t.assigned_to, u.team, t.status FROM tickets t JOIN users u ON u.id=t.assigned_to WHERE t.id=?", tid)
    assert t["team"] == "IT Network" and t["status"] == "Open"
    assert row("SELECT 1 FROM comments WHERE ticket_id=? AND body LIKE 'Assigned to % (automatically%'", tid)
    tid = raise_ticket(c, "Strange request", category="Other", location="Lab 3")  # a team with no technician: left unassigned
    assert row("SELECT assigned_to FROM tickets WHERE id=?", tid)["assigned_to"] is None
    login(c, "tech", "demo-tech-2026")
    assert post(c, "/settings/rules", {"stale_hours": "1"}, "/").status_code == 403


def test_remove_demo_accounts():
    m.DB = os.path.join(tempfile.mkdtemp(), "rd.db")
    m.init_db()
    m.seed_demo()
    assert "Not removed" in m.remove_demo()
    with sqlite3.connect(m.DB) as c:
        c.execute("INSERT INTO users(username, name, password_hash, role) VALUES ('itadmin', 'IT admin', 'x', 'admin')")
    n_tickets = row("SELECT COUNT(*) n FROM tickets")["n"]
    assert "Removed" in m.remove_demo()
    assert row("SELECT GROUP_CONCAT(username) u FROM users")["u"] == "itadmin"
    assert row("SELECT COUNT(*) n FROM tickets")["n"] == n_tickets  # tickets stay


def test_upload_virus_scan():
    m.DB = os.path.join(tempfile.mkdtemp(), "v.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "staff", "demo-staff-2026")
    png = bytes.fromhex("89504e470d0a1a0a") + b"0" * 20
    real_run = m.subprocess.run
    send = lambda: c.post("/tickets/new", data={"csrf": csrf(c, "/tickets/new"), "title": "Broken", "location": "Lab",
                                                "photos": [(io.BytesIO(png), "a.png")]}, content_type="multipart/form-data")
    try:
        os.environ["UPLOAD_SCAN"] = "1"
        m.subprocess.run = lambda cmd, **kw: m.subprocess.CompletedProcess(cmd, 1)  # infected
        send()
        assert row("SELECT COUNT(*) n FROM attachments")["n"] == 0 and not os.listdir(m.UPLOADS)
        m.subprocess.run = lambda cmd, **kw: m.subprocess.CompletedProcess(cmd, 0)  # clean
        send()
        assert row("SELECT COUNT(*) n FROM attachments")["n"] == 1
    finally:
        m.subprocess.run = real_run
        os.environ.pop("UPLOAD_SCAN", None)


def test_login_lockout():
    m.DB = os.path.join(tempfile.mkdtemp(), "lk.db")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    for _ in range(5):
        login(c, "admin", "nope")
    r = post(c, "/login", {"username": "admin", "password": "demo-admin-2026"}, "/login")
    assert r.status_code == 200 and b"Too many wrong passwords" in r.data
    with sqlite3.connect(m.DB) as db:
        db.execute("UPDATE login_failures SET at=datetime('now', 'localtime', '-20 minutes')")
    assert login(c, "admin", "demo-admin-2026").status_code == 302


def test_rating_auto_close_and_health():
    import hd_extra as x
    m.DB = os.path.join(tempfile.mkdtemp(), "rc.db")
    m.init_db()
    m.seed_demo()
    x.last_check["at"] = time.time()
    c = m.app.test_client()
    assert c.get("/healthz").data == b"ok"  # no sign-in needed, shows nothing else

    # the requester rates the fix when confirming it; nobody else can, and odd values are ignored
    login(c, "staff", "demo-staff-2026")
    a, b = raise_ticket(c, "Mouse broken", location="Lab 1"), raise_ticket(c, "Keyboard broken", location="Lab 1")
    login(c, "tech", "demo-tech-2026")
    for tid in (a, b):
        post(c, f"/tickets/{tid}/resolve", {"resolution": "Replaced"}, f"/tickets/{tid}")
    post(c, f"/tickets/{b}/close", {"rating": "1"}, f"/tickets/{b}")  # IT closing: no rating recorded
    assert row("SELECT status, rating FROM tickets WHERE id=?", b)["rating"] is None
    login(c, "staff", "demo-staff-2026")
    assert b"Rate the fix" in c.get(f"/tickets/{a}").data
    post(c, f"/tickets/{a}/close", {"rating": "4"}, f"/tickets/{a}")
    t = row("SELECT status, rating FROM tickets WHERE id=?", a)
    assert (t["status"], t["rating"]) == ("Closed", 4)
    assert b"Rated 4/5" in c.get(f"/tickets/{a}").data
    login(c, "admin", "demo-admin-2026")
    assert b"4.0 / 5" in c.get("/analytics").data

    # auto-close: off by default; when on, a resolved ticket with no answer for N days is closed with a note
    login(c, "tech", "demo-tech-2026")
    d = raise_ticket(c, "Old fix", location="Lab 2")
    post(c, f"/tickets/{d}/resolve", {"resolution": "Done"}, f"/tickets/{d}")
    with sqlite3.connect(m.DB) as db:
        db.execute("UPDATE tickets SET resolved_at=datetime('now', 'localtime', '-5 days') WHERE id=?", (d,))
    x.last_check["at"] = 0
    c.get("/")
    assert row("SELECT status FROM tickets WHERE id=?", d)["status"] == "Resolved"
    login(c, "admin", "demo-admin-2026")
    bad = post(c, "/settings/rules", {"sla_Critical": "4", "sla_High": "24", "sla_Medium": "72", "sla_Low": "168", "stale_hours": "24", "auto_close_days": "99"}, "/settings")
    assert b"from 0 (never) to 60" in c.get(bad.location, follow_redirects=True).data
    post(c, "/settings/rules", {"sla_Critical": "4", "sla_High": "24", "sla_Medium": "72", "sla_Low": "168", "stale_hours": "24", "auto_close_days": "3"}, "/settings")
    x.last_check["at"] = 0
    c.get("/")
    assert row("SELECT status FROM tickets WHERE id=?", d)["status"] == "Closed"
    assert row("SELECT 1 FROM comments WHERE ticket_id=? AND body LIKE 'Closed automatically%'", d)
    assert row("SELECT status FROM tickets WHERE id=?", a)["status"] == "Closed"  # untouched
    x.last_check["at"] = time.time()


def test_passwords_and_idle_sign_out():
    m.DB = os.path.join(tempfile.mkdtemp(), "pw.db")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "admin", "demo-admin-2026")
    post(c, "/users/add", {"name": "New", "username": "newperson1", "role": "staff", "password": "password123"}, "/users")
    assert row("SELECT 1 FROM users WHERE username='newperson1'") is None
    post(c, "/users/add", {"name": "New", "username": "newperson1", "role": "staff", "password": "Temp-Pass-2026"}, "/users")
    assert row("SELECT must_change FROM users WHERE username='newperson1'")["must_change"] == 1
    c.post("/logout", data={"csrf": csrf(c, "/login")})
    post(c, "/login", {"username": "newperson1", "password": "Temp-Pass-2026"}, "/login")
    assert c.get("/tickets/new").location.endswith("/account")
    post(c, "/account", {"current": "Temp-Pass-2026", "new": "blue kettle morning"}, "/account")
    assert c.get("/tickets/new").status_code == 200
    with c.session_transaction() as s:
        s["seen"] = int(time.time()) - 9 * 3600
    assert "/login" in c.get("/").location


def test_admin_change_record():
    m.DB = os.path.join(tempfile.mkdtemp(), "al.db")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "admin", "demo-admin-2026")
    post(c, "/users/add", {"name": "Meena", "username": "meena", "role": "staff", "password": "Secret-Words-99"}, "/users")
    post(c, "/settings/departments/add", {"name": "Robotics"}, "/settings")
    post(c, "/settings/rules", {"sla_Critical": "2", "sla_High": "24", "sla_Medium": "72", "sla_Low": "168", "stale_hours": "24"}, "/settings")
    acts = [r[0].split(" (")[0] for r in sqlite3.connect(m.DB).execute("SELECT action FROM admin_log ORDER BY id")]
    assert acts == ["Added a user", "Added a department", "Changed the service rules"], acts
    assert "Secret" not in "".join(r[0] or "" for r in sqlite3.connect(m.DB).execute("SELECT detail FROM admin_log"))
    assert b"Changed the service rules" in c.get("/users").data


if __name__ == "__main__":
    test_flow()
    test_old_database_upgrades()
    test_demo_data()
    test_ams_over_the_web()
    test_notifications_analytics_maintenance_writeback()
    test_due_maintenance_runs_by_itself()
    test_daily_reminder()
    test_service_rules_and_auto_assign()
    test_remove_demo_accounts()
    test_upload_virus_scan()
    test_login_lockout()
    test_passwords_and_idle_sign_out()
    test_admin_change_record()
    test_rating_auto_close_and_health()
    print("ok")
