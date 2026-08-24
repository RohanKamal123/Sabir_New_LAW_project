# Project context

AI software for **Bangladeshi Supreme Court barristers** — a practitioner productivity tool
covering case tracking, legal research, and drafting in roughly equal measure.

- **Build model:** solo developer, AI-assisted coding.
- **Audience:** the author's own practice first, then other barristers as a product.
- **Full plan:** [`docs/ROADMAP.md`](docs/ROADMAP.md) — read this before proposing architecture or
  scoping features.

## Key constraints to keep in mind

- **No official Supreme Court API.** Cause lists and case-status search on `supremecourt.gov.bd`
  are plain HTML/PDF, published the evening before hearings, split by division and bench. Scraping
  + diffing is the integration path.
- **Statute text is free and structured** at `bdlaws.minlaw.gov.bd` (official Bangladesh Code).
  Treat it as an exact-text lookup corpus — no generative synthesis, so no hallucination risk.
- **Case law reporters (DLR, BLD, BLC, BLT, MLR) are paywalled** with no public API. This is the
  hard data problem and is deliberately deferred to Tier 2.
- **Citation accuracy is the core engineering problem** for anything that answers with case law.
  Global tools with licensed corpora still hallucinate 17–33% of the time. Unreliable citations are
  the fastest way to lose practitioner trust — don't ship generative case-law answers ungrounded.
- **Rule-based where correctness matters.** Limitation periods and filing deadlines are encoded
  rules, not LLM output.

## Sequencing

1. **Tier 0 (days):** cause-list alerts → case-status tracker → statute lookup → template-based
   AI drafting. Ship as **one** product; #1/#2 share scraping infrastructure, #3/#4 share the
   drafting UI. Cause-list alerts are the wedge — build first.
2. **Tier 1 (weeks):** limitation/deadline calculator, matter management, messaging-app (Telegram/
   WhatsApp) interface.
3. **Tier 2 (months):** grounded case-law RAG, precedent bank, e-filing integration, chamber
   collaboration.

## Before building

- Verify `supremecourt.gov.bd` and `bdlaws.minlaw.gov.bd` terms of use permit automated access.
- Check Bangladesh Bar Council rules on solicitation/advertising before any feature that resembles
  lawyer marketing or client-matching.
