"""Telegram bot interface.

Roadmap #7: move the tools into a surface the barrister already has open. A
dashboard is somewhere you go; a chat thread is somewhere you already are, and
the alerts land there anyway.

The design keeps all the logic in the services and makes this module a router:
parse a command, call a service, format a reply. That means a WhatsApp port
later replaces this file and nothing else.

Transport is long polling (``getUpdates``), not webhooks, deliberately — a
barrister running this on a laptop or a cheap VPS has no public HTTPS endpoint,
and long polling needs no inbound connectivity at all.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import httpx

from ..config import Settings, settings as default_settings
from ..db import connect, init_db
from ..http import PoliteClient
from ..scrapers import case_status as case_status_scraper
from . import matters as matters_service
from . import limitation, statutes, watchlist

log = logging.getLogger(__name__)

MAX_MESSAGE = 4096

HELP = """\
*Barrister Tools*

_Your day_
/today — your listings for today
/diary — deadlines falling due
/matters — your open files

_Watching_
/watch advocate Sabir Rahman
/watch case First Appeal 226/2013
/watches — what you're watching
/unwatch 3

_Look up_
/case First Appeal 226 2013 — status and history
/statute s. 5 of the Limitation Act
/limitation 2026-01-15 90 appeal — deadline with working

_Files_
/matter MAT-2026-001 — open a file
/note MAT-2026-001 Conference with client
/time MAT-2026-001 90 Drafting the petition

/help — this message"""


class BotError(Exception):
    """A problem the user should be told about in plain words."""


@dataclass
class Reply:
    text: str
    markdown: bool = True

    def truncated(self) -> str:
        if len(self.text) <= MAX_MESSAGE:
            return self.text
        return self.text[: MAX_MESSAGE - 20].rstrip() + "\n… (truncated)"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _escape(text: str) -> str:
    """Neutralise Telegram Markdown so a party's name cannot break formatting."""
    return re.sub(r"([*_`\[\]])", r"\\\1", text or "")


def _resolve_user(conn: sqlite3.Connection, chat_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_chat_id = ?", (str(chat_id),)
    ).fetchone()
    if row is None:
        raise BotError(
            "I don't know you yet. Ask whoever runs this install to register "
            f"your chat id `{chat_id}`:\n\n"
            f"`barrister adduser \"Your Name\" --telegram {chat_id}`"
        )
    return row


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise BotError(f"I couldn't read {value!r} as a date. Try 2026-01-15 or 15/01/2026.")


def _matter_or_fail(conn: sqlite3.Connection, user_id: int, reference: str) -> sqlite3.Row:
    matter = matters_service.find_matter_by_reference(conn, user_id, reference)
    if matter is None:
        raise BotError(f"No file with reference `{_escape(reference)}`. Try /matters.")
    return matter


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_help(conn, user, args: str, settings: Settings) -> Reply:
    return Reply(HELP)


def cmd_today(conn, user, args: str, settings: Settings) -> Reply:
    """Today's listings, read from the last sweep rather than re-scraping.

    The sweep runs on a cron the evening before; hitting the Court's site again
    for every /today would multiply our traffic by the number of impatient
    users, for data that has not changed.
    """
    when = _parse_date(args.strip()) if args.strip() else date.today()
    listed = when.strftime("%d/%m/%Y")

    rows = conn.execute(
        """SELECT DISTINCT e.* FROM cause_list_entries e
           JOIN watches w ON w.user_id = ? AND w.active = 1
           WHERE e.list_date = ?
             AND (
               (w.kind = 'case'  AND (e.case_type || ' ' || e.case_number || '/' || e.case_year)
                                     LIKE '%' || w.value || '%')
               OR (w.kind = 'advocate' AND e.advocates LIKE '%' || w.value || '%')
               OR (w.kind = 'party'    AND e.parties   LIKE '%' || w.value || '%')
             )
           ORDER BY e.court_name, e.serial""",
        (user["id"], listed),
    ).fetchall()

    if not rows:
        return Reply(
            f"Nothing of yours listed for *{listed}*.\n\n"
            "_If the list has only just been published, the sweep may not have run yet._"
        )

    lines = [f"*Your listings — {listed}*", ""]
    for row in rows:
        case_ref = f"{row['case_type']} {row['case_number']}/{row['case_year']}"
        matter = matters_service.matter_for_case(
            conn, int(user["id"]),
            case_type=row["case_type"], case_number=row["case_number"], case_year=row["case_year"],
        )
        suffix = f"  [{matter['reference']}]" if matter else ""
        lines.append(f"*{_escape(case_ref)}*{suffix}")
        lines.append(f"  {_escape(row['court_name'] or '')} — serial {row['serial']}")
        lines.append(f"  _{_escape(row['section'] or 'section n/a')}_")
        lines.append("")
    return Reply("\n".join(lines))


