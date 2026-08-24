# Design system

## The reference

Not a SaaS dashboard. The two documents a Supreme Court barrister already reads
every working day are **the printed cause list** and **the law report**. Both
are dense, ruled, typographically quiet, and built to be scanned in a hurry by
someone who knows exactly what they are looking for.

Designing away from that would make the product feel like software about their
work rather than an extension of it — and would make the cause-list screen
harder to check against the Court's own printed list, which is the single thing
it exists to do.

Everything below follows from that one decision.

## Rules

### 1. Rules, not cards

Information is separated by hairlines, the way a cause list separates benches.
No drop shadows, no floating panels, no `border-radius` beyond 2px on form
controls. A card says "this is a discrete object in an app"; a rule says "this is
the next entry on the list", which is what it actually is.

### 2. Serif to read, mono for data

Body text is serif. Case numbers, dates, serials, minutes and money are mono
with `font-variant-numeric: tabular-nums`, so columns align down the page and a
barrister can run a finger down a serial column the way they do on paper.

`tabular-nums` is set on `body`, not per-component. There is no context in this
product where proportional figures are wanted.

### 3. One accent, and it means one thing

Oxblood (`--oxblood`) means **urgent**: overdue, due within a week, an
unverified rule, a citation that was not supplied, a refusal. Nothing else is
coloured. If everything is coloured, nothing is — and this product's whole
credibility rests on the barrister believing the red things when they appear.

Status is carried by a word plus a small mark (`● OPEN`, `◐ RESERVED`,
`○ CLOSED`), not by a coloured pill. Green and amber exist only as glyph colours
for those marks, never as fills.

### 4. Margin rules, not highlighted rows

A listing that belongs to one of the barrister's files gets a 3px oxblood rule
in the left margin — the way you would mark your own matters on a printed list
with a pen. Highlighting the whole row would fight the surrounding text; a
margin rule is visible peripherally and invisible when you are reading.

### 5. Paper, not screen

Background is warm off-white (`#f6f3ec`), ink is warm near-black (`#22201c`).
Dark mode is lamplight on paper — a warm dark brown-grey, ink becomes parchment
— not a black void with white text.

Dark mode is implemented on `prefers-color-scheme` with a `[data-theme]`
override, and every colour has its base definition on bare `:root`, so no token
is defined only inside a media query.

### 6. It must print

Barristers print things and take them to court. `@media print` drops the
navigation, forms and buttons; flattens the palette to black on white; removes
link underlines and URL colouring; and sets `page-break-inside: avoid` on
entries, diary items and table rows so a matter never splits across a page.

The printed cause list page is meant to be a usable substitute for the Court's
own printout.

## Tokens

| Token | Light | Role |
|---|---|---|
| `--paper` | `#f6f3ec` | Page ground |
| `--paper-raised` | `#fbf9f4` | Masthead, inputs, hover |
| `--paper-sunk` | `#efeade` | Quoted authority, quiet caveats |
| `--ink` | `#22201c` | Body text |
| `--ink-muted` | `#5f5a51` | Parties, secondary detail |
| `--ink-faint` | `#8b8478` | Rubrics, labels, provenance |
| `--rule` | `#d6cfc0` | Hairline between entries |
| `--rule-heavy` | `#2a2722` | Section and table heads |
| `--oxblood` | `#8a2b2b` | Urgency. Only urgency. |
| `--ink-blue` | `#1f3f66` | Links |

Type: `--serif` (Georgia / Times, with `Noto Serif Bengali` for Bengali
jurisdiction text), `--mono` (system mono), `--sans` (Helvetica/Arial, used only
for small-caps labels).

No web fonts. The app is meant to work in chambers with poor connectivity, and a
font that fails to load is worse than one that was never asked for.

## Components

| Class | What it is |
|---|---|
| `.masthead` | Court-document head: title, double rule, date and barrister |
| `.nav` | Folder tabs — current page marked by an oxblood underline |
| `.rubric` | Small-caps section label with a rule, the law report's rubric |
| `.listing` / `.entry` | The cause list. Serial in a fixed gutter, parties beneath |
| `.entry--connected` | Indented, prefixed "with" — matters heard together |
| `.entry--mine` | Margin rule: one of the barrister's own files |
| `.section-head` | `For Hearing`, `For Judgment` — indented to the case column |
| `.ledger` | Tabular data: files, time, documents |
| `.state` | Status as a marked word |
| `.diary` / `.diary__item--urgent` | Deadlines; urgent ones take the margin rule |
| `.figures` / `.figure` | Counts, divided by rules rather than boxed |
| `.provision` | Statute text set like a law report, with its source URL |
| `.working` | The limitation computation, set like a clerk's marginal working |
| `.authority` | A quoted statutory provision |
| `.caveat` | A warning that must be read; `.caveat--quiet` for context |

## The caveat component

Worth its own note, because it carries most of the product's honesty.

Every place where this software could be confidently wrong renders a `.caveat`:
an unverified limitation article, a citation the barrister did not supply, a
deadline whose basis has not been checked, a statute result reminding you it was
retrieved rather than generated.

They are styled to be read — oxblood margin rule, tinted ground, a small-caps
label — not to be dismissed. `.caveat--quiet` is for context that is genuinely
informational (the corpus is empty; no model is configured) and drops the
accent so the loud ones keep their meaning.

## What was deliberately not done

- **No icon set.** Icons here would be decoration; the words are shorter than
  the icons would be legible.
- **No animation.** Nothing in a cause list moves.
- **No charts.** There is no question in this product a chart answers better
  than a number.
- **No brand colour beyond the accent.** The product should look like the
  barrister's own stationery, not like a vendor's.
- **No component framework.** The whole system is 700 lines of hand-written CSS
  with no build step, which is proportionate to a server-rendered app of this
  size and means a future maintainer can read all of it in one sitting.
