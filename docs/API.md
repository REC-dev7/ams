# Read-only API

Lets another system (today the Helpdesk) look up an asset without a person signing in. It can only **read**, and it gives identity, place and holder only: **no costs, vendors, purchase numbers, documents, licence keys or user accounts**.

## Switching it on

The API is **off** until the AMS has a secret token:

| Where | Setting | Value |
|---|---|---|
| AMS server | `AMS_API_TOKEN` | a long random string, for example the output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| Helpdesk server | `AMS_API_TOKEN` | the same string |
| Helpdesk server | `AMS_URL` | the AMS address, for example `https://rec-ams.onrender.com` |

On Render: open each service, **Environment**, add the variables, save (it restarts). Keep the token private; anyone holding it can read the asset list. To rotate it, change it in both places.

With no token the AMS answers 404 for every API address. With a wrong or missing token it answers 401.

## Requests

Every request sends `Authorization: Bearer <token>`.

### One asset

`GET /api/v1/assets/<tag or old asset code>`

```
curl -H "Authorization: Bearer $TOKEN" https://ams.example/api/v1/assets/AMS-00013
```

```json
{
  "tag": "AMS-00013", "name": "L1-PC-04", "category": "Desktop computer", "brand": "Dell", "model": "OptiPlex 7090", "serial_no": "DLL10004",
  "lifecycle_stage": "Deployed", "condition": "Faulty", "status": "Faulty",
  "building": "CSE Block", "room": "Computer Lab 1", "location_known": true,
  "managing_department": "CSE", "using_department": "CSE", "holder": "CSE",
  "old_asset_code": "REC/CSE/L1/PC-04", "archived": false
}
```

`404` with `{"error": ...}` when no asset has that code.

### Search

`GET /api/v1/assets?q=<text>&limit=<1-1000, default 100>`

Matches tag, name or old code. Parts, archived and scrapped assets are left out. Returns `{"count": n, "items": [ ...same fields... ]}`.

## How the Helpdesk uses it

- A ticket form's asset picker is filled from the search (cached for 5 minutes).
- A ticket that names a tag shows the asset's name, place and status.
- If the AMS is asleep or unreachable (4 second limit), tickets still work and the tag is shown as plain text.
- When both apps run on the same computer you don't need the API: the Helpdesk reads the AMS database file read-only instead (see `helpdesk/README.md`).

## Not included yet

Writing through the API (for example marking an asset faulty from a ticket), per-person API keys, and request logging. Those need the college's decision on who may change what.
