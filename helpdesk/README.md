# REC Helpdesk

Report and fix problems with campus electronics (computers, network, software, power).
Separate system from the AMS: own database, own login, own port. Optional read-only link to the
AMS so a ticket can point at an asset tag and show its name and location.

## Run

```
pip install flask
python app.py            # http://localhost:5002, first visit asks you to create the admin
python app.py --demo     # sample tickets + demo logins
python app.py --lan      # open to other campus PCs
python app.py --debug    # auto-reload (local only)
```

Demo logins (only with `--demo`): `admin` / `demo-admin-2026`, `tech` / `demo-tech-2026`,
`staff` / `demo-staff-2026`. Login page also has one-click buttons.

## AMS link

Tickets can name an AMS asset tag. The helpdesk reads `../ams.db` read-only (nothing is written to it).
Another location: `set AMS_DB=C:\path\to\ams.db` before starting. No AMS found: tickets still work,
the tag is just text.

## Roles

- **Admin**: everything, assign tickets, manage users
- **Technician**: take or get assigned tickets, change priority, resolve, close
- **Staff**: raise tickets, comment, confirm fixes or reopen their own

## Flow

Open -> (assigned) In progress -> Resolved (technician writes the fix) -> Closed (raiser confirms).
"Not fixed, reopen" sends it back to Open with a fresh fix-by time.

## Fix-by times (SLA)

Critical 4 h · High 1 day · Medium 3 days · Low 7 days, counted from when the ticket was raised.
Overdue tickets are flagged on the dashboard. Change in `SLA_HOURS` in `app.py`.

## Test

```
python test_helpdesk.py
```
