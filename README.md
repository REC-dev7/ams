# College Asset Management System (AMS)

Tracks every computer, part, network device and electronic item across campus,
by building → room (lab, classroom, office, network room), with issues and service history.

## Run

```
pip install flask
python app.py            # empty system, first visit asks you to create the admin
python app.py --demo     # adds sample campus data + demo logins, then runs
python app.py --lan      # let other campus PCs open it at http://<this-pc-ip>:5000
python app.py --debug    # auto-reload while editing code (local only, never with --lan)
```

Open http://localhost:5000

## Loading real data

- **CSV import** (Admin → Assets → Import CSV): download the template, fill it in Excel, save as CSV UTF-8, upload.
  Missing buildings/rooms/categories are created. If any row is wrong nothing is saved and the errors are listed.
- **Duplicate** (asset page): copies a PC and, optionally, its parts. Fastest way to add 60 identical lab PCs by hand.
- Export CSV uses the same columns, so exports can be edited and re-imported.

## Demo logins (only created by `--demo`)

| Role       | Username | Password          |
|------------|----------|-------------------|
| Admin      | admin    | demo-admin-2026   |
| Technician | tech     | demo-tech-2026    |
| Staff      | staff    | demo-staff-2026   |

Delete `ams.db` to start fresh. Change or remove demo users before real use.

## Roles

- **Admin**: everything, including buildings/rooms/categories and users
- **Technician**: add/edit assets and parts, log service, resolve issues
- **Staff**: view everything, report issues

## Features

- Dashboard: totals, health, by category / building, open issues, recent activity
- Buildings & rooms: open any lab to see all equipment, with computer parts nested
- Assets: search/filter, CSV export, serial/IP/MAC, warranty, cost, vendor
- Parts: RAM, disk, monitor etc. recorded under their computer, move with it
- Issues: hardware / network / software / power, priority, resolve with fix notes
- Service log: repairs and maintenance with cost and next-due date
- QR labels: print per room, scan to open the asset page
- Stock verification: per-room checklist ("Verify stock"), last-verified date, yearly overdue count
- Stock register: printable room/building-wise list with cost, book value, signature lines
- Depreciation: straight-line book value, useful life set per category (Locations & categories)
- Lending: issue projectors/laptops to staff with a due date, overdue list ("Lent out")
- Full change history per asset

## Test

```
python test_app.py
```

## Later

Helpdesk will raise tickets against assets; the `issues` table is the link point.
