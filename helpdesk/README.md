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

Demo logins (only with `--demo`): `<username>` / `demo-<username>-2026` for `admin`, `tech` (IT Network),
`tech2` (IT Systems), `staff` (CSE), `staff2` (Mechanical), `coord`, `hod`, `auditor`. The login page has one-click buttons.
An older database is upgraded in place when the app starts.

## AMS link

Two ways, depending on where the apps run:

- **Same computer:** the Helpdesk reads the AMS database file read-only (below).
- **Separate servers (Render):** set `AMS_URL` (the AMS address) and `AMS_API_TOKEN` (the same secret as on the AMS) and it asks the AMS's read-only API instead. See `../docs/API.md`.


Tickets can name an AMS asset tag. The helpdesk reads `../ams.db` read-only (nothing is written to it).
Another location: `set AMS_DB=C:\path\to\ams.db` before starting. No AMS found: tickets still work,
the tag is just text.

## Roles

- **Admin**: everything, manage users, places and departments, export CSV
- **IT technician**: take or get assigned tickets, route to another team, ask the requester, resolve, link outages, log completed work, internal notes. Can have a team.
- **Staff / faculty**: raise tickets and follow their own; confirm a fix or reopen
- **Dept coordinator / HOD**: raise for the department, see and confirm the department's tickets (set their department)
- **Auditor**: read only, can export CSV

Permissions are checked on the server: someone who guesses a ticket or photo address still gets "Not found".

## Flow

Open -> In progress -> (Waiting on requester) -> Resolved -> Closed. Every change is saved in the ticket's status history.

- **Waiting on requester**: IT asks a question; the fix-by clock pauses and restarts when the requester replies.
- **Resolved**: the technician writes the fix (and parts used); the requester or their department confirms (Closed) or reopens.
- **Planned**: a *New requirement* (new equipment) goes to the procurement list with a note instead of staying open. It has no deadline.
- **Request types**: Fault, Service request, New requirement, Question.
- **Teams**: the problem category decides the team (IT Network, IT Systems, Printers & AV, Electrical (EB), AC, Civil). Re-route a ticket posted in the wrong place.
- **Urgent reason**: exam in progress, event running, class in session or management request raises the priority.
- **Places**: Block, Floor, Room pickers fed from *Places & departments* (paste many at once). Anything else can still be typed.
- **Repeat problems**: a ticket shows "3 in 30 days" when the same kind of problem keeps coming from one place.
- **Outages**: link tickets reporting the same outage; resolve the main one and the rest follow.
- **Quiet tickets**: open tickets with no update for 24 hours are flagged and listed.
- **Log completed work**: technicians record walk-up or self-found fixes so they still count.
- **Dashboard** (IT and auditor): last 30 days - first reply time, time to fix, % fixed in time, repeat places, open by team, active outages.

## Notifications, analytics, planned maintenance

All in `hd_extra.py`.

- **Notifications** (admin: *Notifications*). A new ticket goes to a Google Chat space (critical ones flagged) and by email to the technicians of the ticket's team. Assigning emails the technician; resolving emails the requester to confirm. Each channel is **off until its settings exist** on the server:

  | Setting | Meaning |
  |---|---|
  | `HD_CHAT_WEBHOOK` | Google Chat incoming-webhook address (`https://...`) |
  | `HD_SMTP_HOST`, `HD_SMTP_PORT`, `HD_SMTP_USER`, `HD_SMTP_PASS`, `HD_SMTP_FROM` | outgoing mail (port 587 STARTTLS or 465 SSL) |
  | `HD_BASE_URL` | public address, for the links in messages (optional) |

  Add each person's email on the Users page. Sends run in the background; a failing mail server never blocks a ticket, and the result of the last 50 sends is listed on the Notifications page.
- **Analytics** (everyone except staff; HODs and coordinators see their department): tickets by month, kind, team, department and priority, % fixed on time, median first reply and time to fix, places and equipment with repeat problems, and per-technician figures for admin and auditor. Period: 30 days, 90 days, a year or all time.
- **Planned maintenance** (IT): a repeating job (weekly to yearly) raises a ticket by itself when its date comes, then moves the date on. A long gap raises one ticket, not a pile. Pause, delete, or *Raise now* from the list. The check runs when someone loads a page (at most every 5 minutes), so there is no extra scheduler to keep alive.
- **Update the AMS** (off by default): with `HD_AMS_WRITEBACK=1` here and `AMS_API_WRITE=1` on the AMS (plus the shared `AMS_API_TOKEN`), a fault ticket that names an asset marks it Faulty, and fixing the last open ticket for it marks it Working. Both are written to the asset's history. Details in `../docs/API.md`.

## Fix-by times (SLA)

Critical 4 h · High 1 day · Medium 3 days · Low 7 days, counted from when the ticket was raised.
Overdue tickets are flagged on the dashboard. Change in `SLA_HOURS` in `app.py`.

## Test

```
python test_helpdesk.py
```