def cmd_diary(conn, user, args: str, settings: Settings) -> Reply:
    days = int(args.strip()) if args.strip().isdigit() else 30
    rows = matters_service.upcoming_deadlines(conn, int(user["id"]), within_days=days)
    if not rows:
        return Reply(f"No deadlines in the next {days} days.")

    today = date.today()
    lines = [f"*Deadlines — next {days} days*", ""]
    for row in rows:
        due = date.fromisoformat(row["due_on"])
        remaining = (due - today).days
        if remaining < 0:
            marker = f"⚠️ OVERDUE by {abs(remaining)}d"
        elif remaining <= 3:
            marker = f"⚠️ {remaining}d left"
        else:
            marker = f"{remaining}d left"
        lines.append(f"*{row['due_on']}* — {marker}")
        lines.append(f"  {_escape(row['label'])}")
        lines.append(f"  {_escape(row['reference'])} · {_escape(row['title'])}")
        if not row["verified"]:
            lines.append("  _basis not verified — check before relying on it_")
        lines.append("")
    return Reply("\n".join(lines))


def cmd_matters(conn, user, args: str, settings: Settings) -> Reply:
    rows = matters_service.list_matters(conn, int(user["id"]), status=args.strip() or None)
    if not rows:
        return Reply("No files yet.")
    summary = matters_service.practice_summary(conn, int(user["id"]))
    lines = [
        f"*Your files* — {summary['matters']['open']} open, "
        f"{summary['unbilled_hours']}h unbilled",
        "",
    ]
    for row in rows[:30]:
        lines.append(f"`{row['reference']}` *{_escape(row['title'])}*")
        detail = [row["status"]]
        if row["client_name"]:
            detail.append(_escape(row["client_name"]))
        if row["case_count"]:
            detail.append(f"{row['case_count']} case(s)")
        lines.append("  " + " · ".join(detail))
    return Reply("\n".join(lines))


def cmd_matter(conn, user, args: str, settings: Settings) -> Reply:
    if not args.strip():
        raise BotError("Which file? e.g. `/matter MAT-2026-001`")
    matter = _matter_or_fail(conn, int(user["id"]), args.strip())
    overview = matters_service.matter_overview(conn, int(matter["id"]))

    lines = [f"`{matter['reference']}` *{_escape(matter['title'])}*", ""]
    if overview["matter"]["client_name"]:
        lines.append(f"Client: {_escape(overview['matter']['client_name'])}")
    lines.append(f"Status: {matter['status']}")
    lines.append(f"Time: {overview['time']['hours']}h "
                 f"({overview['time']['unbilled_minutes']}m unbilled)")

    if overview["cases"]:
        lines += ["", "*Cases*"]
        lines += [
            f"  {_escape(c['case_type'])} {c['case_number']}/{c['case_year']}"
            for c in overview["cases"]
        ]
    open_deadlines = [d for d in overview["deadlines"] if not d["completed"]]
    if open_deadlines:
        lines += ["", "*Deadlines*"]
        lines += [f"  {d['due_on']} — {_escape(d['label'])}" for d in open_deadlines]
    if overview["notes"]:
        lines += ["", "*Recent notes*"]
        lines += [
            f"  {n['noted_on']} — {_escape(n['body'][:80])}" for n in overview["notes"][:5]
        ]
    return Reply("\n".join(lines))


def cmd_watch(conn, user, args: str, settings: Settings) -> Reply:
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in ("advocate", "party", "case"):
        raise BotError(
            "Use `/watch advocate <name>`, `/watch party <name>` or `/watch case <reference>`."
        )
    kind, value = parts[0], parts[1].strip()
    watch_id = watchlist.add_watch(conn, int(user["id"]), kind, value)
    return Reply(f"Watching {kind} *{_escape(value)}* (watch `{watch_id}`).")


