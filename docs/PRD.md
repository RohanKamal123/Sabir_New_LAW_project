# Product Requirements Document

**Product:** Barrister Tools — practitioner software for the Supreme Court of Bangladesh
**Status:** Tier 0 and Tier 1 built; Tier 2 not started
**Owner:** Solo developer (AI-assisted), building first for own practice
**Last updated:** 2026-08-24

---

## 1. Summary

A single product that handles the repetitive daily work of a Supreme Court of
Bangladesh barrister — cause-list watching, case tracking, statute lookup,
document drafting, limitation calculation and case-file management — built on the
public data the Court and the Ministry of Law already publish but do not deliver.

It exists because that data is **published but not pushed**: the cause list
appears on a government website the evening before hearings, split across ~60
bench pages, with no API, no search, and no notification. Every litigator in
Dhaka currently finds out what they are listed for by going and looking. This
product looks for them.

The distinguishing design commitment is **restraint about AI**. The features
that can be wrong in ways that lose a client's cause of action or a
practitioner's trust — limitation periods, case-law citations — are rule-based
or refuse to answer, never generated. Generation is confined to document
narrative, behind a citation guard.

---

## 2. Problem & context

### 2.1 The market

- **No official API from the Supreme Court.** Cause lists and case status
  (`supremecourt.gov.bd`) are plain HTML/PDF, published per division and bench.
  Scraping is the only integration path, and it is viable — a solo-built app
  already does the cause-list piece.
- **Statute text is free and structured** at `bdlaws.minlaw.gov.bd` — the
  official Bangladesh Code, every act, no paywall. A zero-hallucination-risk
  source: exact-text lookup, not synthesis.
- **Case-law reporters (DLR, BLD, BLC, BLT, MLR) are paywalled** with no public
  API. The one genuinely hard data problem, and where global tools with licensed
  corpora still hallucinate 17–33% of the time.
- **The nearest competitor (FindmyAdvocate) is client-to-lawyer matching**, not
  a practitioner tool. The practitioner-productivity niche is open.

### 2.2 The user

A practising barrister of the Supreme Court of Bangladesh, litigating across the
High Court Division and the Appellate Division, running their own docket. First
user is the author; intended users are other barristers and small chambers.

### 2.3 Jobs to be done

1. "Tell me the evening before if I'm listed tomorrow, and where."
2. "Tell me when something changes on a case I'm watching."
3. "Give me the exact text of a section, right now, that I can rely on."
4. "Draft the skeleton of a petition from facts I paste, without inventing law."
5. "Compute this filing deadline correctly, and show me the working."
6. "Keep my files — clients, cases, notes, time, deadlines — in one place."

---

## 3. Goals & non-goals

### 3.1 Goals

- Replace the nightly manual cause-list read with a single push notification
  that names the barrister's own files.
- Make correctness-critical outputs (limitation, statute text) auditable and
  never generated.
- Run cheaply and privately on one machine a solo practitioner controls.
- Meet the barrister where they already are — a messaging app — not only in a
  dashboard.

### 3.2 Non-goals (deliberate)

- **No generated case-law answers.** Deferred until citation accuracy can be
  solved; shipping it wrong costs more trust than not having it.
- **No client-facing or lawyer-marketing features.** Keeps clear of Bar Council
  solicitation rules until they are checked.
- **No multi-tenant SaaS.** Single-tenant; per-user schema throughout so this
  can change later without a migration.
- **No mobile app.** The Telegram bot is the mobile surface.

---

## 4. Principles

1. **Rule-based where correctness matters.** Limitation periods and deadlines
   are encoded rules with citations, not model output.
2. **Retrieve, don't generate, for statute.** Every statute result is a verbatim
   span with its source URL.
3. **Refuse rather than guess.** An unverified rule, an unsupplied citation, a
   missing fact, a matter of judgment — each produces a marked refusal, not a
   confident answer.
4. **One core, many surfaces.** Matching, diffing and limitation logic exist
   once; CLI, API, web and bot are thin.
5. **Be a good citizen of the sources.** One request at a time, rate-limited,
   cached, honestly identified. The product's survival depends on continued
   access.
