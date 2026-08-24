# Compliance notes

The roadmap flags two checks to run *before* building. Here is where each stands,
what was actually verified, and what a human still has to do.

## 1. Automated access to the two upstream sites

**Checked on 2026-08-24.** Neither site publishes a `robots.txt`:

| URL | Result |
|---|---|
| `https://www.supremecourt.gov.bd/robots.txt` | `404` |
| `http://bdlaws.minlaw.gov.bd/robots.txt` | `404` (an HTML 404 page, served `200` by the app's error handler) |

So there is **no machine-readable crawl directive to obey or violate** on either
host. That is not the same as permission, and the absence of a `robots.txt` is
not a defence — it just means the constraint has to be self-imposed.

### What the code does about it

`barrister/http.py` is built to make this crawler the least interesting traffic
in either site's logs:

- **Serial requests only.** No concurrency anywhere in the codebase.
- **A floor on request spacing** — `BARRISTER_REQUEST_DELAY`, default 1.5s.
- **On-disk response cache** (`BARRISTER_CACHE_TTL`, default 6h), so a re-run of
  the nightly sweep costs zero requests.
- **Retry only on transient failures** (429, 5xx, connection errors) with
  exponential backoff. A `404` is treated as an answer, not something to hammer.
- **An honest `User-Agent`.** Set `BARRISTER_CONTACT_EMAIL` and it becomes
  `BarristerTools/0.1 (+mailto:you@chambers.test)`. **Do set it.** A court IT
  team that can email you does not have to block you.

### Volume, concretely

A full daily sweep is one bench-list page per division plus one page per sitting
bench — around 60 requests for the High Court Division on a normal day, once a
day, at 1.5s apart. That is roughly what one person browsing the cause list
generates, spread over 90 seconds.

### Still to do — by a human

- [ ] **Read the terms of use / any acceptable-use notice on both sites.** A 404
      on `robots.txt` says nothing about published terms. This has not been done.
- [ ] **Email the Supreme Court's IT contact** describing the tool and the
      request volume before running the sweep against production on a schedule.
      Asking first is cheap; being blocked after the fact is not.
- [ ] Re-check both `robots.txt` URLs periodically — a file appearing later
      changes the answer, and nothing in this codebase watches for that.

## 2. Bangladesh Bar Council rules

**Not checked — this needs a lawyer, not a scraper.**

The roadmap's concern is solicitation and advertising rules, which bite on
anything resembling lawyer marketing or client-matching (FindmyAdvocate's lane).

Where this build currently sits: every feature here is **inward-facing practice
software** — a barrister's own cause list, their own cases, statute text, their
own drafts. Nothing in this codebase is client-facing, nothing matches clients to
lawyers, and nothing publishes a barrister's details anywhere.

That keeps the current scope clear of the obvious problem, but it is a
description of the code, not a legal opinion.

- [ ] **Get the Bar Council rules on solicitation/advertising read** before any
      feature that lists barristers publicly, matches clients to counsel, or
      markets a barrister's services — i.e. before this becomes a product sold to
      other chambers rather than a tool used inside one.

## 3. What this software does not do, deliberately

- **It does not answer with case law.** No feature retrieves, summarises or cites
  a Bangladeshi judgment. Case-law reporters (DLR, BLD, BLC, BLT, MLR) are
  paywalled, and the tools that *do* have licensed corpora still hallucinate
  citations at 17–33%. Tier 2 is where that problem gets solved properly, or not
  at all.
- **The drafting feature refuses to cite.** The system prompt forbids citing any
  authority the barrister did not supply, and `check_citations()` flags any law
  report reference that appears in output without having been supplied. See
  `barrister/services/drafting.py`.
- **The limitation calculator refuses unverified rules.** Schedule articles were
  machine-extracted from a PDF table and every one is `"verified": false` until a
  lawyer checks it. `deadline_for_article()` raises rather than answer from one.
  See `barrister review-queue`.
- **Statute lookup never generates.** Results are verbatim spans of the official
  Bangladesh Code with the source URL attached.
