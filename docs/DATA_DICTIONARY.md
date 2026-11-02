# Data dictionary

Generated from the live database definition by `python docs/make_docs.py`. Do not edit by hand.

## activity

History of everything that happened to an asset. event_date is when it really happened, at is when it was entered.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | links to assets.id |
| user_id | INTEGER | links to users.id |
| text | TEXT | required |
| at | TEXT | default CURRENT_TIMESTAMP |
| event_type | TEXT |  |
| event_date | TEXT |  |
| reason | TEXT |  |
| approval_ref | TEXT |  |

## asset_ids

Other names for an asset: old asset codes, other systems' IDs (with their source) and previous tags. Searchable, unique per kind+value+source.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| kind | TEXT | required |
| value | TEXT | required |
| source | TEXT | required, default '' |
| added_at | TEXT | default CURRENT_TIMESTAMP |
| added_by | INTEGER | links to users.id |

## asset_interfaces



| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| name | TEXT | required |
| mac | TEXT |  |
| ip | TEXT |  |
| vlan | TEXT |  |
| connected_to | TEXT |  |
| notes | TEXT |  |
| added_by | INTEGER | links to users.id |
| added_at | TEXT | default CURRENT_TIMESTAMP |

## assets

One row per individually tracked item (and per part of an item). id is the internal key; tag is the permanent label that is never reused.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| tag | TEXT |  |
| name | TEXT | required |
| category_id | INTEGER | links to categories.id |
| room_id | INTEGER | links to rooms.id |
| parent_id | INTEGER | links to assets.id |
| status | TEXT | required, default 'Working' |
| brand | TEXT |  |
| model | TEXT |  |
| serial_no | TEXT |  |
| ip_address | TEXT |  |
| mac_address | TEXT |  |
| purchase_date | TEXT |  |
| warranty_until | TEXT |  |
| cost | REAL |  |
| vendor | TEXT |  |
| notes | TEXT |  |
| next_service_due | TEXT |  |
| created_at | TEXT | default CURRENT_TIMESTAMP |
| last_audit | TEXT |  |
| assigned_to | TEXT |  |
| assigned_due | TEXT |  |
| lifecycle_stage | TEXT |  |
| condition | TEXT | default 'Working' |
| verification | TEXT |  |
| managing_dept_id | INTEGER | links to departments.id |
| owning_dept_id | INTEGER | links to departments.id |
| using_dept_id | INTEGER | links to departments.id |
| custodian | TEXT |  |
| location_unresolved | INTEGER | default 0 |
| acquisition_basis | TEXT |  |
| po_number | TEXT |  |
| invoice_number | TEXT |  |
| indent_no | TEXT |  |
| cpr_no | TEXT |  |
| grn_no | TEXT |  |
| stock_register_ref | TEXT |  |
| cost_basis | TEXT |  |
| tax_included | INTEGER |  |
| legacy_code | TEXT |  |
| archived_at | TEXT |  |
| archive_reason | TEXT |  |
| import_batch_id | INTEGER |  |
| source_row | INTEGER |  |
| source_json | TEXT |  |
| rack | TEXT |  |

## attribute_defs

Extra fields a kind of asset asks for (name, data type, unit, allowed values, required).

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| category_id | INTEGER | required, links to categories.id |
| name | TEXT | required |
| datatype | TEXT | required |
| unit | TEXT |  |
| choices | TEXT |  |
| required | INTEGER | default 0 |
| sort | INTEGER | default 0 |

## attribute_values

The values entered for those fields on each asset.

| Field | Type | Notes |
|---|---|---|
| asset_id | INTEGER | primary key, links to assets.id |
| attr_id | INTEGER | primary key, links to attribute_defs.id |
| value | TEXT |  |

## buildings

Campus buildings.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | required |
| code | TEXT |  |
| campus | TEXT |  |

## categories

Kinds of asset (asset types), with the useful life used for book value.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | required |
| life_years | INTEGER | default 5 |

## check_findings



| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| kind | TEXT | required |
| room_id | INTEGER | links to rooms.id |
| asset_id | INTEGER | links to assets.id |
| description | TEXT |  |
| found_on | TEXT | required |
| found_by | INTEGER | links to users.id |
| status | TEXT | required, default 'Open' |
| resolution | TEXT |  |
| resolved_by | INTEGER | links to users.id |
| resolved_at | TEXT |  |

