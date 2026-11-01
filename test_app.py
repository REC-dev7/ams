"""Smoke test: python test_app.py"""
import os
import re
import tempfile

import app as m


def csrf(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, page="/"):
    return c.post(path, data={**data, "csrf": csrf(c, page)})


def login(c, user, pw):
    c.post("/logout", data={"csrf": csrf(c, "/login")})
    return post(c, "/login", {"username": user, "password": pw}, "/login")


def test_flow():
    m.DB = os.path.join(tempfile.mkdtemp(), "t.db")
    m.init_db()
    c = m.app.test_client()

    assert c.get("/").location.endswith("/setup")
    post(c, "/setup", {"name": "Admin", "username": "admin", "password": "adminpass1"}, "/setup")
    assert c.get("/").location.startswith("/login")
    assert c.post("/login", data={"username": "admin", "password": "adminpass1"}).status_code == 400  # no csrf
    assert login(c, "admin", "wrongpass").status_code == 200
    assert login(c, "admin", "adminpass1").status_code == 302

    m.seed_demo()
    for page in ["/", "/locations", "/assets", "/assets?parts=1", "/issues", "/issues?status=All", "/services",
                 "/labels?room=1", "/settings", "/users", "/rooms/1", "/assets/1", "/assets/new", "/assets/1/edit", "/account"]:
        r = c.get(page)
        assert r.status_code == 200, (page, r.status_code)
    assert b"System not booting" in c.get("/issues").data
    assert c.get("/assets?export=csv").data.startswith(b"tag,name")

    # add asset, add a part, move parent -> part follows
    r = post(c, "/assets/new", {"name": "Test PC", "room_id": "1", "status": "Working", "cost": "1000"})
    pc = int(r.location.rsplit("/", 1)[1])
    r = post(c, f"/assets/new?parent={pc}", {"name": "Test RAM", "status": "Working"})
    ram = int(r.location.rsplit("/", 1)[1])
    post(c, f"/assets/{pc}/edit", {"name": "Test PC", "room_id": "2", "status": "Working"})
    with m.app.app_context():
        assert m.q("SELECT room_id FROM assets WHERE id=?", (ram,), one=True)["room_id"] == 2
    assert b"Room: Computer Lab 1" in c.get(f"/assets/{pc}").data
    assert b"Name is required" in post(c, "/assets/new", {"name": "", "room_id": "1"}).data
    assert b"must be a number" in post(c, "/assets/new", {"name": "x", "room_id": "1", "cost": "abc"}).data

    # issue -> faulty -> resolve -> working; service sets next due
    post(c, f"/assets/{pc}/issues", {"title": "Network weak", "type": "Network", "priority": "High", "mark_faulty": "1"})
    with m.app.app_context():
        assert m.q("SELECT status FROM assets WHERE id=?", (pc,), one=True)["status"] == "Faulty"
        iid = m.q("SELECT id FROM issues WHERE asset_id=?", (pc,), one=True)["id"]
    post(c, f"/issues/{iid}/resolve", {"resolution": "Replaced cable", "mark_working": "1"})
    post(c, f"/assets/{pc}/services", {"date": "2026-01-01", "type": "Repair", "next_due": "2026-06-01"})
    with m.app.app_context():
        a = m.q("SELECT status, next_service_due FROM assets WHERE id=?", (pc,), one=True)
        assert (a["status"], a["next_service_due"]) == ("Working", "2026-06-01")

    # duplicate with parts: new PC gets its own copy of the RAM
    r = post(c, f"/assets/new?copy={pc}", {"name": "Test PC 2", "room_id": "1", "status": "Working", "copy_parts_from": str(pc)})
    pc2 = int(r.location.rsplit("/", 1)[1])
    with m.app.app_context():
        assert [p["name"] for p in m.q("SELECT name FROM assets WHERE parent_id=?", (pc2,))] == ["Test RAM"]

    # CSV import: template imports cleanly (PC + part); a bad file saves nothing
    import io
    tpl = c.get("/assets/import?template=1").data
    r = c.post("/assets/import", data={"csrf": csrf(c, "/assets/import"), "file": (io.BytesIO(tpl), "t.csv")})
    assert r.status_code == 302, r.data
    with m.app.app_context():
        part = m.q("SELECT p.tag FROM assets a JOIN assets p ON p.id=a.parent_id WHERE a.name='8 GB RAM' AND p.tag='CSE-L3-001'", one=True)
        assert part and m.q("SELECT 1 FROM rooms WHERE name='Computer Lab 3'", one=True)
        before = m.q("SELECT COUNT(*) n FROM assets", one=True)["n"]
    bad = b"name,building,room,cost\nGood PC,CSE Block,Lab X,100\nBad PC,CSE Block,Lab X,abc\n"
    r = c.post("/assets/import", data={"csrf": csrf(c, "/assets/import"), "file": (io.BytesIO(bad), "b.csv")})
    assert b"Row 3" in r.data
    with m.app.app_context():
        assert m.q("SELECT COUNT(*) n FROM assets", one=True)["n"] == before
    assert c.get("/assets?export=csv").data.startswith(b"tag,name,category")

    # depreciation: half-way through life = half value; no date = full cost
    from datetime import date, timedelta
    half = (date.today() - timedelta(days=round(365.25 * 2.5))).isoformat()
    assert abs(m.book_value(1000, half, 5) - 500) < 1 and m.book_value(1000, None, 5) == 1000
    assert m.book_value(1000, "2000-01-01", 5) == 0

    # stock check: ticked item verified, unticked logged as not found
    for page in ["/register", "/register?room=1", "/loans", "/rooms/1?audit=1"]:
        assert c.get(page).status_code == 200, page
    post(c, "/rooms/2/audit", {"found": str(pc)})
    with m.app.app_context():
        assert m.q("SELECT last_audit FROM assets WHERE id=?", (pc,), one=True)["last_audit"] == date.today().isoformat()
        assert m.q("SELECT 1 FROM activity WHERE asset_id=? AND text='Not found in stock check'", (ram,), one=True)

    # lend and return
    post(c, f"/assets/{pc2}/lend", {"assigned_to": "Dr. X", "assigned_due": "2020-01-01"})
    assert b"Dr. X" in c.get("/loans").data and b"overdue" in c.get("/loans").data
    post(c, f"/assets/{pc2}/lend", {"action": "return"})
    assert b"Dr. X" not in c.get("/loans").data

    # can't delete parent with parts; staff role limits
    assert post(c, f"/assets/{pc}/delete", {}).location.endswith(f"/assets/{pc}")
    post(c, "/users/add", {"name": "Staff", "username": "staff", "role": "staff", "password": "staffpass1"}, "/users")
    login(c, "staff", "staffpass1")
    assert c.get("/assets/new").status_code == 403
    assert c.get("/settings").status_code == 403
    assert post(c, f"/assets/{pc}/issues", {"title": "Slow"}).status_code == 302  # staff can report
    assert post(c, f"/assets/{pc}/services", {"type": "Repair"}).status_code == 403


if __name__ == "__main__":
    test_flow()
    print("ok")
