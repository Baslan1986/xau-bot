# Solnerv Attend API contract (Phase 1)

Status: **draft for the lead's review**. The code in `attend/` implements this contract, and the tests in `attend/tests/test_rules.py` exercise it.

## 1. Who owns what

| Data | Owner | Attend's role |
|---|---|---|
| People (identity, roles, team membership, name forms) | **SolNerve** (`/people`, roster held in Shams) | Read-only mirror keyed by `personRef` |
| Leave records, approvals, balances | **SolNerve** | Answers interlock checks; when leave is approved it moves shifts and mirrors balances for display |
| Plant profiles | Operator (owner) | Stores them and validates them |
| Shift patterns, shifts, clock events, presence, geofences | **Attend** | Source of truth |
| Message delivery (WhatsApp) | **Shams** | Attend only POSTs to one outbound hook |

The mode is set with `ATTEND_PEOPLE_SOURCE=solnerve`. In that mode, Attend's local people and leave writes return **409**: `POST/PUT /api/admin/employees`, `/employees/bulk`, `POST /api/leaves`, `/respond`, `/cancel` and `/admin/leaves/{id}/decide`. The app login (phone and PIN) stays in Attend: `PUT /api/admin/employees/{id}/login`.

`local` mode keeps a self-contained people and leave workflow, for demos and tests. Whether to keep it is an open question for the lead.

## 2. Conventions

- Base path: `/api/integration`. Auth: `Authorization: Bearer <ATTEND_INTEGRATION_TOKEN>`. If the token is unset, every endpoint returns **503**; a wrong token returns **401**.
- People are referenced only by `personRef` (SolNerve id). Responses never carry names, phone numbers or money.
- Times are ISO-8601. Send a UTC offset (`2026-11-18T07:00:00+02:00`). A time without an offset is read in the plant's timezone. Attend returns plant-local time with its offset.
- Ids (`tenantId`, `plantId`) match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`.
- Batch endpoints report errors per item and never fail the whole batch.

## 3. Plant profile

`PUT /api/integration/plants` upserts a profile, keyed by (`tenantId`, `plantId`).

```json
{
  "tenantId": "t1", "plantId": "p1", "name": "…",
  "timezone": "Area/City", "locale": "ar",
  "polygons": { "plant": [[lat, lng], ...], "site": [[lat, lng], ...] | null },
  "shifts": [ { "code": "D", "start": "07:00", "hours": 12, "label": "…" } ],
  "graceMinutes": 10, "minStaff": 1
}
```

- `polygons.plant` gates **arrive/depart**. `polygons.site` (optional) gates **enter/exit**; when it is null, the plant polygon is used.
- Shift `code`: 1–8 characters, A–Z or 0–9. `O` is reserved for rest days in team patterns.
- `graceMinutes` sets the humane-time window: an arrival up to `grace` minutes late counts as on time, and a departure within `grace` of the shift end counts as the end.
- Returns the stored profile (with Attend's internal `id`), or **400** with a reason.

## 4. People (SolNerve → Attend)

`PUT /api/integration/people`

```json
{ "tenantId": "t1", "people": [ {
    "personRef": "sn-123", "displayName": "…",
    "aliases": ["…", "…"], "roles": ["technician"],
    "plantId": "p1", "team": "A", "jobTitle": "…", "active": true,
    "loginPhone": "07…", "balances": { "annual": 10, "sick": 14, "departureHours": 8 }
} ] }
```

- Upserts by (`tenantId`, `personRef`).
- Roles `attend_admin`, `admin` or `supervisor` grant approval rights in Attend. Authorisation comes from roles, never from names.
- `team` is matched by name to an Attend team at that plant. Teams Attend doesn't have come back in `unknownTeams`, and nothing is created for them.
- `aliases` is the full set of name forms for that person (for example a first name, a title plus the name, and a family name). A form already owned by another person is an error for that item.
- `balances` is a display mirror only. SolNerve remains the source.
- Response: `{ "upserted": n, "errors": [{"personRef", "error"}], "unknownTeams": [...] }`

## 5. Names: exact or asked, never guessed

`POST /api/integration/names/resolve` with body `{ "tenantId": "t1", "form": "…", "context": "optional, ≤200 chars" }`

- `{"status": "matched", "attendUserId": 7, "personRef": "sn-123"}` means the form matched one known alias exactly, after normalisation.
- `{"status": "unknown", "questionId": 12}` means no exact match. An open question is created (or reused) for a human to answer.
- Normalisation removes only spelling noise: diacritics, tatweel, the variants أ/إ/آ/ٱ → ا, dots and repeated spaces. It never removes words, so a title or kunya that isn't a listed alias stays unknown.

Questions:

- `GET /api/integration/names/questions?tenantId=t1` returns `[{"questionId", "form", "context"}]`.
- `POST /api/integration/names/questions/{id}/answer` with body `{"tenantId", "personRef"}` turns the form into an alias, so it is never asked again.

Admins can answer the same questions in Attend's UI (People → "أسماء بحاجة تأكيد").

## 6. Leave

### 6.1 Interlock check (SolNerve asks before approving)

`POST /api/integration/interlock/check`

```json
{ "tenantId": "t1", "personRef": "sn-1", "kind": "annual|sick|departure",
  "start": "…", "end": "…", "substituteRef": "sn-2" }
