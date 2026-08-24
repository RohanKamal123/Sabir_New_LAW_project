# Operations

How to run this in a chamber, on one box, without surprises.

## Before anything else

Read [`COMPLIANCE.md`](COMPLIANCE.md). Two checks there still need a human, and
one of them (terms of use) should happen before you point the sweep at
production on a schedule.

Set a contact address. It goes in the `User-Agent` on every request:

```bash
export BARRISTER_CONTACT_EMAIL="clerk@yourchambers.example"
```

A court IT team that can email you does not have to block you.

## Install

```bash
git clone <this repo> && cd Sabir_New_LAW_project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11+.

## First run

```bash
# 1. Register the barrister. The first user is the one the web UI shows.
barrister adduser "Sabir Rahman" --chamber "Rahman & Co." --telegram 123456789

# 2. Tell it what to watch for. Your name as the cause list prints it.
barrister watch 1 advocate "Sabir Rahman"

# 3. Open a file and attach a case. This also starts watching the case.
barrister matter open 1 "Ruhul Amin v Aiyub Ali" \
    --client "Md. Ruhul Amin" --case "First Appeal 226 2013"

# 4. Pull the statute corpus. Start with what you actually use.
barrister statutes sync --act 88          # The Limitation Act, 1908

# 5. Check it works.
barrister sweep --dry-run
```

## The cron

The Court publishes lists the evening before hearings. One sweep an hour or so
after that is the whole operational requirement.

```cron
# Cause list sweep — the evening before hearings
30 20 * * 0-4  cd /opt/barrister && .venv/bin/barrister sweep >> /var/log/barrister/sweep.log 2>&1

# Case status re-check — weekly is plenty; histories move slowly
0 6 * * 5      cd /opt/barrister && .venv/bin/barrister status "First Appeal" 226 2013 --track-for 1 >> /var/log/barrister/track.log 2>&1
```

Sunday to Thursday is the Bangladeshi working week; Friday and Saturday are the
weekend, which is also what the limitation calculator assumes for s. 4.

`sweep` is idempotent — an alert already delivered is not resent — so running it
twice, or re-running after a failure, is safe.

## Telegram

```bash
# 1. Create a bot with @BotFather, then:
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."

# 2. Message your bot once, then find your chat id:
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | grep -o '"id":[0-9]*'

# 3. Register that chat id against the user:
barrister adduser "Sabir Rahman" --telegram <chat id>

# 4. Run the bot (systemd unit below).
barrister bot
```

Long polling, so no public HTTPS endpoint and no inbound firewall rule needed.

## The web UI

```bash
uvicorn barrister.api:app --host 127.0.0.1 --port 8000
```

Bind to localhost. **There is no authentication** — the app is single-tenant and
assumes it is reachable only by the person it belongs to. If you need it from
outside the machine, put it behind an authenticating reverse proxy or a
Tailscale/WireGuard interface. Do not put it on a public IP.

## systemd

`/etc/systemd/system/barrister-web.service`:

```ini
[Unit]
Description=Barrister Tools web UI
After=network.target

[Service]
Type=simple
User=barrister
WorkingDirectory=/opt/barrister
Environment=BARRISTER_DATA_DIR=/var/lib/barrister
Environment=BARRISTER_CONTACT_EMAIL=clerk@yourchambers.example
ExecStart=/opt/barrister/.venv/bin/uvicorn barrister.api:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/barrister-bot.service`:

```ini
[Unit]
Description=Barrister Tools Telegram bot
After=network.target

[Service]
Type=simple
User=barrister
WorkingDirectory=/opt/barrister
EnvironmentFile=/etc/barrister/env      # holds TELEGRAM_BOT_TOKEN, API keys
Environment=BARRISTER_DATA_DIR=/var/lib/barrister
ExecStart=/opt/barrister/.venv/bin/barrister bot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Keep `/etc/barrister/env` at mode 600. It holds API keys.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BARRISTER_DATA_DIR` | `~/.barrister` | Database and HTTP cache |
| `BARRISTER_CONTACT_EMAIL` | *(unset)* | Contact address in the `User-Agent`. Set it. |
| `BARRISTER_REQUEST_DELAY` | `1.5` | Minimum seconds between upstream requests |
| `BARRISTER_REQUEST_TIMEOUT` | `30` | Per-request timeout |
| `BARRISTER_MAX_RETRIES` | `3` | Retries on transient failures |
| `BARRISTER_CACHE_TTL` | `21600` | Response cache lifetime (6h) |
| `BARRISTER_UA` | `BarristerTools/0.1` | `User-Agent` product token |
| `TELEGRAM_BOT_TOKEN` | *(unset)* | Enables Telegram delivery and the bot |
| `BARRISTER_DRAFTING_PROVIDER` | `auto` | `auto`, `anthropic`, `deepseek` |
| `ANTHROPIC_API_KEY` | *(unset)* | Drafting via Claude |
| `BARRISTER_DRAFTING_MODEL` | `claude-opus-5` | Claude model |
| `DEEPSEEK_API_KEY` | *(unset)* | Drafting via DeepSeek |
| `BARRISTER_DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek model |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | For a proxy |
| `BARRISTER_DRAFTING_MAX_TOKENS` | `64000` | Ceiling per draft |

## Backup

Everything that matters is one file.

```bash
sqlite3 /var/lib/barrister/barrister.db ".backup /backup/barrister-$(date +%F).db"
```

Use `.backup`, not `cp` — the database runs in WAL mode and a plain copy can
catch it mid-write. The HTTP cache under `$BARRISTER_DATA_DIR/http-cache` is
disposable; deleting it costs one slow sweep.

## When something breaks

**The sweep found nothing.** Check the Court has published. Lists appear the
evening before hearings and there is no list for a vacation day. `barrister
causelist --division 2 --limit 1` shows what the site is actually serving.

**Alerts stopped arriving.** Undelivered alerts stay queued, so nothing is lost.
`sqlite3 barrister.db "SELECT COUNT(*) FROM alerts WHERE delivered_at IS NULL"`.
A non-zero count with a working token usually means the chat id is wrong.

**A parser started returning nothing.** The Court changed its markup. The
fixtures in `tests/fixtures/` are the previous shape; save the new page beside
them, update the parser, and the diff between fixtures tells you what moved.

**Statute search returns nothing.** The corpus is probably not synced —
`barrister statutes search` says so explicitly. Check with
`sqlite3 barrister.db "SELECT COUNT(*) FROM statute_sections"`.

**Requests are being refused.** Stop, do not retry harder. Raise
`BARRISTER_REQUEST_DELAY`, confirm `BARRISTER_CONTACT_EMAIL` is set, and email
the Court's IT contact. Getting a legal product blocked for hammering a court
website would be an unusually ironic own-goal.

## Upgrading

Pull, then run anything — the schema catches up on the next connection.

New tables and indexes come from `CREATE TABLE IF NOT EXISTS`. Columns added to
tables that already shipped are listed in `ADDED_COLUMNS` in `barrister/db.py`
and applied with `ALTER TABLE` idempotently at the same point, because
`IF NOT EXISTS` silently leaves an existing table alone and a new column would
otherwise never appear on an older install.

There is no down-migration and no version stamp; the schema is small enough that
this is the whole story. If you ever do need to reset something, the tables
holding only re-fetchable data are `cause_list_entries`, `snapshots` and
`statute_*`. Never drop `matters`, `clients`, `time_entries`, `matter_notes`,
`matter_documents`, `matter_deadlines` or `alerts` — those hold the only rows in
the database that cannot be scraped again.
