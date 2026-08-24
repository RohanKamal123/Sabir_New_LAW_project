# User journey

**[The Night Before](https://claude.ai/code/artifact/737a3fdb-fc0c-4b07-a262-e3c497db31e8)** —
published artifact. Source: [`journey/the-night-before.html`](journey/the-night-before.html).

A Bangladeshi Supreme Court barrister's week, hour by hour, from the moment the
cause list drops on Thursday evening to a result appearing on a case he had
stopped thinking about the following Friday. Each moment shows what he does,
what the software does while he is not looking, and — where it helps — the
actual screen.

## What it covers

| Section | Question it answers |
|---|---|
| **The day it replaces** | What the four repetitive tasks are, and why the Court's publishing model creates them |
| **Thursday evening into Sunday** | Eleven time-stamped beats through the week, with screen specimens |
| **One core, four ways in** | Why a CLI, an API, a web UI and a bot never disagree with each other |
| **Where it says no** | The four places the product deliberately refuses to answer, and why each refusal is load-bearing |
| **What is still open** | The seams — work a human still has to do before this is finished |

## The beats

| Time | Actor | What happens |
|---|---|---|
| Thu 20:30 | System | The list drops; the sweep walks ~65 pages at 1.5s apart |
| Thu 20:31 | System | Two matters match; both belong to open files |
| Thu 20:32 | Sabir | One Telegram message for the whole day, naming the files |
| Sun 07:10 | Sabir | The day view over breakfast |
| Sun 07:40 | Sabir | The diary catches a reply affidavit two days overdue |
| Sun 08:15 | Sabir | Section 5, verbatim, with its source URL |
| Sun 08:40 | Sabir | A limitation deadline, with the copy-time exclusion shown |
| Sun 09:20 | Sabir | Drafts the condonation application; a citation is flagged |
| Sun 10:30 | Sabir | Prints the bench list and takes it to court |
| Sun 16:05 | Sabir | Logs time and a note from the Telegram thread |
| Fri 06:00 | System | A result appears: "Adjourned till 07.09.2026" |

## Design note

The journey document has a deliberately different visual identity from the
product it describes — cool slate ground, brass time-spine, Archivo/Spectral
typography — so that the product's own screens can appear inside it as framed
**specimens** in their warm-paper, serif language. The shift between the two is
the point: the document is examining the product, not imitating it.

The product's own design system is documented separately in
[`DESIGN.md`](DESIGN.md).
