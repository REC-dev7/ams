# Running both systems on the college server

For the IT team. Assumes one Ubuntu Server LTS machine (virtual is fine) inside the campus network.
Both apps share the machine and the repository; each has its own database, uploads, login and address.

## 1. Machine

| | Minimum (trial) | Recommended |
|---|---|---|
| CPU | 2 cores | 4 cores, 2.5 GHz or faster |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB SSD | 100 GB SSD (photos and documents grow) |
| Backups | another disk or machine | another machine, nightly, kept 30 days |

Measured (`loadtest.py`, a laptop, the plain development server, each user opening a page every second):
150 signed-in users at once loaded about 140 pages a second with no errors; a typical page took under 10 ms
and the slowest 5% under 100 ms. The production setup below is faster than that. Re-run the test on the real
server before go-live (section 7).

## 2. Install

```bash
sudo apt update && sudo apt install -y python3-venv nginx git clamav clamav-daemon
sudo useradd --system --create-home --home-dir /srv/rec rec
sudo -u rec git clone https://github.com/REC-dev7/ams.git /srv/rec/app
cd /srv/rec/app && sudo -u rec python3 -m venv venv && sudo -u rec venv/bin/pip install -r requirements.txt
```

Create the first accounts: start each app once (step 3), open it, and the setup page asks for the first
super admin (AMS) or admin (Helpdesk). **Do not set `DEMO`** on this server.

## 3. Services (systemd)

`/etc/systemd/system/rec-ams.service`:

```ini
[Unit]
Description=REC AMS
After=network.target

[Service]
User=rec
WorkingDirectory=/srv/rec/app
EnvironmentFile=/srv/rec/ams.env
ExecStart=/srv/rec/app/venv/bin/gunicorn --preload --workers 3 --threads 4 --bind 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/rec-helpdesk.service`: the same, with `Description=REC Helpdesk`,
`EnvironmentFile=/srv/rec/helpdesk.env` and
`ExecStart=/srv/rec/app/venv/bin/gunicorn --preload --chdir helpdesk --workers 3 --threads 4 --bind 127.0.0.1:8001 app:app`.

Workers: about 2 × cores + 1, at most 5. Both apps use SQLite, which allows one write at a time; for a staff
tool that is plenty (see the load test). Several workers are safe: the session key, planned-maintenance jobs and
the daily reminder are each claimed once.

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now rec-ams rec-helpdesk
```

## 4. Settings (the two `.env` files, readable only by the `rec` user)

`/srv/rec/ams.env`

```
AMS_API_TOKEN=<long random string: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
OTHER_APP_URL=https://helpdesk.<college domain>/
UPLOAD_SCAN=1
# AMS_API_WRITE=1   only after the college agrees the Helpdesk may mark assets faulty/working
```

`/srv/rec/helpdesk.env`

```
AMS_URL=https://ams.<college domain>
AMS_API_TOKEN=<the same string>
OTHER_APP_URL=https://ams.<college domain>/
HD_BASE_URL=https://helpdesk.<college domain>
UPLOAD_SCAN=1
HD_CHAT_WEBHOOK=<Google Chat incoming webhook>          # optional
HD_SMTP_HOST=smtp.gmail.com                             # optional, with the four below
HD_SMTP_PORT=587
HD_SMTP_USER=helpdesk@<college domain>
HD_SMTP_PASS=<app password>
HD_SMTP_FROM=REC Helpdesk <helpdesk@<college domain>>
# HD_AMS_WRITEBACK=1   together with AMS_API_WRITE=1 on the AMS
```

`chmod 600 /srv/rec/*.env`. After a change: `sudo systemctl restart rec-ams rec-helpdesk`.

`UPLOAD_SCAN=1` checks every photo, document and import with ClamAV (`clamscan`) and refuses the file if it is
infected or the scanner cannot run. Keep signatures fresh: `sudo systemctl enable --now clamav-freshclam`.

## 5. Web server and HTTPS (nginx)

`/etc/nginx/sites-available/rec`:

```nginx
server {
    server_name ams.<college domain>;
    client_max_body_size 12m;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
}
server {
    server_name helpdesk.<college domain>;
    client_max_body_size 12m;
    location / { proxy_pass http://127.0.0.1:8001; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
}
```

Enable it, then add HTTPS with the college certificate, or `sudo certbot --nginx` if the names are public.
Set `RENDER=1` in both `.env` files once HTTPS works: it marks the login cookie secure (the name is historical).
Keep the server reachable only from the campus network unless staff need it from home.

## 6. Backups (nightly)

`crontab -e` as the `rec` user:

```
30 1 * * * cd /srv/rec/app && venv/bin/python backup.py --out /backup/rec && venv/bin/python backup.py --verify "$(ls -t /backup/rec/ams-*.zip | head -1)"
45 1 * * * cd /srv/rec/app && venv/bin/python backup.py --db helpdesk/helpdesk.db --uploads helpdesk/uploads --name helpdesk --out /backup/rec
0 3 * * * find /backup/rec -name '*.zip' -mtime +30 -delete
```

`/backup` should be another disk or a network share. Once a month restore one into a spare folder
(`backup.py --restore FILE --to /tmp/check`) and open it: a backup nobody has restored is only a hope.

## 7. Before go-live

1. If either app was ever started with demo data: `venv/bin/python app.py --remove-demo` and
   `cd helpdesk && ../venv/bin/python app.py --remove-demo`. It refuses until a real admin account exists.
2. Load test a copy (not the live one during work hours), with a test account:
   `venv/bin/python loadtest.py https://ams.<domain> --users 150 --seconds 60 --login testadmin:<password>`.
   Pass if there are no errors and the slowest 5% stay under 1 second.
3. Helpdesk, *Places & departments*: check the fix-by hours, the quiet-ticket limit and auto-assign with the IT
   team; add the real blocks, floors and rooms.
4. Helpdesk, *Notifications*: press *Send a test*.
5. Import the real allotment report into the AMS through the wizard; check the preview before approving.

## 8. Rolling out

1. Week 1: IT team only (they raise their own walk-up jobs with *Log completed work*).
2. Weeks 2-3: two or three departments.
3. Then everyone. Keep the old Excel registers until the stock check matches.

## 9. Updating and going back

```bash
cd /srv/rec/app && sudo -u rec git pull && sudo -u rec venv/bin/pip install -r requirements.txt
sudo systemctl restart rec-ams rec-helpdesk
```

Both apps upgrade their own databases at start-up. Take a backup first. To go back:
`git checkout <previous commit>`, restore the backup taken before the update, restart.

## 10. Watching it

- Logs: `journalctl -u rec-ams -u rec-helpdesk --since today`.
- An outside check every few minutes on `/healthz` of each app (any uptime monitor) alerting the IT team: it answers `ok` when the app and its database respond, and needs no sign-in.
- Five wrong passwords in 15 minutes lock that username for 15 minutes (an admin can reset the password meanwhile).

## Not covered yet

- **PostgreSQL.** Both apps use SQLite, which the load test shows is enough for staff use on one server.
  Moving to PostgreSQL is worth it only if many more users or several servers are needed; it is a code change.
- **College single sign-on** (Google or the directory), ERP and Digii links: waiting on the college's decision.
