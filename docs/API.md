# API reference

Two surfaces on one FastAPI app: a JSON API and a server-rendered web UI. Both
call the same services, so neither can drift from the other.

```bash
uvicorn barrister.api:app --host 127.0.0.1 --port 8000
```

Interactive docs at `/docs`. **There is no authentication** — see the deployment
note in [`OPERATIONS.md`](OPERATIONS.md).

Endpoints marked **live** reach the Court's website on request and are therefore
slow (a rate-limited fetch, possibly a cache miss) and can fail if the site is
down. Everything else reads local SQLite.

---

## Meta

### `GET /health`

```json
{
  "status": "ok",
  "drafting_provider": "anthropic",
  "sources": {
    "supreme_court": "https://www.supremecourt.gov.bd/web/",
    "bdlaws": "http://bdlaws.minlaw.gov.bd/"
  }
}
```

`drafting_provider` is the resolved provider — `anthropic`, `deepseek`, or
`none` when no key is configured.

---

## Users and watches

### `POST /users` → `201`

```json
{"name": "Sabir Rahman", "chamber": "Rahman & Co.", "telegram_chat_id": "123456789"}
```

### `POST /users/{user_id}/watches` → `201`

```json
{"kind": "advocate", "value": "Sabir Rahman"}
```

`kind` is `advocate`, `party` or `case`. Anything else is `422`.

Matching is generous by design: honorifics and spacing are folded, so
`Abu Hanif` matches `Mr. Md. Abu Hanif`, and `CR 2347 of 2007` matches
`Civil Revision 2347/2007`. A false positive costs ten seconds of reading; a
false negative costs a hearing.

### `GET /users/{user_id}/watches`
### `GET /users/{user_id}/alerts?limit=20`

Alerts newest first. `delivered_at` is null while an alert is still queued —
delivery failures are retried on the next sweep rather than dropped.

---

## Cause lists

### `GET /cause-list/benches?division=2` — **live**

Every bench sitting on the current list. `division` is `1` (Appellate) or `2`
(High Court).

```json
[{"court_id": "42", "bench_id": "10294", "court_name": "Annex Building Court No. 18",
  "judges": "Justice Sheikh Abdul Awal And Justice A. K. M. Rabiul Hassan",
  "list_date": "24/08/2026", "division": "High Court Division"}]
```

### `GET /cause-list?division=2&bench_id=10294` — **live**

The listings before one bench. Omit `bench_id` and every bench in the division
is fetched, which is ~60 rate-limited requests — use the sweep instead.

Fields worth knowing:

| Field | Meaning |
|---|---|
| `serial` | Position on the list. A connected matter carries its parent's serial. |
| `connected_to` | Set when this is a matter heard "(with)" the entry above. |
| `section` | `For Hearing`, `For Judgment`, `Fixing a date of hearing`… |
| `case_number` | A string, not an integer — `832(FM)` occurs. |
| `notes` | Parenthesised annotations: `heard in part`, `with`, a next date. |
| `advocates` | Extracted from `[Adv : …]`, split on "with", role suffixes stripped. |

`section` matters more than it looks: serial 3 "For Judgment" is a very
different day from serial 3 "For Hearing".

---

## Cases

### `GET /case-types?division=2`

The Court's own registry of 110 case types, shipped locally. Use it to resolve a
name to the `case_type_id` the site wants.

### `GET /cases/{case_type}/{number}/{year}?division=2` — **live**

`case_type` is the human name — `Writ Petition`, `First Appeal`. Exact match
wins; otherwise the shortest containing name, so `writ petition` does not
resolve to `In re : VC Writ Petition`. Unknown names are `400`.

```json
{
  "case_type": "First Appeal", "case_number": "226", "case_year": "2013",
  "petitioner": "Md. Ruhul Amin and ors.", "petitioner_lawyer": "Mr. Abdur Rahim",
  "respondent": "Alhaj Md. Aiyub Ali Sikder and ors.",
  "hearings": [
    {"number": 22, "date": "24/08/26", "court": "Annex Building Court No. 18",
     "judges": "Justice Sheikh Abdul Awal, …", "result": null}
  ]
}
```

Hearings are newest first. `result` is the Court's own text, reproduced as
published — including its spelling.

---

## Statutes

### `GET /statutes/search?q=…&limit=10`

Retrieval only. Every result is a verbatim span of the official Bangladesh Code
with the URL it came from; nothing is summarised or paraphrased.

A query shaped like a citation (`s. 5 of the Limitation Act`) is answered by
exact section lookup first — a barrister who names a section wants that section,
not the ten that mention it. Anything else goes to FTS5.

