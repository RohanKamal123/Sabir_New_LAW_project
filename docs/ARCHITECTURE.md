# Architecture

## The shape of it

```
                    supremecourt.gov.bd          bdlaws.minlaw.gov.bd
                    (no API, no robots.txt)      (UTF-16, official Code)
                              │                            │
                              └────────────┬───────────────┘
                                           │
                                  barrister/http.py
                        serial · rate-limited · cached · honest UA
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
          scrapers/cause_list.py   scrapers/case_status.py   scrapers/bdlaws.py
                    │                      │                      │
                    └──────────────────────┼──────────────────────┘
                                           │
    ┌───────────────────────────────── services/ ─────────────────────────────────┐
    │  matching   watchlist   tracker   statutes   limitation   matters  drafting │
    │  notify     bot                                                             │
    └──────────────────────────────────┬──────────────────────────────────────────┘
                                       │
                            db.py — one SQLite file
                                       │
              ┌────────────────┬───────┴────────┬────────────────┐
              │                │                │                │
           cli.py           api.py           web.py       services/bot.py
          (cron +          (JSON API)      (server-rendered   (Telegram)
         interactive)                          HTML)
```

Four front ends, one set of services. That is the only structural rule in the
codebase and everything else follows from it: matching a name against a cause
list, diffing a case history, computing a limitation period — each exists
exactly once, so the bot cannot drift from the web UI, and the API cannot drift
from the cron job.

## Layers

### `barrister/http.py` — the politeness layer

Every outbound request goes through `PoliteClient`. Nothing else in the codebase
calls `httpx` against an upstream. That is deliberate: the whole product depends
on continued access to two small government sites, so the constraint belongs in
one place where it cannot be forgotten.

It is serial (no concurrency anywhere), enforces a floor between requests,
caches responses on disk, retries only transient failures, and sends a
`User-Agent` with a contact address.

It also owns `decode_html()`, which sniffs UTF-16. bdlaws serves the entire
Bangladesh Code as UTF-16 and means it; decoding those bytes as UTF-8 produces
mojibake that silently defeats every selector downstream rather than raising.

### `barrister/scrapers/` — parsing, no side effects

Each scraper is a pure function from HTML to dataclasses plus a thin fetch
wrapper. They never touch the database. That is what makes the fixture-driven
test suite possible: 300+ tests run against real pages saved from the live
sites, with no network.

The parsers encode things learned the hard way from real markup:

- **Cause list.** A row with an empty serial is a *connected matter* heard
  "(with)" the previous entry, and must inherit its serial or it vanishes from a
  barrister's alerts. Single-cell rows are section headings — being serial 3
  "For Judgment" is a very different day than serial 3 "For Hearing". Case
  numbers are not integers: `Civil Rule 832(FM)/2006` is real.
- **Case status.** The public search page is JS-driven, but underneath are three
  plain endpoints. The 110-entry case-type registry those dropdowns populate is
  shipped in `data/case_types.json`, turning a status check into one request
  instead of three and letting a user type "Writ Petition" rather than `13`.
- **bdlaws.** Part headings sit *between* section links in document order, so
  the parser walks the document carrying the current Part forward.

### `barrister/services/` — everything that decides something

| Module | Responsibility |
|---|---|
| `matching` | Does this listing belong to this barrister? Name normalisation, generous by design. |
| `watchlist` | Users, watches, the nightly sweep, alert queueing and delivery. |
| `tracker` | Case-status snapshots and the three changes worth a notification. |
| `statutes` | Corpus storage and FTS5 retrieval. Never generates. |
| `limitation` | Date arithmetic over encoded statutory rules. Refuses unverified ones. |
| `matters` | The case file: clients, cases, notes, documents, time, deadlines. |
| `drafting` | Template + narrative, across two model providers. |
| `notify` | Alert delivery (console, Telegram, null). |
| `bot` | Telegram command routing. |

### `barrister/db.py` — one SQLite file, no ORM

The schema is small and the point of Tier 0 is that a solo practitioner can run
this on a cheap box and copy the database off as a backup. An ORM and a
migration framework would be more machinery than the problem has.

`record_snapshot()` is the generic change-detection primitive: it stores a
content hash per `(source, key)` and reports whether anything changed, so "did
this case move since yesterday?" is a hash comparison rather than a re-parse.

## Design decisions worth knowing

### Why the parsers are separate from the fetchers

So that tests can be honest. A parser test that mocks HTTP proves the mock
works. A parser test that runs against a page the Court actually served proves
the parser works. Every fixture in `tests/fixtures/` is a real response.

### Why change detection compares parsed values, not bytes

The Court's pages change for boring reasons — whitespace, a re-render, a
rotating banner. Diffing bytes would alert on all of it. `tracker.diff_status()`
compares parsed hearings and reports only three things: a new listing, a result
filled in where there was none, and a result amended.

### Why the limitation calculator refuses its own data

The First Schedule's 183 articles exist only as a PDF table.
`tools/extract_limitation_schedule.py` extracts them, but PDF-table extraction
is imperfect — see the column bleed still visible on some articles. A wrong
limitation period costs a client their cause of action, so every article carries
`"verified": false` and `deadline_for_article()` raises `UnverifiedRuleError`
rather than answer. Structural rules (ss. 4, 12) are exact date arithmetic over
text quoted verbatim in the source, and those are safe.

This is the one place where the honest engineering answer is a feature that
refuses to work until a human does something.

### Why drafting splits template from narrative

The shape of a Bangladeshi Supreme Court petition is fixed by convention. That
is a template, not something a model should re-invent per document. So the
template owns the structure and the model writes only the narrative paragraphs,
from facts the barrister supplied. Two guards sit behind that: a system prompt
forbidding any citation the barrister did not supply, and `check_citations()`
flagging law-report references that appear anyway.

### Why the web UI is server-rendered

The product is a few hundred rows of SQLite and some scraped HTML. A SPA would
add a build step, a second set of models, and a loading spinner to a page that
renders in one query — and would break printing, which barristers actually do.

### Why single-tenant

`web.current_user()` returns the first user. Multi-tenancy means authentication,
sessions and a password-reset flow, none of which earns its keep before there
are users. The database schema is already per-user throughout (`user_id` on
every owned table), so adding auth later is a login form, not a migration.

## Data flow: the nightly sweep

```
  cron 20:30
      │
      ▼
  barrister sweep
      │
      ├─ fetch_benches(div 1) ────────► ~4 requests
      ├─ fetch_benches(div 2) ────────► 1 request
      ├─ for each of ~60 benches ─────► 1 request each, 1.5s apart
      │      parse_cause_list()
      │
      ├─ store_entries()  ─────────────► cause_list_entries
      │
      ├─ for each user with active watches
      │      match_all(entries, watches)      ← matching.py
      │      _matter_refs()                   ← matters.py, names the file
      │      format_alert()                   ← one message, not one per matter
      │      queue_alert()                    ← dedupe key makes re-runs safe
      │
      └─ deliver_pending() ────────────► Telegram; failures stay queued
```

Total: roughly 65 requests spread over 90 seconds, once a day. Comparable to one
person browsing the cause list.

## Testing

307 tests, no network. The suite is weighted towards the places where being
wrong is expensive:

- **Matching** has the most tests per line of any module, because a false
  negative is a missed hearing.
- **Limitation** tests assert the *working*, not just the answer — which rule
  fired, what was excluded, what authority was cited.
- **Parsers** run against real saved markup, so a change to the Court's HTML is
  caught against what it actually serves.
- **Drafting** tests the citation guard hardest, since that is the guard against
  the failure mode that would lose a practitioner's trust permanently.
