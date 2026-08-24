# AI Software for Bangladeshi Supreme Court Barristers — Build Roadmap

**Premise:** solo build (one developer, AI-assisted coding), intended as a product for other
barristers too, covering case tracking, research, and drafting roughly equally.

---

## The landscape you're building into

- **No official API from the Supreme Court of Bangladesh.** Cause lists and case-status search
  (`supremecourt.gov.bd`) are plain HTML/PDF, published the evening before hearings, by division
  (Appellate / High Court Division) and bench. That's scrapable, and someone already has: a
  solo-built app (`cause-list.vercel.app`) does exactly this today, which is proof that a single
  developer can pull this off cheaply.
  - [Cause List — TNP Legal](https://tnp.legal/cause-list-supreme-court-of-bangladesh)
  - [Supreme Court case search](https://www.supremecourt.gov.bd/web/?page=case_search.php&menu=11)
  - [BD Cause List](https://cause-list.vercel.app/)

- **Statute text is free and structured.** `bdlaws.minlaw.gov.bd` (the official Bangladesh Code)
  hosts every act with per-act pages and chronological/alphabetical indexes — public, no paywall.
  A low-risk, zero-hallucination-risk data source: exact-text lookup, not generative synthesis.
  - [bdlaws.minlaw.gov.bd](http://bdlaws.minlaw.gov.bd/)

- **Case law reporters (DLR, BLD, BLC, BLT, MLR) sit behind paid databases** (BDLEX,
  bldlegalized.com) with no public API. This is the one genuinely hard data problem, and it's
  exactly where the big global players (Lexis, Westlaw) still get 17–33% hallucination rates even
  with licensed corpora. Don't underestimate this piece.
  - [BDLEX](https://www.bdlex.com/)
  - [Digital Bangladesh Legal Decisions](https://www.bldlegalized.com/)

- **Nearest existing competitor, FindmyAdvocate, is a client-to-lawyer matching platform**, not a
  practitioner productivity tool — it solves "find a lawyer," not "help a barrister run their
  Supreme Court practice." The practitioner-tool niche looks genuinely open right now.
  - [FindmyAdvocate — Prothom Alo](https://en.prothomalo.com/science-technology/vprxf4ttdg)

---

## Tier 0 — Ship now (days, minimal effort)

Public, scrapable data and thin LLM wrapping only. No licensed data, no RAG pipeline, no
legal-content curation required yet.

1. **Cause list watch + alerts.** Scrape the daily cause list; let a barrister register their
   name/chamber and case numbers; push a notification (Telegram bot or WhatsApp) the evening the
   list drops if they're on it — bench, serial number, case type. The single most repetitive daily
   task for any litigator, the cheapest thing to build (cron job + diff + notify), and the best
   acquisition hook: free, obviously useful, no trust required to "just work." **Build this first.**

2. **Case status tracker.** Wrap the official case-number/party-name search in a clean UI; let
   users "watch" specific cases and get notified on status changes (new order, adjournment,
   disposal) via periodic re-scrape and diff. Pairs naturally with #1 — same scraping
   infrastructure.

3. **Statute lookup.** Fast search over `bdlaws.minlaw.gov.bd` content — exact section text, no AI
   generation involved, so no accuracy risk. Cheap to scrape once (finite, stable corpus) and
   re-sync occasionally for amendments. Gives a "research" feature on day one without touching the
   hard case-law problem.

4. **Template-based AI drafting.** Structured templates for the documents barristers actually file
   — leave-to-appeal petitions, writ petitions, applications — where the AI fills in facts the user
   pastes in, using a system prompt built from Bangladeshi drafting conventions. This is "prompt a
   capable LLM well and export to Word/PDF," not a legal-reasoning system.

**Effort:** a small number of dev-days each — none require a licensed dataset, ML training, or a
verified case-law corpus. **Ship these as one product (not four):** #1 and #2 share scraping
infrastructure; #3 and #4 share the same drafting UI.

---

## Tier 1 — Weeks of effort

5. **Limitation/deadline calculator.** Rule-based, not AI — encode Limitation Act 1908 periods and
   Supreme Court rules so entering a judgment/order date auto-computes filing deadlines, tied into
   the case tracker. The effort is knowledge-encoding (getting the rules right, verifiable only by
   a lawyer), not engineering. High trust value if correct.

6. **Matter/case-file management.** Client info, documents, notes, time tracking per matter — a
   lightweight "Clio for BD barristers." Standard CRUD web-app work, no AI required, but it's what
   turns single-use tools into a daily habit and a recurring fee.

7. **Messaging-app interface.** Move Tier 0 alerts (and eventually drafting/research queries) into
   a WhatsApp or Telegram bot rather than a separate dashboard. Tools embedded in an already-open
   surface get far more daily reliance than standalone apps; for Bangladeshi practitioners, a
   messaging app is likely that surface. Worth the build once Tier 0 has users.

---

## Tier 2 — Months of effort (the real moat, and the real risk)

8. **Grounded case-law research (RAG over judgments).** Build a corpus by scraping the Supreme
   Court's own judgments/orders sections plus whatever open sources exist (some judgments circulate
   via sites like lawcastlebd), OCR the older PDFs, then build retrieval so answers cite real,
   checkable Bangladeshi case law instead of an LLM's memory. Global legal AI tools still fail
   17–33% of the time even with licensed, curated data — treat **citation accuracy as the core
   engineering problem**, not a nice-to-have. Shipping this before it's reliable loses the exact
   trust that makes barristers come back.

9. **Verified precedent bank / playbooks.** Chamber-specific templates and clause libraries curated
   from real filings. Valuable, but requires content curation (your own and other barristers'
   precedents), not just code.

10. **E-filing / court-system integration.** Bangladesh's judiciary is only in early stages of
    e-filing/digital case management ("E-Judiciary expansion underway") — a bet on future court
    infrastructure you don't control, not something to plan a near-term roadmap around.
    - [E-Judiciary expansion — BSS News](https://www.bssnews.net/special-stories/384287)

11. **Firm/chamber collaboration.** Conflict checking across a chamber's docket, shared calendars
    synced to real cause-list changes, spend tracking. Valuable once there are multiple paying
    chambers, not before.

---

## Two things to check before you build

- **Scraping terms of use.** Confirm `supremecourt.gov.bd` and `bdlaws.minlaw.gov.bd` don't
  prohibit automated access in their terms — being the tool that gets a legal product blocked for a
  ToS violation would be an ironic own-goal.
- **Bar Council rules.** Since other barristers are intended users (not just yourself), check
  Bangladesh Bar Council rules on solicitation/advertising before building anything that looks like
  lawyer marketing or client-matching (FindmyAdvocate's lane, which may carry different regulatory
  obligations than a practitioner productivity tool).

---

## Suggested sequencing

Ship **Tier 0 as one lean product, free**, and use it to get real barristers using it daily —
cause-list alerts are the low-friction wedge that gets adoption without asking anyone to trust an
AI yet. Layer in the **deadline calculator and case-file management (Tier 1)** once there are
users, since that's what converts free users into paying ones. Only invest in the **grounded
case-law RAG system (Tier 2)** once real time can be dedicated to getting citation accuracy right —
shipping it early and getting it wrong costs more trust than not having it at all.
