# Running it, and testing it by hand

Two ways to try it: with **seeded demo data** (no network, works offline — start
here) or **live** against the Court's website. Both use the same commands; they
differ only in whether the database already has data in it.

## 0. One-time setup

```bash
cd Sabir_New_LAW_project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The demo writes to its own `.demodata` directory in the repo, so it never
touches your real `~/.barrister` install. When you drive the CLI against the
demo (rather than `run_demo.py`), point it at that dir:

```bash
export BARRISTER_DATA_DIR=$(pwd)/.demodata
```

---

## 1. The fastest path — seed, then open the web UI

```bash
python3 tools/seed_demo.py --reset      # builds <repo>/.demodata
python3 tools/run_demo.py               # serves that data dir on :8000
```

`run_demo.py` pins `BARRISTER_DATA_DIR` to the same `.demodata` directory the
seed wrote, so you don't have to remember to set it. Open
**http://127.0.0.1:8000**.

You are logged in as a demo barrister, "Sabir Rahman", with three files, two of
them listed today, a synced statute, and deadlines including one overdue. The
seed builds everything from the committed test fixtures, so it needs no internet
and gives you the same data the screenshots were taken from.

If you prefer to run uvicorn yourself, point it at the same dir:

```bash
export BARRISTER_DATA_DIR=$(pwd)/.demodata
uvicorn barrister.api:app --port 8000
```

To keep the demo out of the repo tree entirely, pass `--data-dir`:
`python3 tools/seed_demo.py --reset --data-dir /tmp/barrister-demo`.

### What to click, and what you should see

| Page | What to look for |
|---|---|
| **Today** (`/`) | Four figures across the top; two listings under "Annex Building Court No. 18", each with a `MAT-…` file reference in oxblood; a "Falling due" list with one item marked overdue in red. |
| **Cause list** (`/cause-list`) | Pick a bench from the dropdown → **this fetches live from the Court** (see §3). With no bench chosen it just prompts you. |
| **Files** (`/matters`) | Three files. Status shown as a marked word (`● open`, `◐ reserved`), not a coloured pill. Click a reference. |
| **A file** (`/matters/1`) | Cases, a deadline, attendance notes, a document, and a time ledger totalling 3.25h. |
| **Diary** (`/diary`) | Deadlines across all files; the overdue one takes a red margin rule; one shows "basis not verified". |
| **Statutes** (`/statutes`) | Search `s. 5 of the Limitation Act` → the full verbatim text of section 5 with its source URL. Search `sufficient cause` → same section by keyword. |
| **Limitation** (`/limitation`) | Fill: start `2026-01-15`, proceeding *Appeal*, period `90`, copy applied `2026-01-20`, copy ready `2026-02-03` → deadline **2026-04-30**, with the four-step working and the s.12(2) text quoted. |
| **Drafting** (`/drafting`) | Fill in a petitioner, respondent, some facts and a prayer → a full writ petition renders with the narrative marked as a gap (no model configured — see §4). |

**Check dark mode:** your OS dark setting, or add `?` and toggle your browser's
theme — the whole thing should become lamplight-on-paper, never white text on a
white card.

**Check printing:** on the cause list or a file, `Ctrl/Cmd-P` — navigation and
buttons drop away, everything goes black-on-white, and no matter splits across
a page.

---

## 2. Testing from the command line (no browser)

Every feature is also a CLI command. Against the seeded database:

```bash
# See the demo barrister's watches and files
barrister watches 1
barrister matter list 1
barrister matter show 1 MAT-2026-001

# The diary
barrister diary 1

# Statute search (offline — uses what the seed synced)
barrister statutes search "s. 5 of the Limitation Act"

# Limitation, with its full working
barrister limitation --from 2026-01-15 --days 90 --proceeding appeal \
    --copy-applied 2026-01-20 --copy-ready 2026-02-03

# Log time and a note, then look again
barrister matter time 1 MAT-2026-001 30 "Reading in" --rate 5000
barrister matter note 1 MAT-2026-001 "Client called re next date"
barrister matter show 1 MAT-2026-001