## contract_assets



| Field | Type | Notes |
|---|---|---|
| contract_id | INTEGER | primary key, links to contracts.id |
| asset_id | INTEGER | primary key, links to assets.id |

## contracts



| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| kind | TEXT | required |
| vendor | TEXT |  |
| reference | TEXT |  |
| start_date | TEXT |  |
| end_date | TEXT |  |
| cost | REAL |  |
| department_id | INTEGER | links to departments.id |
| notes | TEXT |  |
| archived_at | TEXT |  |
| created_by | INTEGER | links to users.id |
| created_at | TEXT | default CURRENT_TIMESTAMP |

## counters

The permanent-tag counter. It only goes up.

| Field | Type | Notes |
|---|---|---|
| name | TEXT | primary key |
| value | INTEGER | required |

## custody

Who holds an asset, over time. Exactly one record per asset has ended_at empty (enforced by a unique index).

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| holder_type | TEXT | required |
| department_id | INTEGER | links to departments.id |
| person | TEXT |  |
| room_id | INTEGER | links to rooms.id |
| due_date | TEXT |  |
| started_on | TEXT | required |
| started_at | TEXT | default CURRENT_TIMESTAMP |
| started_by | INTEGER | links to users.id |
| reason | TEXT |  |
| approval_ref | TEXT |  |
| ack_by | TEXT |  |
| ack_at | TEXT |  |
| ended_on | TEXT |  |
| ended_at | TEXT |  |
| ended_by | INTEGER | links to users.id |
| end_reason | TEXT |  |

## departments

Approved department list (managing, owning and using department on assets point here).

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | required |
| code | TEXT |  |

## document_links

Which asset or purchase batch a document belongs to.

| Field | Type | Notes |
|---|---|---|
| document_id | INTEGER | required, links to documents.id |
| asset_id | INTEGER | links to assets.id |
| purchase_batch_id | INTEGER | links to purchase_batches.id |

## documents

Purchase and ownership papers (PO, invoice, GRN, warranty...). Newer versions point back to the one they replace.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| file | TEXT | required |
| original_name | TEXT | required |
| ext | TEXT | required |
| size | INTEGER |  |
| sha256 | TEXT | required |
| kind | TEXT | required |
| version | INTEGER | required, default 1 |
| supersedes_id | INTEGER | links to documents.id |
| description | TEXT |  |
| reference | TEXT |  |
| uploaded_by | INTEGER | links to users.id |
| uploaded_at | TEXT | default CURRENT_TIMESTAMP |

## import_batches

One uploaded file: the original is stored untouched with its fingerprint; status runs Uploaded, Mapped, Previewed, Approved or Reversed.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| filename | TEXT | required |
| stored_file | TEXT | required |
| sha256 | TEXT | required |
| size | INTEGER |  |
| uploaded_by | INTEGER | links to users.id |
| uploaded_at | TEXT | default CURRENT_TIMESTAMP |
| sheet | TEXT |  |
| header_row | INTEGER | default 1 |
| settings_json | TEXT |  |
| status | TEXT | required, default 'Uploaded' |
| total_rows | INTEGER |  |
| created_n | INTEGER |  |
| skipped_n | INTEGER |  |
| error_n | INTEGER |  |
| approved_by | INTEGER | links to users.id |
| approved_at | TEXT |  |
| reversed_at | TEXT |  |
| notes | TEXT |  |

## import_mappings

Saved column mappings, reused when the same file layout arrives again.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | required |
| signature | TEXT | required |
| columns_json | TEXT | required |
| created_by | INTEGER | links to users.id |
| created_at | TEXT | default CURRENT_TIMESTAMP |

## issues

Problems reported against an asset.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| title | TEXT | required |
| type | TEXT |  |
| priority | TEXT |  |
| description | TEXT |  |
| status | TEXT | required, default 'Open' |
| reported_by | INTEGER | links to users.id |
| reported_at | TEXT | default CURRENT_TIMESTAMP |
| resolved_by | INTEGER | links to users.id |
| resolved_at | TEXT |  |
| resolution | TEXT |  |

## licences