```

- With `substituteRef`: returns `{ "ok": true|false, "reason": "…"|null, "amount": n }`.
- Without `substituteRef`: returns `{ "ok": null, "amount": n, "candidates": [{"personRef", "ok", "reason"}] }`.
- `amount` is the number of the person's shifts in the window (annual/sick), or hours (departure).
- If the window is invalid (for example no shifts in it), the response is `{ "ok": false, "reason": "…" }`.

Rules:

- **annual / sick:** the substitute is at the same plant, active, has no overlapping leave, and is not scheduled during any of the requester's shifts in the window.
- **departure:** the substitute is currently checked in at the plant.

### 6.2 Leave sync (SolNerve → Attend)

`PUT /api/integration/leaves`

```json
{ "tenantId": "t1", "leaves": [ {
    "leaveRef": "lv-9", "personRef": "sn-1", "kind": "annual",
    "start": "…", "end": "…", "status": "pending|approved|rejected|cancelled",
    "substituteRef": "sn-2"
} ] }
```

- Idempotent by `leaveRef`.
- On the first `approved` (annual/sick), Attend re-checks the interlock, then moves the requester's shifts in the window to the substitute (`source=substitute`, `covering_for`). With no substitute, those shifts are removed. The substitute is notified.
- Once applied, changing the window or status of that leave is refused, and the reason is returned. Reversal is an open question.
- Response: `{ "results": [ {"leaveRef", "ok", "applied", "shiftsMoved"} | {"leaveRef", "ok": false, "error"} ] }`

## 7. Attendance out (Attend → SolNerve)

### Monthly attendance

`GET /api/integration/attendance?tenantId=t1&month=YYYY-MM[&plantId=p1]`

```json
{ "tenantId": "t1", "month": "2026-11", "people": [ {
  "personRef": "sn-1", "scheduled_hours": 0, "worked_hours": 0, "site_hours": 0,
  "overtime_hours": 0, "late_minutes": 0, "early_minutes": 0, "absences": 0,
  "leave_shifts": 0, "missing_departs": 0, "shifts_done": 0 } ] }
```

Hours and minutes only. **No names, no phone numbers, no money.** Pay is computed elsewhere and must never be joined back to this payload in logs.

### Presence (for check-in on SolNerve `/people`)

`GET /api/integration/presence?tenantId=t1&plantId=p1` returns:

```json
{ "plantId": "p1", "people": [{ "personRef": "sn-1", "state": "arrive|enter|exit|depart|null", "since": "…" }] }
```

## 8. Outbound notification hook (Attend → Shams → WhatsApp)

Attend contains no messaging client. When `ATTEND_NOTIFY_URL` is set, every notification is POSTed there:

```
POST {ATTEND_NOTIFY_URL}
Content-Type: application/json
X-Attend-Event: <kind>
Idempotency-Key: attend-notification-<n>
X-Attend-Signature: sha256=<hex HMAC-SHA256(body, ATTEND_NOTIFY_SECRET)>
```

```json
{ "id": "attend-notification-42", "tenantId": "t1", "plantId": "p1",
  "personRef": "sn-1", "attendUserId": 7, "kind": "missed_depart",
  "locale": "ar", "text": "Arabic text, ready to send",
  "params": { }, "createdAt": "2026-11-18T19:05:00Z" }
```

- Any **2xx** means delivered, and the notification is never sent again. Anything else, or a network error, is retried on the next tick as soon as possible, with backoff capped at 10 minutes, up to `ATTEND_NOTIFY_MAX_ATTEMPTS` (default 20) attempts. After that it is marked `failed`.
- Delivery is at-least-once, so the receiver must de-duplicate on `id`.
- `kind` values: `sos`, `sos_peer`, `missed_arrival`, `missed_arrival_admin`, `missed_depart`, `coverage_gap`, `cover_request`, `cover_accepted`, `cover_declined`, `cover_assigned`, `cover_cancelled`, `leave_pending`, `leave_approved`, `leave_rejected`, `shift_added`, `info`. Shams may use `kind`, `params` and `locale` to template messages instead of `text`.
- SOS is dispatched immediately; everything else goes out within one tick (`ATTEND_TICK_SECONDS`, default 20).
- Attend never logs payloads; only counts and error codes are kept.

## 9. Catch-up rule ("a missed step runs once, as soon as possible")

| Check | Key | Behaviour after downtime |
|---|---|---|
| Missed arrival (grace + 15 min into the shift) | shift id | One reminder to the employee and one to admins |
| Missed depart (shift end + grace + 60 min) | arrive event id | One reminder |
| Coverage gaps, next 48 h | plant + plant-local date | Runs once on the next tick, not once per missed day |
| Hook delivery | notification id | Retried until delivered, never duplicated |