# Try the drafting scaffold (no model needed)
barrister draft writ_petition \
    --petitioner "Md. Karim Uddin of Dhaka" \
    --respondent "Bangladesh, represented by the Secretary, Ministry of Land" \
    --subject "Challenge to the acquisition notice dated 12.03.2026" \
    --facts "The petitioner owns 3 katha at Mirpur. No hearing was given." \
    --prayer "Issue a Rule Nisi calling upon the respondents to show cause"

# The limitation review queue — the articles still awaiting a lawyer's check
barrister review-queue --limit 10
```

To start from an empty database instead and build it up yourself:

```bash
export BARRISTER_DATA_DIR=/tmp/barrister-fresh
barrister adduser "Your Name" --telegram 123456789
barrister watch 1 advocate "Your Name"
barrister matter open 1 "Test v Someone" --case "First Appeal 226 2013"
```

---

## 3. Testing live against the Court's website

These commands actually fetch `supremecourt.gov.bd`. They are slow on purpose
(one request at a time, 1.5s apart) and depend on the site being up. **Set a
contact address first** — it goes in the request's User-Agent:

```bash
export BARRISTER_CONTACT_EMAIL="you@example.com"

# Today's cause list for one High Court bench
barrister causelist --division 2 --limit 1

# A real case's status and full hearing history
barrister status "First Appeal" 226 2013

# The full nightly sweep, printing alerts instead of sending them
barrister sweep --dry-run
```

To confirm the web UI's live path, open `/cause-list`, choose a division and a
bench, and press **Show** — that page fetches on demand.

To build the statute corpus for real (instead of the seeded slice):

```bash
barrister statutes sync --act 88          # The Limitation Act, 1908
barrister statutes search "extension of period"
```

**If a request is refused or times out:** that is the politeness layer or the
site, not a crash. Raise `BARRISTER_REQUEST_DELAY`, confirm your contact email
is set, and try one bench rather than a full sweep.

---

## 4. Testing the drafting model (optional)

Without a key, drafting renders the template with the narrative as a marked gap
— which is itself worth seeing. To test real generation, set **either**:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # Claude, the default
# or
export DEEPSEEK_API_KEY="sk-..."           # DeepSeek
```

`/health` (or `barrister`'s startup) reports which provider resolved. Then draft
again — the narrative paragraphs fill in, and if the model produces a
law-report citation you did not pass via `--authority`, it is flagged at the top
of the output.

To test the citation guard deliberately, give it facts that invite a citation
and supply none; the `UNVERIFIED CITATION` warning is what you are checking for.

---

## 5. Testing the Telegram bot (optional)

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."   # from @BotFather

# Message your bot once, then read your chat id:
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"

# Register that id, then run the bot:
barrister adduser "Your Name" --telegram <chat id>
barrister bot
```

Now message the bot: `/help`, `/today`, `/diary`, `/matters`,
`/limitation 2026-01-15 90 appeal`, `/statute s. 5 of the Limitation Act`.

To test the bot logic **without Telegram at all**, the routing is a plain
function:

```bash
python3 - <<'PY'
from barrister.db import session
from barrister.services.bot import handle
from barrister.services.watchlist import add_user
with session(":memory:") as c:
    add_user(c, "Sabir", telegram_chat_id="123")
    print(handle(c, "123", "/limitation 2026-01-15 90 appeal").text)
PY
```

---

## 6. Running the automated tests

The 300+ tests need no network and finish in about twelve seconds:

```bash
python3 -m pytest              # everything
python3 -m pytest -q tests/test_limitation.py    # one area
python3 -m pytest -k matching                    # by keyword
```

Every parser test runs against a real page saved in `tests/fixtures/`, so if the
Court changes its HTML and something here stops matching, that is the test to
look at.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/` says "not registered" | Empty database | `python3 tools/seed_demo.py` or `barrister adduser …` |
| Statute search finds nothing | Corpus not synced | `barrister statutes sync --act 88`, or run the seed |
| Cause list page errors | Court site slow or down | It fetches live; retry, or use a seeded page. Not a crash. |
| Drafting says "no model configured" | No API key | Expected. Set a key (§4) or read the scaffold as-is. |
| `python-multipart` error on start | Missing dependency | `pip install -r requirements.txt` (it is listed there) |
| Everything is slow | The politeness layer | By design — one request at a time, 1.5s apart. Seed for instant data. |