Software the college has the right to use: quantity, renewal, key (visible to purchase and admins only).

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| product | TEXT | required |
| vendor | TEXT |  |
| licence_type | TEXT |  |
| metric | TEXT |  |
| quantity | INTEGER | required, default 1 |
| in_use | INTEGER |  |
| start_date | TEXT |  |
| renewal_date | TEXT |  |
| support_end | TEXT |  |
| key_ref | TEXT |  |
| cost | REAL |  |
| po_number | TEXT |  |
| department_id | INTEGER | links to departments.id |
| notes | TEXT |  |
| archived_at | TEXT |  |
| created_at | TEXT | default CURRENT_TIMESTAMP |

## photos

Pictures of an asset or issue.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| issue_id | INTEGER | links to issues.id |
| file | TEXT | required |
| uploaded_by | INTEGER | links to users.id |
| at | TEXT | default CURRENT_TIMESTAMP |

## purchase_batches

A purchase such as '20 computers' that has not yet been turned into individually tagged assets.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| import_batch_id | INTEGER | links to import_batches.id |
| description | TEXT | required |
| category_id | INTEGER | links to categories.id |
| department_id | INTEGER | links to departments.id |
| quantity | INTEGER | required |
| unit_cost | REAL |  |
| total_cost | REAL |  |
| cost_basis | TEXT |  |
| tax_included | INTEGER |  |
| vendor | TEXT |  |
| po_number | TEXT |  |
| invoice_number | TEXT |  |
| purchase_date | TEXT |  |
| status | TEXT | required, default 'Unconfirmed' |
| units_created | INTEGER | default 0 |
| notes | TEXT |  |
| created_at | TEXT | default CURRENT_TIMESTAMP |

## rooms

Rooms, labs and offices inside a building.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| building_id | INTEGER | required, links to buildings.id |
| name | TEXT | required |
| type | TEXT |  |
| floor | TEXT |  |

## services

Repairs and maintenance done.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| asset_id | INTEGER | required, links to assets.id |
| date | TEXT | required |
| type | TEXT |  |
| description | TEXT |  |
| done_by | TEXT |  |
| cost | REAL |  |
| next_due | TEXT |  |
| logged_by | INTEGER | links to users.id |

## staged_rows

The preview of each file row before anything enters the register: what will happen, errors, warnings, duplicate match.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| batch_id | INTEGER | required, links to import_batches.id |
| row_no | INTEGER |  |
| raw_json | TEXT |  |
| data_json | TEXT |  |
| kind | TEXT |  |
| errors_json | TEXT |  |
| warnings_json | TEXT |  |
| dup_asset_id | INTEGER |  |
| decision | TEXT | default 'Create' |
| asset_id | INTEGER |  |
| purchase_batch_id | INTEGER |  |

## stock_items

Consumables counted by quantity (toner, cables).

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | required |
| unit | TEXT | required, default 'nos' |
| department_id | INTEGER | links to departments.id |
| room_id | INTEGER | links to rooms.id |
| min_level | REAL | default 0 |
| notes | TEXT |  |
| archived_at | TEXT |  |
| created_at | TEXT | default CURRENT_TIMESTAMP |

## stock_movements

Every receipt, issue and count correction for those items.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| item_id | INTEGER | required, links to stock_items.id |
| qty | REAL | required |
| kind | TEXT | required |
| event_date | TEXT | required |
| entered_at | TEXT | default CURRENT_TIMESTAMP |
| user_id | INTEGER | links to users.id |
| department_id | INTEGER | links to departments.id |
| reason | TEXT |  |
| reference | TEXT |  |

## user_scopes

Extra departments a department-level user may see besides their own.

| Field | Type | Notes |
|---|---|---|
| user_id | INTEGER | primary key, links to users.id |
| department_id | INTEGER | primary key, links to departments.id |

## users

People who can sign in. A main role plus optional extra roles; department scopes are in user_scopes.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| username | TEXT | required |
| name | TEXT | required |
| password_hash | TEXT | required |
| role | TEXT | required |
| extra_roles | TEXT |  |
| department_id | INTEGER |  |

## value_maps

The reviewed dictionary: how a spelling in an old file (category, department, status) is understood.

| Field | Type | Notes |
|---|---|---|
| id | INTEGER | primary key |
| kind | TEXT | required |
| source | TEXT | required |
| target | TEXT | required |