def cmd_watches(conn, user, args: str, settings: Settings) -> Reply:
    rows = watchlist.list_watches(conn, int(user["id"]))
    if not rows:
        return Reply("You're not watching anything. Try `/watch advocate Your Name`.")
    lines = ["*Watching*", ""]
    lines += [f"`{r['id']}` {r['kind']} — {_escape(r['value'])}" for r in rows]
    lines += ["", "_Remove one with /unwatch <id>_"]
    return Reply("\n".join(lines))


def cmd_unwatch(conn, user, args: str, settings: Settings) -> Reply:
    if not args.strip().isdigit():
        raise BotError("Use `/unwatch <id>` — see /watches for the ids.")
    watch_id = int(args.strip())
    row = conn.execute(
        "SELECT * FROM watches WHERE id = ? AND user_id = ?", (watch_id, user["id"])
    ).fetchone()
    if row is None:
        raise BotError(f"No watch `{watch_id}` of yours.")
    watchlist.remove_watch(conn, watch_id)
    return Reply(f"Stopped watching {row['kind']} *{_escape(row['value'])}*.")


def cmd_case(conn, user, args: str, settings: Settings) -> Reply:
    parts = args.strip().rsplit(maxsplit=2)
    if len(parts) != 3:
        raise BotError("Use `/case <type> <number> <year>`, e.g. `/case First Appeal 226 2013`.")
    case_type, number, year = parts

    with PoliteClient(settings) as client:
        try:
            status = case_status_scraper.fetch_case_status(
                client, case_type=case_type, case_number=number, year=year
            )
        except ValueError as exc:
            raise BotError(str(exc)) from exc

    lines = [f"*{_escape(status.case_ref)}*", ""]
    lines.append(f"{_escape(status.petitioner or '?')}\nvs\n{_escape(status.respondent or '?')}")
    if status.hearings:
        lines += ["", f"*Last {min(5, len(status.hearings))} of {len(status.hearings)} hearings*"]
        for hearing in status.hearings[:5]:
            lines.append(
                f"  {hearing.date or '?'} — {_escape(hearing.result or 'no result recorded')}"
            )
    lines += ["", "_Verify against the official record before relying on it._"]
    return Reply("\n".join(lines))


def cmd_statute(conn, user, args: str, settings: Settings) -> Reply:
    if not args.strip():
        raise BotError("What are you looking for? e.g. `/statute s. 5 of the Limitation Act`")
    hits = statutes.search(conn, args.strip(), limit=3)
    if not hits:
        return Reply(
            "Nothing found. The corpus may not be synced yet "
            "(`barrister statutes sync --act 88`)."
        )
    lines = []
    for hit in hits:
        lines.append(f"*{_escape(hit.citation)}*")
        lines.append(f"_{_escape(hit.heading)}_")
        body = hit.body if len(hit.body) <= 900 else hit.body[:900].rstrip() + " …"
        lines.append(_escape(body))
        lines.append(hit.url)
        lines.append("")
    return Reply("\n".join(lines))


def cmd_limitation(conn, user, args: str, settings: Settings) -> Reply:
    parts = args.split()
    if len(parts) < 2:
        raise BotError(
            "Use `/limitation <date> <days> [proceeding]`, "
            "e.g. `/limitation 2026-01-15 90 appeal`."
        )
    start = _parse_date(parts[0])
    if not parts[1].isdigit():
        raise BotError(f"I couldn't read {parts[1]!r} as a number of days.")
    proceeding = parts[2] if len(parts) > 2 else "suit"

    result = limitation.compute(
        start_date=start, period=limitation.Period(int(parts[1]), "days"), proceeding=proceeding
    )
    lines = [
        f"*Deadline: {result.deadline.isoformat()}* ({result.deadline:%A})",
        f"{result.days_remaining()} days from today",
        "",
        "*Working*",
    ]
    lines += [
        f"  {i}. [{s.rule}] {_escape(s.explanation)}"
        + (f" → {s.result.isoformat()}" if s.result else "")
        for i, s in enumerate(result.steps, start=1)
    ]
    if result.warnings:
        lines += ["", "*Note*"] + [f"  {_escape(w)}" for w in result.warnings]
    lines += ["", "_A computation, not advice._"]
    return Reply("\n".join(lines))


