"""User and watch management, alert persistence, and the nightly sweep."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..scrapers.cause_list import CauseListEntry
from .matching import Match, match_all
from .notify import Notifier

log = logging.getLogger(__name__)


# -- users and watches --------------------------------------------------

def add_user(
    conn: sqlite3.Connection,
    name: str,
    *,
    chamber: str | None = None,
    telegram_chat_id: str | None = None,
    email: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO users (name, chamber, telegram_chat_id, email) VALUES (?, ?, ?, ?)",
        (name, chamber, telegram_chat_id, email),
    )
    return int(cursor.lastrowid)


def add_watch(conn: sqlite3.Connection, user_id: int, kind: str, value: str) -> int:
    if kind not in {"advocate", "party", "case"}:
        raise ValueError(f"unknown watch kind: {kind!r}")
    conn.execute(
        """INSERT INTO watches (user_id, kind, value) VALUES (?, ?, ?)
           ON CONFLICT(user_id, kind, value) DO UPDATE SET active = 1""",
        (user_id, kind, value.strip()),
    )
    row = conn.execute(
        "SELECT id FROM watches WHERE user_id = ? AND kind = ? AND value = ?",
        (user_id, kind, value.strip()),
    ).fetchone()
    return int(row["id"])


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> None:
    conn.execute("UPDATE watches SET active = 0 WHERE id = ?", (watch_id,))


def list_watches(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY kind, value",
        (user_id,),
    ).fetchall()


def active_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT DISTINCT u.* FROM users u
           JOIN watches w ON w.user_id = u.id AND w.active = 1
           ORDER BY u.id"""
    ).fetchall()


# -- storing the day's list --------------------------------------------

def store_entries(conn: sqlite3.Connection, entries: Iterable[CauseListEntry]) -> int:
    """Persist parsed listings. Re-running the same sweep is a no-op."""
    stored = 0
    for entry in entries:
        conn.execute(
            """INSERT INTO cause_list_entries
               (list_date, division, court_id, bench_id, court_name, judges, section,
                serial, case_type, case_number, case_year, district, parties, advocates, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (
                entry.list_date, entry.division, entry.court_id, entry.bench_id,
                entry.court_name, entry.judges, entry.section, entry.serial,
                entry.case_type, entry.case_number, entry.case_year, entry.district,
                entry.parties, "; ".join(entry.advocates), entry.raw,
            ),
        )
        stored += 1
    return stored


# -- alerts -------------------------------------------------------------

def format_alert(matches: Sequence[Match]) -> tuple[str, str]:
    """Render one user's matches into a subject and a body they can act on.

    A barrister reads this on a phone the evening before. What they need, in
    order: which court, what serial number, what the bench will do with it.
    """
    first = matches[0].entry
    subject = f"Cause list {first.list_date}: {len(matches)} matter(s) listed"

    lines: list[str] = []
    for match in sorted(
        matches, key=lambda m: (m.entry.court_name or "", m.entry.serial or 0)
    ):
        entry = match.entry
        lines.append(f"• {entry.case_ref}")
        lines.append(f"   {entry.division} — {entry.court_name or 'court n/a'}")
        if entry.judges:
            lines.append(f"   Bench: {entry.judges}")
        serial = f"Serial {entry.serial}" if entry.serial is not None else "Serial n/a"
        if entry.connected_to is not None:
            serial += f" (heard with serial {entry.connected_to})"
        lines.append(f"   {serial} — {entry.section or 'section n/a'}")
        if entry.petitioner or entry.respondent:
            lines.append(f"   {entry.petitioner or '?'} vs {entry.respondent or '?'}")
        if entry.notes:
            lines.append(f"   Note: {', '.join(entry.notes)}")
        lines.append(f"   Matched: {match.reason}")
        lines.append("")

    lines.append("Source: supremecourt.gov.bd — verify against the official list before relying on it.")
    return subject, "\n".join(lines)


def queue_alert(
    conn: sqlite3.Connection, user_id: int, dedupe_key: str, subject: str, body: str
) -> int | None:
    """Record an alert unless this user already has one with the same key."""
    cursor = conn.execute(
        """INSERT INTO alerts (user_id, dedupe_key, subject, body)
           VALUES (?, ?, ?, ?) ON CONFLICT(user_id, dedupe_key) DO NOTHING""",
        (user_id, dedupe_key, subject, body),
    )
    return int(cursor.lastrowid) if cursor.rowcount else None


def deliver_pending(conn: sqlite3.Connection, notifier: Notifier) -> int:
    """Send every alert not yet confirmed delivered. Safe to re-run."""
    rows = conn.execute(
        """SELECT a.*, u.telegram_chat_id, u.email, u.name
           FROM alerts a JOIN users u ON u.id = a.user_id
           WHERE a.delivered_at IS NULL ORDER BY a.id"""
    ).fetchall()

    delivered = 0
    for row in rows:
        recipient = row["telegram_chat_id"] or row["email"] or row["name"]
        if notifier.send(recipient, row["subject"], row["body"]):
            conn.execute(
                "UPDATE alerts SET delivered_at = datetime('now') WHERE id = ?", (row["id"],)
            )
            delivered += 1
        else:
            log.warning("alert %s undelivered; will retry next run", row["id"])
    return delivered


@dataclass
class SweepResult:
    entries_seen: int
    users_notified: int
    alerts_created: int
    alerts_delivered: int


def run_sweep(
    conn: sqlite3.Connection,
    entries: Sequence[CauseListEntry],
    notifier: Notifier,
    *,
    deliver: bool = True,
) -> SweepResult:
    """Match the day's list against every watch and queue alerts.

    Alerts are grouped per user — one message listing all their matters, not
    one message per matter. A barrister with nine listed cases wants one
    notification, not nine.
    """
    store_entries(conn, entries)

    created = 0
    notified = 0
    for user in active_users(conn):
        watches = [(w["kind"], w["value"]) for w in list_watches(conn, user["id"])]
        matches = match_all(entries, watches)
        if not matches:
            continue

        subject, body = format_alert(matches)
        # One alert per user per list-date; re-running the sweep after the
        # court amends the list produces a new key only if the matters changed.
        dedupe_key = "causelist:" + "|".join(sorted(m.dedupe_key for m in matches))
        if queue_alert(conn, int(user["id"]), dedupe_key, subject, body) is not None:
            created += 1
        notified += 1

    delivered = deliver_pending(conn, notifier) if deliver else 0
    return SweepResult(len(entries), notified, created, delivered)
