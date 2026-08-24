# Barrister Tools

Practitioner software for barristers of the **Supreme Court of Bangladesh** —
cause-list alerts, case tracking, statute lookup, limitation calculation and
template drafting, shipped as one product.

All of Tier 0 and Tier 1 of [`docs/ROADMAP.md`](docs/ROADMAP.md). Read
[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) before pointing the scrapers at
production on a schedule.

## What works

| Roadmap | Feature | Where |
|---|---|---|
| #1 | Cause-list watch and alerts | `scrapers/cause_list.py`, `services/watchlist.py` |
| #2 | Case status tracking with change detection | `scrapers/case_status.py`, `services/tracker.py` |
| #3 | Statute lookup over the Bangladesh Code | `scrapers/bdlaws.py`, `services/statutes.py` |
| #4 | Template-based drafting (Claude or DeepSeek) | `services/drafting.py` |
| #5 | Rule-based limitation calculator | `services/limitation.py` |
| #6 | Matter and case-file management | `services/matters.py` |
| #7 | Telegram bot interface | `services/bot.py` |

Four front ends over one set of services — a CLI for cron and the terminal, a
JSON API, a server-rendered web UI, and a Telegram bot. Matching, diffing and
limitation logic exists exactly once, so no surface can drift from another.

All parsers were built against real pages saved from the live sites, which are
committed as fixtures in `tests/fixtures/` and are what the 309-test suite runs
against.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/JOURNEY.md`](docs/JOURNEY.md) | **The Night Before** — a barrister's week through the product, with screen designs |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pieces fit and why each decision was made |
| [`docs/API.md`](docs/API.md) | Every JSON endpoint and web route |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Install, cron, systemd, backup, what to do when it breaks |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The design system and the reasoning behind it |
| [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) | Scraping and Bar Council checks — done and outstanding |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | The original plan and current build status |

## Install

```bash
pip install -r requirements.txt
export BARRISTER_CONTACT_EMAIL="you@chambers.test"   # please set this
```

## The wedge: cause-list alerts

Register yourself, say what to watch for, then run the sweep the evening the
lists drop.

```bash
barrister adduser "Sabir Rahman" --telegram 123456789
barrister watch 1 advocate "Sabir Rahman"        # your name as it appears in the list
barrister watch 1 case "First Appeal 226/2013"   # a specific matter
barrister watch 1 party "Karim Uddin"            # a client

barrister sweep --dry-run     # print alerts instead of sending
barrister sweep               # deliver via Telegram if TELEGRAM_BOT_TOKEN is set
```

You get **one** message listing all your matters, not one per matter:

```
Cause list 24/08/2026: 2 matter(s) listed
• First Appeal 226/2013
   High Court Division — Annex Building Court No. 18
   Bench: Justice Sheikh Abdul Awal and Justice A. K. M. Rabiul Hassan
   Serial 10 — Fixing a date of hearing
   Md. Ruhul Amin and ors. vs Alhaj Md. Aiyub Ali Sikder and ors.
   Matched: case listed: First Appeal 226/2013