6. **Design for the desk, not the demo.** The visual reference is the printed
   cause list and the law report; it must print and it must be scannable.

---

## 5. Scope — what is built

### 5.1 Cause-list watch & alerts *(Roadmap #1 — built)*

**Requirements**
- Scrape the daily cause list for both divisions and every sitting bench.
- Let a barrister register watch terms: their name (advocate), a party, or a
  case number.
- Match generously — fold honorifics, spacing and initials; accept case-number
  shorthand — because a false negative is a missed hearing.
- Deliver one grouped notification per barrister per day, naming the file where
  the case belongs to one.
- Be idempotent: re-running the sweep never re-sends a delivered alert;
  undelivered alerts are retried.

**Acceptance**
- A watch on "Abu Hanif" matches a listing of "Mr. Md. Abu Hanif".
- A barrister with two listed matters gets one message, not two.
- Running the sweep twice produces one alert.

### 5.2 Case status tracker *(Roadmap #2 — built)*

**Requirements**
- Look up a case by human-readable type name, number and year (110-type registry
  shipped offline).
- Parse the full hearing history, including each hearing's result.
- Detect and alert on three changes only: a new listing, a result recorded where
  there was none, a result amended — comparing parsed values, not page bytes.

**Acceptance**
- "Writ Petition" resolves without the user knowing the internal type id.
- A whitespace-only change in the page produces no alert.
- A newly recorded result produces exactly one alert describing it.

### 5.3 Statute lookup *(Roadmap #3 — built)*

**Requirements**
- Scrape acts and sections from the Bangladesh Code (UTF-16 handled).
- Store in SQLite FTS5; search by keyword or by citation.
- A citation-shaped query resolves by exact section lookup first.
- Every result is verbatim, with its source URL. No generation anywhere in the
  path.

**Acceptance**
- "s. 5 of the Limitation Act" returns section 5, not the sections that mention
  it.
- The returned body byte-matches the official page.

### 5.4 Template-based drafting *(Roadmap #4 — built)*

**Requirements**
- Templates own document structure (cause title, SHEWETH, numbered grounds,
  Roman-numeral prayer, closing); the model writes only narrative.
- Support two providers (Claude via SDK; DeepSeek via OpenAI-compatible HTTP),
  auto-selected from configured keys; render the skeleton with neither.
- Forbid citing any authority not supplied; flag any law-report citation that
  appears unsupplied.
- Mark missing facts `[TO BE SUPPLIED]` rather than inventing them.

**Acceptance**
- With no key, a full petition renders with the narrative as a marked gap.
- A citation not passed as an authority is flagged in the output warnings.

### 5.5 Limitation calculator *(Roadmap #5 — built)*

**Requirements**
- Encode Limitation Act 1908 ss. 4, 12(1), 12(2), 12(3) as date arithmetic.
- Show the working: each step, each exclusion, with the statutory text cited.
- Never compute s. 5 condonation (a matter of judgment); flag it instead.
- Treat the First Schedule's articles as **unverified** (machine-extracted from
  a PDF) and refuse to compute from one until a lawyer marks it verified.

**Acceptance**
- An appeal deadline correctly adds back certified-copy time and rolls a
  deadline off a closed day (s. 4).
- Requesting a Schedule article raises until it is verified.
- The output quotes the section it relied on.

### 5.6 Matter/case-file management *(Roadmap #6 — built)*

**Requirements**
- Clients, files (with chamber references), attached cases, notes, documents,
  time entries and deadlines.
- Linking a case to a file starts its cause-list watch, so alerts name the file.
- A practice-level summary and a cross-file diary.

**Acceptance**
- Opening a file and attaching a case creates a watch automatically.
- A listed case maps back to its file reference on the day view and in alerts.

### 5.7 Messaging-app interface *(Roadmap #7 — built)*

**Requirements**
- A Telegram bot over long polling (no public endpoint required).
- Commands for the day, the diary, files, watching, lookup, and quick file entry
  (note, time).
- All logic delegated to services; formatting and routing only.

**Acceptance**
- `/today` returns the day's listings, naming files.
- `/time MAT-… 90 …` logs time and confirms the new total.