def cmd_note(conn, user, args: str, settings: Settings) -> Reply:
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        raise BotError("Use `/note <file reference> <text>`.")
    matter = _matter_or_fail(conn, int(user["id"]), parts[0])
    matters_service.add_note(conn, int(matter["id"]), parts[1])
    return Reply(f"Noted on `{matter['reference']}`.")


def cmd_time(conn, user, args: str, settings: Settings) -> Reply:
    parts = args.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        raise BotError("Use `/time <file reference> <minutes> <description>`.")
    matter = _matter_or_fail(conn, int(user["id"]), parts[0])
    matters_service.log_time(conn, int(matter["id"]), int(parts[1]), parts[2])
    summary = matters_service.time_summary(conn, int(matter["id"]))
    return Reply(
        f"Logged {parts[1]}m on `{matter['reference']}` — {summary.hours}h total."
    )


COMMANDS: dict[str, Callable[..., Reply]] = {
    "start": cmd_help,
    "help": cmd_help,
    "today": cmd_today,
    "diary": cmd_diary,
    "matters": cmd_matters,
    "matter": cmd_matter,
    "watch": cmd_watch,
    "watches": cmd_watches,
    "unwatch": cmd_unwatch,
    "case": cmd_case,
    "statute": cmd_statute,
    "limitation": cmd_limitation,
    "note": cmd_note,
    "time": cmd_time,
}


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def handle(
    conn: sqlite3.Connection, chat_id: str, text: str, settings: Settings | None = None
) -> Reply:
    """Route one incoming message to a command. Never raises."""
    settings = settings or default_settings
    text = (text or "").strip()
    if not text.startswith("/"):
        return Reply("Send /help to see what I can do.")

    head, _, args = text.partition(" ")
    # "/today@ChamberBot" in a group chat addresses this bot specifically.
    name = head[1:].split("@", 1)[0].lower()

    handler = COMMANDS.get(name)
    if handler is None:
        return Reply(f"I don't know `/{_escape(name)}`. Send /help.")

    try:
        user = _resolve_user(conn, chat_id) if name not in ("start", "help") else None
        return handler(conn, user, args, settings)
    except BotError as exc:
        return Reply(str(exc))
    except Exception:  # a bug must not kill the poll loop
        log.exception("bot command /%s failed", name)
        return Reply("Something went wrong handling that. It has been logged.")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

class TelegramTransport:
    """Long-polling client for the Telegram Bot API."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or default_settings
        if not self.settings.telegram_bot_token:
            raise BotError("TELEGRAM_BOT_TOKEN is not set")
        self._client = client or httpx.Client(timeout=70.0)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"

    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = self._client.get(self._url("getUpdates"), params=params)
        response.raise_for_status()
        return response.json().get("result", [])

    def send(self, chat_id: str, reply: Reply) -> bool:
        payload = {"chat_id": chat_id, "text": reply.truncated()}
        if reply.markdown:
            payload["parse_mode"] = "Markdown"
        response = self._client.post(self._url("sendMessage"), data=payload)
        if response.status_code != 200 and reply.markdown:
            # A stray bracket in a party's name can make Telegram reject the
            # whole message; better plain text than nothing.
            log.warning("markdown rejected, retrying plain: %s", response.text[:200])
            response = self._client.post(
                self._url("sendMessage"), data={"chat_id": chat_id, "text": reply.truncated()}
            )
        return response.status_code == 200


def run(
    settings: Settings | None = None,
    *,
    transport: TelegramTransport | None = None,
    db_path: str | None = None,
    max_iterations: int | None = None,
) -> int:
    """Poll for messages and answer them.

    ``max_iterations`` bounds the loop for tests; production passes nothing and
    runs until interrupted.
    """
    settings = settings or default_settings
    transport = transport or TelegramTransport(settings)
    offset: int | None = None
    handled = 0
    iterations = 0

    log.info("bot polling for updates")
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            updates = transport.get_updates(offset)
        except httpx.HTTPError as exc:
            log.warning("getUpdates failed, retrying: %s", exc)
            continue

        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "")
            if not chat_id:
                continue

            conn = connect(db_path)
            try:
                init_db(conn)
                reply = handle(conn, chat_id, text, settings)
                conn.commit()
            finally:
                conn.close()

            transport.send(chat_id, reply)
            handled += 1

    return handled
