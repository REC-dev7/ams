"""Smoke test: python test_helpdesk.py"""
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta

import app as m


def csrf(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, page="/"):
    return c.post(path, data={**data, "csrf": csrf(c, page)})


def login(c, user, pw):
    c.post("/logout", data={"csrf": csrf(c, "/login")})
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


def test_flow():
    tmp = tempfile.mkdtemp()
    m.DB = os.path.join(tmp, "h.db")
    m.AMS_DB = os.path.join(tmp, "ams.db")
    fake_ams(m.AMS_DB)
    m.init_db()
    c = m.app.test_client()

    assert c.get("/").location.endswith("/setup")
    post(c, "/setup", {"name": "Admin", "username": "admin", "password": "adminpass1"}, "/setup")
    assert login(c, "admin", "wrongpass").status_code == 200
    assert login(c, "admin", "adminpass1").status_code == 302
    post(c, "/users/add", {"name": "Tech", "username": "tech", "role": "technician", "password": "techpass12"}, "/users")
    post(c, "/users/add", {"name": "Staff", "username": "staff", "role": "staff", "password": "staffpass1"}, "/users")

    # staff raises a ticket against an AMS asset; location filled in from the AMS
    login(c, "staff", "staffpass1")
    assert c.get("/users").status_code == 403
    r = post(c, "/tickets/new", {"title": "System not working", "category": "Hardware", "priority": "Critical",
                                 "asset_tag": "AMS-00013"}, "/tickets/new")
    assert r.status_code == 302
    tid = int(r.location.rsplit("/", 1)[1])
    page = c.get(f"/tickets/{tid}").get_data(as_text=True)
    assert "CSE Block · Computer Lab 1" in page and "L1-PC-04" in page
    assert b"Describe the problem" in post(c, "/tickets/new", {"title": " "}, "/tickets/new").data

    # SLA: critical = 4 hours
    with m.app.app_context():
        t = m.q("SELECT * FROM tickets WHERE id=?", (tid,), one=True)
        gap = datetime.strptime(t["due_at"], m.FMT) - datetime.strptime(t["created_at"], m.FMT)
        assert gap == timedelta(hours=4)

    # staff can't assign/resolve; technician can
    assert post(c, f"/tickets/{tid}/assign", {"take": "1"}, f"/tickets/{tid}").status_code == 403
    assert post(c, f"/tickets/{tid}/resolve", {"resolution": "x"}, f"/tickets/{tid}").status_code == 403
    login(c, "tech", "techpass12")
    post(c, f"/tickets/{tid}/assign", {"take": "1"}, f"/tickets/{tid}")
    with m.app.app_context():
        t = m.q("SELECT status, assigned_to FROM tickets WHERE id=?", (tid,), one=True)
        assert t["status"] == "In progress" and t["assigned_to"]
    assert b"Write what was done" in c.get(post(c, f"/tickets/{tid}/resolve", {"resolution": " "}, f"/tickets/{tid}").location, follow_redirects=True).data
    post(c, f"/tickets/{tid}/priority", {"priority": "Low"}, f"/tickets/{tid}")
    with m.app.app_context():
        t = m.q("SELECT * FROM tickets WHERE id=?", (tid,), one=True)
        assert datetime.strptime(t["due_at"], m.FMT) - datetime.strptime(t["created_at"], m.FMT) == timedelta(hours=168)
    post(c, f"/tickets/{tid}/resolve", {"resolution": "Replaced power cable"}, f"/tickets/{tid}")

    # raiser reopens (not fixed), tech resolves again, raiser confirms
    login(c, "staff", "staffpass1")
    post(c, f"/tickets/{tid}/reopen", {}, f"/tickets/{tid}")
    with m.app.app_context():
        assert m.q("SELECT status FROM tickets WHERE id=?", (tid,), one=True)["status"] == "Open"
    login(c, "tech", "techpass12")
    post(c, f"/tickets/{tid}/resolve", {"resolution": "Replaced PSU"}, f"/tickets/{tid}")
    login(c, "staff", "staffpass1")
    post(c, f"/tickets/{tid}/close", {}, f"/tickets/{tid}")
    with m.app.app_context():
        assert m.q("SELECT status FROM tickets WHERE id=?", (tid,), one=True)["status"] == "Closed"
    post(c, f"/tickets/{tid}/comment", {"body": "Thanks!"}, f"/tickets/{tid}")
    assert b"Thanks!" in c.get(f"/tickets/{tid}").data

    # overdue detection + demo data pages
    login(c, "admin", "adminpass1")
    with m.app.app_context():
        m.db().execute("INSERT INTO tickets(title, priority, status, created_at, due_at) VALUES ('old', 'High', 'Open', '2020-01-01 00:00:00', '2020-01-02 00:00:00')")
        m.db().commit()
    assert b"old" in c.get("/tickets?overdue=1").data
    m.seed_demo()  # no-op: users already exist
    for page in ["/", "/tickets", "/tickets?scope=all&mine=1&unassigned=1", "/tickets/new", f"/tickets/{tid}", "/users", "/account"]:
        assert c.get(page).status_code == 200, page
    assert c.get("/tickets/9999").status_code == 404

    # works with no AMS present
    m.AMS_DB = os.path.join(tmp, "missing.db")
    assert c.get("/tickets/new").status_code == 200 and c.get(f"/tickets/{tid}").status_code == 200


if __name__ == "__main__":
    test_flow()
    print("ok")