```

Re-running the sweep is idempotent — an alert already delivered is not resent.

Cron it for the evening the lists are published:

```cron
30 20 * * * /usr/local/bin/barrister sweep >> /var/log/barrister.log 2>&1
```

### Matching

Name matching is deliberately generous, because a false positive costs ten
seconds of reading and a false negative costs a missed hearing. Honorifics
(`Mr.`/`Md.`/`Mst.`), spacing and initials are folded away, so a watch on
`Abu Hanif` matches `Mr. Md. Abu Hanif`. Case numbers accept the shorthand
practitioners actually type — `CR 2347 of 2007` finds `Civil Revision 2347/2007`.
Every alert says *why* it matched.

## Case status

```bash
barrister status "First Appeal" 226 2013
barrister status "Writ Petition" 1234 2025 --track-for 1   # alert me on changes
```

The tracker diffs *parsed hearings*, not page bytes, and only reports three
things: a new listing, a result recorded against a hearing that had none, or a
result amended. Case-type names resolve against the court's own registry of 110
types (`barrister/data/case_types.json`), so you type "Writ Petition", not `13`.

## Statutes

```bash
barrister statutes sync --act 88          # The Limitation Act, 1908
barrister statutes search "s. 5 of the Limitation Act"
barrister statutes search "sufficient cause appeal admitted after period"
```

Retrieval only — every result is a verbatim span of the official Bangladesh Code
with its source URL. No model sees the text on the way to you, which is why this
could ship on day one while grounded case-law answers cannot.

A query that reads like a citation is answered by exact section lookup first;
anything else goes to FTS5 full-text search.

## Limitation

```bash
barrister limitation --from 2026-01-15 --days 90 --proceeding appeal \
    --copy-applied 2026-01-20 --copy-ready 2026-02-03
```

```
Filing deadline:   2026-04-30 (Thursday)

Working:
  1. [s. 12(2)] Exclude the day judgment was pronounced (2026-01-15)
  2. [period] Add the prescribed period of 90 days from 2026-01-15 -> 2026-04-15
  3. [s. 12(2)] Exclude 15 day(s) requisite for the decree/order copy (2026-01-20 to 2026-02-03)
  4. [s. 12] Add back 15 excluded day(s) -> 2026-04-30
```

It shows its working and quotes the section it relied on, because a figure you
cannot audit is a figure you cannot file on.

Encoded: **s. 12(1)** (exclude the starting day), **s. 12(2)** (exclude the day
of pronouncement and the time requisite for the decree/order copy), **s. 12(3)**
(the judgment copy too), **s. 4** (a deadline falling on a day the Court is
closed runs to the day it re-opens — Friday and Saturday by default, plus any
`--holiday` you pass). **s. 5** condonation is never computed; an expired period
is reported as expired with a pointer to s. 5, because "sufficient cause" is a
judgment call.

### The First Schedule is not trusted yet

The Schedule's 183 articles are published only as a PDF table.
`tools/extract_limitation_schedule.py` extracts them, but PDF-table extraction is
imperfect, so **every article is `"verified": false`** and the calculator refuses
to use one:

```bash
$ barrister limitation --from 2026-01-15 --article 152 --proceeding appeal
UnverifiedRuleError: Schedule article 152 has not been verified by a lawyer.
Extracted period: 'Thirty days'; trigger: 'The date of the decree or order appealed from.'
```

```bash
barrister review-queue     # what still needs checking
```

Check an article against the official text, set `"verified": true` in
`barrister/data/limitation_schedule.json`, and it starts working. This is the
"knowledge-encoding, verifiable only by a lawyer" work the roadmap describes, and
it is the honest state of it.

## Drafting

```bash
barrister templates
barrister draft writ_petition \
    --petitioner "Md. Karim Uddin of Dhaka" \
    --respondent "Bangladesh, represented by the Secretary, Ministry of Land" \
    --subject "Challenge to the acquisition notice dated 12.03.2026" \
    --facts "The petitioner owns 3 katha at Mirpur. No hearing was given." \
    --ground "That the notice violates the principles of natural justice." \
    --prayer "Issue a Rule Nisi calling upon the respondents to show cause" \
    --out petition.txt
```

The **template owns the document's structure** — cause title, `SHEWETH`, numbered
grounds, the prayer in Roman numerals, the closing. The model only writes the
narrative paragraphs, from facts you supply.

Two safeguards, because an invented citation is the fastest way to lose a
practitioner's trust:

1. The system prompt forbids citing any case or law report you did not supply
   verbatim, and requires `[AUTHORITY TO BE SUPPLIED]` instead of a guess.
2. `check_citations()` scans the output for DLR/BLD/BLC/BLT/MLR-style references
   and warns about any that were not in your `--authority` list.

With no model configured the template still renders with the facts laid out and
the narrative marked as an explicit gap — a usable skeleton.

### Providers

| Provider | Env | Default model |
|---|---|---|
| Claude (default) | `ANTHROPIC_API_KEY` | `claude-opus-5` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` |