### 5.8 Surfaces

- **CLI** — cron (`sweep`, `status --track-for`) and interactive.
- **JSON API** — every capability, for integrations.
- **Web UI** — server-rendered, court-stationery design, prints correctly.
- **Telegram bot** — the mobile surface.

---

## 6. Out of scope — Tier 2 *(not started)*

Grounded case-law RAG, verified precedent bank, e-filing integration, chamber
collaboration. All gated on solving citation accuracy, which is the core
engineering problem and not attempted yet.

---

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| **Politeness** | Serial requests only; ≥1.5s between them; on-disk cache; retry only transient failures; contact address in User-Agent. A full sweep is ~65 requests over ~90s, once a day. |
| **Correctness** | Limitation and statute outputs auditable to source; unverified rules refused. |
| **Privacy** | Single SQLite file on the user's own machine; nothing sent to third parties except the chosen drafting model and Telegram. |
| **Offline** | Everything except live scraping works without network; the test suite (300+) needs none. |
| **Portability** | One file to back up; runs on a cheap VPS or a laptop. |
| **Print** | The cause list and file views print black-on-white with correct page breaks. |
| **Accessibility** | Semantic HTML, visible focus states, legible in both light and dark themes, no reliance on colour alone for status. |
| **Upgrade** | Schema self-heals on connection (new tables and additive columns); no destructive migrations. |

---

## 8. Data sources & dependencies

| Source | Use | Nature | Risk |
|---|---|---|---|
| `supremecourt.gov.bd` | Cause lists, case status | HTML, JS-form endpoints, no API, no robots.txt | Markup changes; access could be restricted |
| `bdlaws.minlaw.gov.bd` | Statute text | UTF-16 HTML, PDF schedules | Stable; amendments require re-sync |
| Anthropic / DeepSeek | Drafting narrative only | API | Optional; product works without |
| Telegram Bot API | Notifications & bot | API | Optional |

---

## 9. Risks & open items

| Risk | Severity | Status / mitigation |
|---|---|---|
| **Limitation Schedule articles are machine-extracted and unverified** | High | Calculator refuses them until a lawyer verifies; `review-queue` lists the 93 outstanding. **Open — needs a lawyer.** |
| **Terms of use not read on either source site** | High | robots.txt confirmed absent (both 404); politeness enforced in code. **Open — human must read ToS and email Court IT before scheduled production use.** |
| **Bar Council solicitation rules unchecked** | Medium | Current scope is inward-facing only. **Open — must be checked before any client-facing or marketing feature, i.e. before selling to other chambers.** |
| Court changes its HTML | Medium | Parsers tested against saved fixtures; a break is caught against real markup and localised to one parser. |
| Access blocked for over-crawling | Medium | Rate-limited, cached, identified; guidance to slow down and contact rather than retry harder. |
| Drafting model hallucinates a citation | Medium | System prompt forbids it; `check_citations()` flags any that appear. |
| Web UI has no auth | Low (by design) | Single-tenant; bind to localhost or an authenticating proxy. Per-user schema allows adding auth later without migration. |

---

## 10. Success measures

Because the first user is the author, early success is qualitative and personal
before it is a metric:

- **Adoption of the wedge:** the nightly cause-list read is fully replaced by the
  alert — the barrister stops opening the Court site to check.
- **Zero missed listings** attributable to a false negative in matching.
- **Trust preserved:** no instance of the product producing a confidently wrong
  limitation date or an invented citation that reached a filing.
- **Daily habit:** the diary and file views are opened without prompting, which
  is what converts a free tool into something worth paying for.
- Later, with other users: retention through the deadline calculator and
  case-file management (Tier 1), which is where free users become paying ones.

---

## 11. Sequencing (delivered)

1. **Tier 0 as one lean product, free** — cause-list alerts as the wedge, then
   status, statute, drafting. *Done.*
2. **Tier 1** — limitation calculator, matter management, messaging interface.
   *Done.*
3. **Tier 2** — grounded case-law, precedent bank, e-filing, collaboration.
   *Deferred, gated on citation accuracy.*

See [`ROADMAP.md`](ROADMAP.md) for the original plan and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how it is built.
