"""Smoke test: python test_app.py"""
import io
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, timedelta

import app as m


def csrf(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, page="/"):
    return c.post(path, data={**data, "csrf": csrf(c, page)})


def login(c, user, pw=None):
    c.post("/logout", data={"csrf": csrf(c, "/login")})
    return post(c, "/login", {"username": user, "password": pw or f"demo-{user}-2026"}, "/login")


def row(sql, *args):
    with m.app.app_context():
        return m.q(sql, args, one=True)


def dept(name):
    return str(row("SELECT id FROM departments WHERE name=?", name)["id"])


def asset_id(name):
    return row("SELECT id FROM assets WHERE name=?", name)["id"]


def new_asset(c, name="Test PC", **kw):
    r = post(c, "/assets/new", {"name": name, "managing_dept_id": dept("CSE"), "room_id": "1", **kw}, "/assets/new")
    return int(r.location.rsplit("/", 1)[1]) if r.status_code == 302 else r


def test_flow():
    m.DB = os.path.join(tempfile.mkdtemp(), "t.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    c = m.app.test_client()

    assert c.get("/").location.endswith("/setup")
    assert m.app.config["SESSION_COOKIE_NAME"] == "ams_session"
    post(c, "/setup", {"name": "Boss", "username": "boss", "password": "bosspass12"}, "/setup")
    assert row("SELECT role FROM users WHERE username='boss'")["role"] == "super_admin"
    assert c.get("/").location.startswith("/login")
    r = c.post("/login", data={"username": "boss", "password": "bosspass12"})  # no csrf
    assert r.status_code == 400 and b"form expired" in r.data.lower() and b"REC" in r.data
    assert login(c, "boss", "wrongpass1").status_code == 200
    assert login(c, "boss", "bosspass12").status_code == 302

    m.seed_demo()
    for page in ["/", "/locations", "/assets", "/assets?parts=1", "/issues", "/issues?status=All", "/services", "/labels?room=1", "/settings",
                 "/users", "/rooms/1", "/assets/1", "/assets/new", "/assets/1/edit", "/account", "/loans", "/register"]:
        assert c.get(page).status_code == 200, page
    assert b"System not booting" in c.get("/issues").data
    assert c.get("/assets?export=csv").data.startswith(b"tag,name")

    # permanent tags come from a counter that only goes up; every asset needs a managing department and a place
    assert b"Managing department is required" in post(c, "/assets/new", {"name": "x", "room_id": "1"}, "/assets/new").data
    assert b"Room is required" in post(c, "/assets/new", {"name": "x", "managing_dept_id": dept("CSE")}, "/assets/new").data
    assert b"must be a number" in post(c, "/assets/new", {"name": "x", "managing_dept_id": dept("CSE"), "room_id": "1", "cost": "abc"}, "/assets/new").data
    pc = new_asset(c, "Test PC", cost="1000")
    ram = int(post(c, f"/assets/new?parent={pc}", {"name": "Test RAM"}, "/assets/new").location.rsplit("/", 1)[1])
    t1, t2 = row("SELECT tag FROM assets WHERE id=?", pc)["tag"], row("SELECT tag FROM assets WHERE id=?", ram)["tag"]
    assert t1.startswith("AMS-") and t2 > t1
    assert row("SELECT lifecycle_stage, status, managing_dept_id FROM assets WHERE id=?", pc)["lifecycle_stage"] == "In stock"
    assert row("SELECT managing_dept_id FROM assets WHERE id=?", ram)["managing_dept_id"] == row("SELECT managing_dept_id FROM assets WHERE id=?", pc)["managing_dept_id"]
    assert b"already used" in post(c, "/assets/new", {"name": "dup", "tag": t1, "managing_dept_id": dept("CSE"), "room_id": "1"}, "/assets/new").data
    # unknown location is allowed when flagged
    flagged = new_asset(c, "Found in old list", location_unresolved="1", room_id="")
    assert row("SELECT room_id, location_unresolved FROM assets WHERE id=?", flagged)["location_unresolved"] == 1 and c.get(f"/assets/{flagged}").status_code == 200

    # issue -> condition Faulty; resolve -> Working
    post(c, f"/assets/{pc}/issues", {"title": "Network weak", "type": "Network", "priority": "High", "mark_faulty": "1"})
    a = row("SELECT condition, status FROM assets WHERE id=?", pc)
    assert (a["condition"], a["status"]) == ("Faulty", "Faulty")
    iid = row("SELECT id FROM issues WHERE asset_id=?", pc)["id"]
    post(c, f"/issues/{iid}/resolve", {"resolution": "Replaced cable", "mark_working": "1"})
    post(c, f"/assets/{pc}/services", {"date": "2026-01-01", "type": "Repair", "next_due": "2026-06-01", "condition": "Working"})
    a = row("SELECT condition, status, next_service_due FROM assets WHERE id=?", pc)
    assert (a["condition"], a["status"], a["next_service_due"]) == ("Working", "In store", "2026-06-01")

    # duplicate with parts: new PC gets its own copy of the RAM and its own tag
    r = post(c, f"/assets/new?copy={pc}", {"name": "Test PC 2", "managing_dept_id": dept("CSE"), "room_id": "1", "copy_parts_from": str(pc)}, "/assets/new")
    pc2 = int(r.location.rsplit("/", 1)[1])
    assert [p["name"] for p in row_all("SELECT name FROM assets WHERE parent_id=?", pc2)] == ["Test RAM"]
    assert len({t["tag"] for t in row_all("SELECT tag FROM assets")}) == row("SELECT COUNT(*) n FROM assets")["n"]

    # CSV import: template imports cleanly; a bad file saves nothing
    tpl = c.get("/assets/import?template=1").data
    r = c.post("/assets/import", data={"csrf": csrf(c, "/assets/import"), "file": (io.BytesIO(tpl), "t.csv")})
    assert r.status_code == 302, r.data
    part = row("SELECT p.tag FROM assets a JOIN assets p ON p.id=a.parent_id WHERE a.name='8 GB RAM' AND p.tag='CSE-L3-001'")
    assert part and row("SELECT 1 FROM rooms WHERE name='Computer Lab 3'")
    assert row("SELECT lifecycle_stage, managing_dept_id FROM assets WHERE tag='CSE-L3-001'")["managing_dept_id"] == int(dept("CSE"))
    before = row("SELECT COUNT(*) n FROM assets")["n"]
    bad = b"name,building,room,cost\nGood PC,CSE Block,Lab X,100\nBad PC,CSE Block,Lab X,abc\n"
    r = c.post("/assets/import", data={"csrf": csrf(c, "/assets/import"), "file": (io.BytesIO(bad), "b.csv")})
    assert b"Row 3" in r.data and row("SELECT COUNT(*) n FROM assets")["n"] == before

    # depreciation
    half = (date.today() - timedelta(days=round(365.25 * 2.5))).isoformat()
    assert abs(m.book_value(1000, half, 5) - 500) < 1 and m.book_value(1000, None, 5) == 1000 and m.book_value(1000, "2000-01-01", 5) == 0

    # stock check sets the verification result
    post(c, "/rooms/1/audit", {"found": str(pc)})
    assert row("SELECT verification, last_audit FROM assets WHERE id=?", pc)["verification"] == "Verified"
    assert row("SELECT verification FROM assets WHERE id=?", ram)["verification"] == "Not found"
    assert row("SELECT 1 FROM activity WHERE asset_id=? AND text='Not found in stock check' AND event_type='Not found'", ram)

    # custody: issue to a department, a second person with a stale page is refused, return to stock
    page = c.get(f"/assets/{pc}").get_data(as_text=True)
    assert 'name="expected" value="0"' in page
    post(c, f"/assets/{pc}/custody", {"to": "Department", "department_id": dept("ECE"), "reason": "New lab", "expected": "0", "event_date": date.today().isoformat()}, f"/assets/{pc}")
    a = row("SELECT lifecycle_stage, using_dept_id, status FROM assets WHERE id=?", pc)
    assert (a["lifecycle_stage"], a["using_dept_id"], a["status"]) == ("Deployed", int(dept("ECE")), "Working")
    assert row("SELECT COUNT(*) n FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["n"] == 1
    r = post(c, f"/assets/{pc}/custody", {"to": "Department", "department_id": dept("Mechanical"), "reason": "Mine", "expected": "0"}, f"/assets/{pc}")
    assert b"Someone else" in c.get(r.location, follow_redirects=True).data and row("SELECT using_dept_id FROM assets WHERE id=?", pc)["using_dept_id"] == int(dept("ECE"))
    assert b"Choose the department" in c.get(post(c, f"/assets/{pc}/custody", {"to": "Department", "reason": "x", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"])}, f"/assets/{pc}").location, follow_redirects=True).data
    future = (date.today() + timedelta(days=3)).isoformat()
    assert b"in the future" in c.get(post(c, f"/assets/{pc}/custody", {"to": "Stockroom", "reason": "x", "event_date": future, "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"])}, f"/assets/{pc}").location, follow_redirects=True).data
    cur = row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"]
    post(c, f"/assets/{pc}/custody", {"to": "Stockroom", "reason": "Lab closed", "expected": str(cur), "event_date": (date.today() - timedelta(days=2)).isoformat()}, f"/assets/{pc}")
    assert row("SELECT lifecycle_stage, using_dept_id FROM assets WHERE id=?", pc)["lifecycle_stage"] == "In stock"
    ev = row("SELECT event_type, event_date, at FROM activity WHERE asset_id=? AND event_type='Return'", pc)
    assert ev["event_date"] == (date.today() - timedelta(days=2)).isoformat() and ev["event_date"] != ev["at"][:10]  # real date vs entry date

    # lend and return through the quick form
    post(c, f"/assets/{pc2}/lend", {"assigned_to": "Dr. X", "assigned_due": "2020-01-01"})
    assert b"Dr. X" in c.get("/loans").data and b"overdue" in c.get("/loans").data
    post(c, f"/assets/{pc2}/lend", {"action": "return"})
    assert b"Dr. X" not in c.get("/loans").data and row("SELECT lifecycle_stage FROM assets WHERE id=?", pc2)["lifecycle_stage"] == "In stock"

    # retirement: proposal, a different approver, disposal evidence, data erased for computers
    victim = asset_id("L2-PC-06")
    login(c, "lab")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement proposed", "reason": "Beyond repair"}, f"/assets/{victim}")
    assert row("SELECT lifecycle_stage FROM assets WHERE id=?", victim)["lifecycle_stage"] == "Retirement proposed"
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement approved", "reason": "ok"}, f"/assets/{victim}")
    assert b"Only an HOD or admin" in c.get(r.location, follow_redirects=True).data
    assert c.get(f"/assets/{asset_id('Kiosk PC')}").status_code == 404  # Library's asset: not in the CSE lab assistant's department
    login(c, "hod")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement approved", "reason": "Approved by committee"}, f"/assets/{victim}")
    assert row("SELECT lifecycle_stage FROM assets WHERE id=?", victim)["lifecycle_stage"] == "Retirement approved"
    login(c, "admin")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Awaiting disposal", "reason": "Listed for e-waste"}, f"/assets/{victim}")
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected"}, f"/assets/{victim}")
    assert b"disposal evidence" in c.get(r.location, follow_redirects=True).data
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected", "evidence": "EW-1042"}, f"/assets/{victim}")
    assert b"data was erased" in c.get(r.location, follow_redirects=True).data
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected", "evidence": "EW-1042", "data_erased": "1"}, f"/assets/{victim}")
    a = row("SELECT lifecycle_stage, status FROM assets WHERE id=?", victim)
    assert (a["lifecycle_stage"], a["status"]) == ("Disposed", "Scrapped")
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "In stock", "reason": "undo"}, f"/assets/{victim}")
    assert b"final" in c.get(r.location, follow_redirects=True).data or b"it can only go to" in c.get(r.location, follow_redirects=True).data
    # the same person can't propose and approve
    other = asset_id("PA System")
    post(c, f"/assets/{other}/lifecycle", {"to_stage": "Retirement proposed", "reason": "Old"}, f"/assets/{other}")
    r = post(c, f"/assets/{other}/lifecycle", {"to_stage": "Retirement approved", "reason": "self approve"}, f"/assets/{other}")
    assert b"different person" in c.get(r.location, follow_redirects=True).data

    # tags: only an admin, with a reason; the old tag stays searchable and is never handed out again
    oldtag = row("SELECT tag FROM assets WHERE id=?", pc2)["tag"]
    login(c, "tech")
    form = {"name": "Test PC 2", "managing_dept_id": dept("CSE"), "room_id": "1", "tag": "CSE-NEW-1"}
    assert b"Only an admin can correct a tag" in post(c, f"/assets/{pc2}/edit", form, f"/assets/{pc2}/edit").data
    login(c, "admin")
    assert b"Say why the tag" in post(c, f"/assets/{pc2}/edit", form, f"/assets/{pc2}/edit").data
    post(c, f"/assets/{pc2}/edit", {**form, "tag_reason": "Label was printed wrong"}, f"/assets/{pc2}/edit")
    assert row("SELECT tag FROM assets WHERE id=?", pc2)["tag"] == "CSE-NEW-1"
    assert str(pc2).encode() and f"/assets/{pc2}".encode() in c.get(f"/assets?q={oldtag}").data
    with m.app.app_context():
        m.db().execute("UPDATE counters SET value=? WHERE name='tag'", (int(oldtag.split("-")[1]) - 1,))
        m.db().commit()
        assert m.next_tag() != oldtag  # the counter was wound back on purpose; the old tag is still refused

    # old codes and other systems' IDs
    post(c, f"/assets/{pc2}/identifiers", {"kind": "legacy_code", "value": "REC/MECH/LAB1/P07"}, f"/assets/{pc2}")
    assert f"/assets/{pc2}".encode() in c.get("/assets?q=P07").data
    assert b"already belongs" in c.get(post(c, f"/assets/{pc}/identifiers", {"kind": "legacy_code", "value": "REC/MECH/LAB1/P07"}, f"/assets/{pc}").location, follow_redirects=True).data
    assert b"which system" in c.get(post(c, f"/assets/{pc}/identifiers", {"kind": "external", "value": "X1"}, f"/assets/{pc}").location, follow_redirects=True).data

    # archive instead of delete: needs a reason, keeps the record and the tag
    assert b"Archive or move its parts" in c.get(post(c, f"/assets/{pc2}/delete", {"reason": "dup"}, f"/assets/{pc2}").location, follow_redirects=True).data
    child = row("SELECT id FROM assets WHERE parent_id=?", pc2)["id"]
    assert b"Say why" in c.get(post(c, f"/assets/{child}/delete", {}, f"/assets/{child}").location, follow_redirects=True).data
    post(c, f"/assets/{child}/delete", {"reason": "entered by mistake"}, f"/assets/{child}")
    post(c, f"/assets/{pc2}/delete", {"reason": "entered twice"}, f"/assets/{pc2}")
    assert row("SELECT archived_at, status FROM assets WHERE id=?", pc2)["status"] == "Scrapped"
    assert f"/assets/{pc2}".encode() not in c.get("/assets").data and f"/assets/{pc2}".encode() in c.get("/assets?archived=1").data
    assert row("SELECT COUNT(*) n FROM activity WHERE asset_id=?", pc2)["n"] >= 3

    # department scope: enforced on the server
    ece = asset_id("Digital Storage Oscilloscope")
    cse = asset_id("L1-PC-01")
    for who in ("staff", "hod", "lab"):
        login(c, who)
        assert c.get(f"/assets/{ece}").status_code == 404, who
        assert c.get(f"/assets/{cse}").status_code == 200, who
        assert f"/assets/{ece}".encode() not in c.get("/assets?q=Oscilloscope").data
        assert c.get(f"/labels?asset={ece}").data.count(b"Oscilloscope") == 0
    login(c, "staff")
    assert post(c, f"/assets/{cse}/issues", {"title": "Slow"}, f"/assets/{cse}").status_code == 302  # a custodian can report
    assert post(c, f"/assets/{cse}/services", {"type": "Repair"}, f"/assets/{cse}").status_code == 403
    assert post(c, f"/assets/{cse}/custody", {"to": "Stockroom", "reason": "x", "expected": "0"}, f"/assets/{cse}").status_code == 403
    login(c, "lab")
    assert post(c, f"/assets/{cse}/custody", {"to": "Stockroom", "reason": "Lab closed", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", cse)["id"])}, f"/assets/{cse}").status_code == 302
    assert c.get("/assets/new").status_code == 403 and c.get("/settings").status_code == 403
    login(c, "auditor")
    assert c.get(f"/assets/{ece}").status_code == 200 and post(c, f"/assets/{ece}/issues", {"title": "x"}, f"/assets/{ece}").status_code in (302, 403)
    assert c.get("/assets/new").status_code == 403
    login(c, "purchase")
    assert c.get(f"/assets/{ece}").status_code == 200 and c.get(f"/assets/{ece}/edit").status_code == 403

    # roles: only a super admin hands out admin roles; extra roles add abilities
    login(c, "admin")
    r = post(c, "/users/add", {"name": "New", "username": "newadmin", "role": "admin", "password": "newpass123"}, "/users")
    assert row("SELECT 1 FROM users WHERE username='newadmin'") is None
    post(c, "/users/add", {"name": "Dual", "username": "dual", "role": "custodian", "extra_roles": ["lab_assistant"], "department_id": dept("ECE"), "password": "dualpass123"}, "/users")
    assert row("SELECT extra_roles FROM users WHERE username='dual'")["extra_roles"] == "lab_assistant"
    login(c, "dual", "dualpass123")
    assert c.get(f"/assets/{ece}").status_code == 200 and c.get(f"/assets/{cse}").status_code == 404
    assert post(c, f"/assets/{ece}/custody", {"to": "Person", "person": "Dr. Z", "reason": "loan", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", ece)["id"])}, f"/assets/{ece}").status_code == 302
    assert row("SELECT custodian FROM assets WHERE id=?", ece)["custodian"] == "Dr. Z"
    login(c, "super")
    post(c, "/users/add", {"name": "New", "username": "newadmin", "role": "admin", "password": "newpass123"}, "/users")
    assert row("SELECT 1 FROM users WHERE username='newadmin'")


def row_all(sql, *args):
    with m.app.app_context():
        return m.q(sql, args)


def test_tags_are_unique_under_concurrency():
    m.DB = os.path.join(tempfile.mkdtemp(), "c.db")
    m.init_db()
    tags, lock = [], threading.Lock()

    def worker():
        for _ in range(15):
            with m.app.test_request_context():
                t = m.next_tag()
                m.db().execute("INSERT INTO assets(name, tag, lifecycle_stage) VALUES ('x', ?, 'In stock')", (t,))
                m.db().commit()
                with lock:
                    tags.append(t)

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(tags) == 45 and len(set(tags)) == 45


def test_old_database_upgrades():
    """A database from the previous release (3 roles, one status, lending columns) is upgraded in place."""
    old = os.path.join(tempfile.mkdtemp(), "old.db")
    with sqlite3.connect(old) as c:
        c.executescript("""
          CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','technician','staff')));
          CREATE TABLE buildings (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, code TEXT);
          CREATE TABLE rooms (id INTEGER PRIMARY KEY, building_id INTEGER NOT NULL REFERENCES buildings(id), name TEXT NOT NULL, type TEXT, floor TEXT, UNIQUE (building_id, name));
          CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, life_years INTEGER DEFAULT 5);
          CREATE TABLE assets (id INTEGER PRIMARY KEY, tag TEXT UNIQUE, name TEXT NOT NULL, category_id INTEGER, room_id INTEGER, parent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Working', brand TEXT, model TEXT, serial_no TEXT, ip_address TEXT, mac_address TEXT, purchase_date TEXT,
            warranty_until TEXT, cost REAL, vendor TEXT, notes TEXT, next_service_due TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_audit TEXT, assigned_to TEXT, assigned_due TEXT);
          CREATE TABLE activity (id INTEGER PRIMARY KEY, asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE, user_id INTEGER, text TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
          INSERT INTO users VALUES (1, 'admin', 'Admin', 'x', 'admin'), (2, 'tech', 'Tech', 'x', 'technician'), (3, 'staff', 'Staff', 'x', 'staff');
          INSERT INTO assets(id, tag, name, status, last_audit) VALUES (1, 'AMS-00001', 'Working PC', 'Working', '2026-01-01'),
            (2, 'AMS-00002', 'Faulty PC', 'Faulty', NULL), (3, 'AMS-00003', 'In repair', 'Under service', NULL),
            (4, 'AMS-00004', 'Spare', 'In store', NULL), (5, 'AMS-00005', 'Scrap', 'Scrapped', NULL);
          UPDATE assets SET assigned_to='Dr. Meena', assigned_due='2026-02-01' WHERE id=1;""")
    m.DB = old
    m.init_db()
    m.init_db()  # idempotent
    with sqlite3.connect(old) as c:
        c.row_factory = sqlite3.Row
        roles = {r["username"]: r["role"] for r in c.execute("SELECT username, role FROM users")}
        assert roles == {"admin": "super_admin", "tech": "technician", "staff": "custodian"}
        st = {r["id"]: (r["lifecycle_stage"], r["condition"], r["status"]) for r in c.execute("SELECT * FROM assets")}
        assert st[1] == ("Deployed", "Working", "Working") and st[2] == ("Deployed", "Faulty", "Faulty")
        assert st[3][0] == "Under maintenance" and st[4][0] == "In stock" and st[5][0] == "Disposed"
        assert c.execute("SELECT verification FROM assets WHERE id=1").fetchone()[0] == "Verified"
        cu = c.execute("SELECT * FROM custody WHERE asset_id=1 AND ended_at IS NULL").fetchone()
        assert cu["person"] == "Dr. Meena" and cu["due_date"] == "2026-02-01"
        assert c.execute("SELECT assigned_to FROM assets WHERE id=1").fetchone()[0] is None
        assert c.execute("SELECT value FROM counters WHERE name='tag'").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM departments").fetchone()[0] > 10
        c.execute("INSERT INTO users(username, name, password_hash, role) VALUES ('h', 'H', 'x', 'hod')")  # the new roles are allowed now


if __name__ == "__main__":
    test_flow()
    test_tags_are_unique_under_concurrency()
    test_old_database_upgrades()
    print("ok")