`BARRISTER_DRAFTING_PROVIDER` is `auto` by default: Claude if its key is set,
else DeepSeek, else templates only. Force one with
`BARRISTER_DRAFTING_PROVIDER=deepseek`. Override models with
`BARRISTER_DRAFTING_MODEL` / `BARRISTER_DEEPSEEK_MODEL`, and point DeepSeek at a
proxy with `DEEPSEEK_BASE_URL`.

Claude goes through the official `anthropic` SDK (streaming, adaptive thinking,
refusal fallbacks). DeepSeek goes through its OpenAI-compatible HTTP endpoint,
which needs no extra package.

## Files

The thing that turns four tools into one desk.

```bash
barrister matter open 1 "Ruhul Amin v Aiyub Ali" \
    --client "Md. Ruhul Amin" --case "First Appeal 226 2013"
barrister matter note 1 MAT-2026-001 "Conference; instructions to press the appeal" --kind attendance
barrister matter time 1 MAT-2026-001 150 "Settling grounds of appeal" --rate 6000
barrister matter show 1 MAT-2026-001
barrister diary 1
```

Linking a case to a file also starts watching it, so the nightly sweep reports
`First Appeal 226/2013 [MAT-2026-001]` instead of a bare number you have to
recognise at 9pm.

## Telegram

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
barrister bot
```

Long polling, so no public HTTPS endpoint and no inbound firewall rule.

```
/today          your listings for today
/diary          deadlines falling due
/matters        your open files
/case First Appeal 226 2013
/statute s. 5 of the Limitation Act
/limitation 2026-01-15 90 appeal
/note MAT-2026-001 Conference with client
/time MAT-2026-001 90 Drafting the petition
```

## Web UI

```bash
uvicorn barrister.api:app --host 127.0.0.1 --port 8000
```

Server-rendered. The design takes the printed cause list and the law report as
its reference rather than a dashboard: hairline rules instead of cards, serif
text with tabular figures so case numbers and dates align down the page, and a
single accent colour that means *urgent* and nothing else. Your own matters are
marked with a rule in the margin, the way you would mark them on paper. It
prints correctly. See [`docs/DESIGN.md`](docs/DESIGN.md).

**No authentication** — it is single-tenant and assumes it is reachable only by
the person it belongs to. Bind it to localhost.

## HTTP API

```bash
uvicorn barrister.api:app --reload
```

Full reference in [`docs/API.md`](docs/API.md). Interactive docs at `/docs`.

The API and the web UI mount on the same application, so both share one
implementation of the matching, diffing and limitation logic rather than each
growing their own copy.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BARRISTER_DATA_DIR` | `~/.barrister` | Database and HTTP cache |
| `BARRISTER_CONTACT_EMAIL` | *(unset)* | Goes in the `User-Agent`. Set it. |
| `BARRISTER_REQUEST_DELAY` | `1.5` | Seconds between requests |
| `BARRISTER_CACHE_TTL` | `21600` | Response cache lifetime |
| `TELEGRAM_BOT_TOKEN` | *(unset)* | Enables Telegram delivery |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | *(unset)* | Drafting |

## Tests

```bash
python3 -m pytest
```

309 tests, no network access required — they run against real pages saved in
`tests/fixtures/`, so a change to a parser is caught against the markup the
Court actually serves. The suite is weighted towards where being wrong is
expensive: matching (a false negative is a missed hearing), limitation (the
tests assert the *working*, not just the answer), and the drafting citation
guard.

## What is deliberately absent

No case-law search, no precedent bank, no generative answers about Bangladeshi
judgments. That is Tier 2, and it is gated on solving citation accuracy — see
[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md).