Returns `[]` when the corpus has not been synced (`barrister statutes sync`).

### `GET /statutes/stats`

```json
{"acts": 1, "sections": 30}
```

---

## Limitation

### `POST /limitation`

```json
{
  "start_date": "2026-01-15",
  "days": 90,
  "proceeding": "appeal",
  "copy_applied_on": "2026-01-20",
  "copy_ready_on": "2026-02-03",
  "holidays": ["2026-03-26"]
}
```

`proceeding` is `suit`, `appeal`, `leave_to_appeal`, `review` or `application`.
Copy dates apply only to the middle three (s. 12(2)); supplied on a suit they
are ignored with a warning.

```json
{
  "deadline": "2026-04-30",
  "excluded_days": 15,
  "days_remaining": -116,
  "expired": true,
  "steps": [
    {"rule": "s. 12(2)", "explanation": "Exclude the day judgment was pronounced (2026-01-15)", "result": null},
    {"rule": "period", "explanation": "Add the prescribed period of 90 days from 2026-01-15", "result": "2026-04-15"},
    {"rule": "s. 12(2)", "explanation": "Exclude 15 day(s) requisite for the decree/order copy …", "result": null},
    {"rule": "s. 12", "explanation": "Add back 15 excluded day(s)", "result": "2026-04-30"}
  ],
  "warnings": ["If this period has expired, s. 5 may still permit admission …"],
  "citations": ["Limitation Act 1908, s. 12(2) — \"In computing the period …\""]
}
```

`steps` and `citations` are the point. A limitation figure you cannot audit is a
figure you cannot file on.

**Use `article` instead of `days` and you will usually get a `409`:**

```json
{"detail": "Schedule article 152 has not been verified by a lawyer. Extracted period: 'Thirty days'; trigger: 'The date of the decree or order appealed from.' Check it against http://bdlaws.minlaw.gov.bd/act-88.html and set \"verified\": true, or pass allow_unverified=True …"}
```

That is not a bug. The First Schedule was machine-extracted from a PDF table and
no article has been checked by a lawyer yet. Pass `"allow_unverified": true` to
compute anyway; the result comes back with `UNVERIFIED RULE` first in
`warnings`.

### `GET /limitation/review-queue`

The articles still awaiting verification, with what was extracted for each.

---

## Drafting

### `GET /drafting/templates`

```json
["application_for_stay", "leave_to_appeal", "writ_petition"]
```

### `POST /drafting/draft`

```json
{
  "template": "writ_petition",
  "petitioners": ["Md. Karim Uddin of Dhaka"],
  "respondents": ["Bangladesh, represented by the Secretary, Ministry of Land"],
  "subject_matter": "Challenge to the acquisition notice dated 12.03.2026",
  "facts": "The petitioner owns 3 katha at Mirpur. No hearing was given.",
  "grounds": ["That the notice violates the principles of natural justice."],
  "prayers": ["Issue a Rule Nisi calling upon the respondents to show cause"],
  "supplied_authorities": ["45 DLR (AD) 123"]
}
```

The template owns the document's structure; the model writes only the narrative
paragraphs from the facts given.

`supplied_authorities` is a whitelist, not a hint. The system prompt forbids
citing anything else, and any law-report citation appearing in the output that
was not supplied comes back in `warnings` prefixed `UNVERIFIED CITATION`.

```json
{
  "text": "IN THE SUPREME COURT OF BANGLADESH\n…",
  "provider": "anthropic",
  "model": "claude-opus-5",
  "warnings": ["AI-assisted draft. Read every paragraph and verify every date, figure and authority before it is settled or filed."]
}
```

With no model configured, `provider` is `none` and the narrative is a marked
gap — the template still renders with the facts laid out, which is a usable
skeleton.

Errors: `400` for an unknown template or a missing template variable, `502` when
the configured provider fails or declines.

---

## Web UI routes

Server-rendered HTML, same services.

| Route | Page |
|---|---|
| `GET /` | Today — your listings, the diary, practice figures |
| `GET /cause-list?division=&bench_id=` | Any bench's list, your matters marked in the margin |
| `GET /matters?status=` | Files |
| `GET /matters/{id}` | One file: cases, deadlines, notes, documents, time |
| `GET /diary?days=30` | Deadlines across every file |
| `GET /statutes?q=` | Statute search |
| `GET /limitation?start=&days=&proceeding=&copy_applied=&copy_ready=` | Calculator with its working |
| `GET`/`POST /drafting` | Drafting |
| `GET /static/app.css` | The design system |
