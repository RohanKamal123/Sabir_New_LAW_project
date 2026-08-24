"""Matter and case-file management.

A matter is the file a barrister keeps: one brief, one client, one or more
court cases, plus the notes, documents, time and deadlines that accrete around
it. This is plain CRUD — no AI anywhere near it — but it is the piece that
turns four separate tools into one desk, because every other feature can now
answer "which of my files does this belong to?"

The important join is :func:`link_case`: registering a case against a matter
also creates the cause-list watch for it, so a listing found by the nightly
sweep can be reported as "Rahman v Bangladesh — your file MAT-2026-004" rather
than as a bare case number the barrister has to recognise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .watchlist import add_watch

MATTER_STATUSES = ("open", "reserved", "disposed", "closed")
NOTE_KINDS = ("note", "attendance", "advice", "hearing")


class MatterError(ValueError):
    """Raised for an invalid matter operation."""


def _row(cursor: sqlite3.Cursor | sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row | None:
    return cursor.execute(sql, params).fetchone()


# --------------------------------------------------------------------------
# clients
# --------------------------------------------------------------------------

def add_client(
    conn: sqlite3.Connection,
    user_id: int,
    name: str,
    *,
    phone: str | None = None,
    email: str | None = None,
    address: str | None = None,
    notes: str | None = None,
) -> int:
    if not name.strip():
        raise MatterError("a client needs a name")
    cursor = conn.execute(
        "INSERT INTO clients (user_id, name, phone, email, address, notes) VALUES (?,?,?,?,?,?)",
        (user_id, name.strip(), phone, email, address, notes),
    )
    return int(cursor.lastrowid)


def list_clients(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clients WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()


# --------------------------------------------------------------------------
# matters
# --------------------------------------------------------------------------

def next_reference(conn: sqlite3.Connection, user_id: int, *, year: int | None = None) -> str:
    """Chamber-style file reference: ``MAT-2026-004``.

    Sequential within a year and per barrister, so two users of the same
    install never collide.
    """
    year = year or date.today().year
    prefix = f"MAT-{year}-"
    row = conn.execute(
        "SELECT reference FROM matters WHERE user_id = ? AND reference LIKE ? "
        "ORDER BY reference DESC LIMIT 1",
        (user_id, prefix + "%"),
    ).fetchone()
    if row is None:
        return f"{prefix}001"
    try:
        last = int(row["reference"].rsplit("-", 1)[1])
    except (IndexError, ValueError):
        last = 0
    return f"{prefix}{last + 1:03d}"


def open_matter(
    conn: sqlite3.Connection,
    user_id: int,
    title: str,
    *,
    reference: str | None = None,
    client_id: int | None = None,
    description: str | None = None,
    court: str | None = None,
    fee_agreed: float | None = None,
) -> int:
    if not title.strip():
        raise MatterError("a matter needs a title")
    reference = (reference or next_reference(conn, user_id)).strip()
    try:
        cursor = conn.execute(
            """INSERT INTO matters
               (user_id, client_id, reference, title, description, court, fee_agreed)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, client_id, reference, title.strip(), description, court, fee_agreed),
        )
    except sqlite3.IntegrityError as exc:
        raise MatterError(f"reference {reference!r} is already in use") from exc
    return int(cursor.lastrowid)


def get_matter(conn: sqlite3.Connection, matter_id: int) -> sqlite3.Row | None:
    return _row(
        conn,
        """SELECT m.*, c.name AS client_name, c.phone AS client_phone
           FROM matters m LEFT JOIN clients c ON c.id = m.client_id
           WHERE m.id = ?""",
        (matter_id,),
    )


def find_matter_by_reference(
    conn: sqlite3.Connection, user_id: int, reference: str
) -> sqlite3.Row | None:
    return _row(
        conn,
        "SELECT * FROM matters WHERE user_id = ? AND reference = ?",
        (user_id, reference.strip()),
    )


def list_matters(
    conn: sqlite3.Connection, user_id: int, *, status: str | None = None
) -> list[sqlite3.Row]:
    sql = """SELECT m.*, c.name AS client_name,
                    (SELECT COUNT(*) FROM matter_cases mc WHERE mc.matter_id = m.id) AS case_count
             FROM matters m LEFT JOIN clients c ON c.id = m.client_id
             WHERE m.user_id = ?"""
    params: list[Any] = [user_id]
    if status:
        if status not in MATTER_STATUSES:
            raise MatterError(f"unknown status {status!r}")
        sql += " AND m.status = ?"
        params.append(status)
    sql += " ORDER BY CASE m.status WHEN 'open' THEN 0 ELSE 1 END, m.reference DESC"
    return conn.execute(sql, params).fetchall()


def set_status(conn: sqlite3.Connection, matter_id: int, status: str) -> None:
    if status not in MATTER_STATUSES:
        raise MatterError(f"unknown status {status!r}; expected one of {MATTER_STATUSES}")
    closed_on = date.today().isoformat() if status in ("disposed", "closed") else None
    conn.execute(
        "UPDATE matters SET status = ?, closed_on = ? WHERE id = ?",
        (status, closed_on, matter_id),
    )


# --------------------------------------------------------------------------
# cases attached to a matter
# --------------------------------------------------------------------------

def link_case(
    conn: sqlite3.Connection,
    matter_id: int,
    *,
    case_type: str,
    case_number: str,
    case_year: str,
    division_id: int | None = None,
    watch: bool = True,
) -> int:
    """Attach a court case to a matter, and start watching it by default.

    The watch is what makes the file useful the evening before a hearing: the
    sweep finds the listing, and :func:`matter_for_case` maps it back here.
    """
    matter = get_matter(conn, matter_id)
    if matter is None:
        raise MatterError(f"no matter {matter_id}")

    watch_id: int | None = None
    if watch:
        watch_id = add_watch(
            conn, int(matter["user_id"]), "case", f"{case_type} {case_number}/{case_year}"
        )

    conn.execute(
        """INSERT INTO matter_cases
           (matter_id, case_type, case_number, case_year, division_id, watch_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(matter_id, case_type, case_number, case_year)
           DO UPDATE SET watch_id = excluded.watch_id, division_id = excluded.division_id""",
        (matter_id, case_type.strip(), str(case_number).strip(), str(case_year).strip(),
         division_id, watch_id),
    )
    row = _row(
        conn,
        """SELECT id FROM matter_cases
           WHERE matter_id = ? AND case_type = ? AND case_number = ? AND case_year = ?""",
        (matter_id, case_type.strip(), str(case_number).strip(), str(case_year).strip()),
    )
    return int(row["id"])


def list_cases(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matter_cases WHERE matter_id = ? ORDER BY case_year DESC, case_number",
        (matter_id,),
    ).fetchall()


def matter_for_case(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    case_type: str,
    case_number: str,
    case_year: str,
) -> sqlite3.Row | None:
    """Which of this barrister's files does a listed case belong to?"""
    return _row(
        conn,
        """SELECT m.* FROM matters m
           JOIN matter_cases mc ON mc.matter_id = m.id
           WHERE m.user_id = ? AND mc.case_type = ? AND mc.case_number = ? AND mc.case_year = ?
           LIMIT 1""",
        (user_id, case_type, str(case_number), str(case_year)),
    )


# --------------------------------------------------------------------------
# notes, documents, time
# --------------------------------------------------------------------------

def add_note(
    conn: sqlite3.Connection,
    matter_id: int,
    body: str,
    *,
    kind: str = "note",
    noted_on: date | None = None,
) -> int:
    if kind not in NOTE_KINDS:
        raise MatterError(f"unknown note kind {kind!r}; expected one of {NOTE_KINDS}")
    if not body.strip():
        raise MatterError("a note needs a body")
    cursor = conn.execute(
        "INSERT INTO matter_notes (matter_id, body, kind, noted_on) VALUES (?,?,?,?)",
        (matter_id, body.strip(), kind, (noted_on or date.today()).isoformat()),
    )
    return int(cursor.lastrowid)


def list_notes(conn: sqlite3.Connection, matter_id: int, *, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matter_notes WHERE matter_id = ? ORDER BY noted_on DESC, id DESC LIMIT ?",
        (matter_id, limit),
    ).fetchall()


def add_document(
    conn: sqlite3.Connection,
    matter_id: int,
    title: str,
    *,
    kind: str | None = None,
    path: str | None = None,
    filed_on: date | None = None,
) -> int:
    if not title.strip():
        raise MatterError("a document needs a title")
    cursor = conn.execute(
        "INSERT INTO matter_documents (matter_id, title, kind, path, filed_on) VALUES (?,?,?,?,?)",
        (matter_id, title.strip(), kind, path, filed_on.isoformat() if filed_on else None),
    )
    return int(cursor.lastrowid)


def list_documents(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matter_documents WHERE matter_id = ? ORDER BY id DESC", (matter_id,)
    ).fetchall()


def log_time(
    conn: sqlite3.Connection,
    matter_id: int,
    minutes: int,
    description: str,
    *,
    worked_on: date | None = None,
    rate: float | None = None,
) -> int:
    if minutes <= 0:
        raise MatterError("time logged must be a positive number of minutes")
    if not description.strip():
        raise MatterError("a time entry needs a description")
    cursor = conn.execute(
        """INSERT INTO time_entries (matter_id, worked_on, minutes, description, rate)
           VALUES (?,?,?,?,?)""",
        (matter_id, (worked_on or date.today()).isoformat(), minutes, description.strip(), rate),
    )
    return int(cursor.lastrowid)


def list_time(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM time_entries WHERE matter_id = ? ORDER BY worked_on DESC, id DESC",
        (matter_id,),
    ).fetchall()


@dataclass
class TimeSummary:
    minutes: int
    hours: float
    billable: float
    unbilled_minutes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes": self.minutes, "hours": self.hours,
            "billable": self.billable, "unbilled_minutes": self.unbilled_minutes,
        }


def time_summary(conn: sqlite3.Connection, matter_id: int) -> TimeSummary:
    rows = list_time(conn, matter_id)
    minutes = sum(int(r["minutes"]) for r in rows)
    unbilled = sum(int(r["minutes"]) for r in rows if not r["billed"])
    billable = sum((r["rate"] or 0.0) * int(r["minutes"]) / 60.0 for r in rows)
    return TimeSummary(
        minutes=minutes, hours=round(minutes / 60.0, 2),
        billable=round(billable, 2), unbilled_minutes=unbilled,
    )


def mark_billed(conn: sqlite3.Connection, entry_ids: Iterable[int]) -> int:
    ids = list(entry_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"UPDATE time_entries SET billed = 1 WHERE id IN ({placeholders})", ids
    )
    return cursor.rowcount


# --------------------------------------------------------------------------
# deadlines
# --------------------------------------------------------------------------

def add_deadline(
    conn: sqlite3.Connection,
    matter_id: int,
    label: str,
    due_on: date,
    *,
    basis: str | None = None,
    verified: bool = False,
) -> int:
    """Pin a deadline to a matter.

    ``verified`` carries through from the limitation calculator: a deadline
    computed from an unverified Schedule article stays marked as such here, so
    the diary never presents a machine-extracted figure as settled.
    """
    cursor = conn.execute(
        """INSERT INTO matter_deadlines (matter_id, label, due_on, basis, verified)
           VALUES (?,?,?,?,?)""",
        (matter_id, label.strip(), due_on.isoformat(), basis, 1 if verified else 0),
    )
    return int(cursor.lastrowid)


def complete_deadline(conn: sqlite3.Connection, deadline_id: int) -> None:
    conn.execute("UPDATE matter_deadlines SET completed = 1 WHERE id = ?", (deadline_id,))


def upcoming_deadlines(
    conn: sqlite3.Connection, user_id: int, *, within_days: int = 30, include_overdue: bool = True
) -> list[sqlite3.Row]:
    """The diary: what falls due soon, across every open file."""
    today = date.today()
    horizon = (today + timedelta(days=within_days)).isoformat()
    floor = "0000-00-00" if include_overdue else today.isoformat()
    return conn.execute(
        """SELECT d.*, m.reference, m.title, m.id AS matter_id
           FROM matter_deadlines d JOIN matters m ON m.id = d.matter_id
           WHERE m.user_id = ? AND d.completed = 0 AND d.due_on BETWEEN ? AND ?
           ORDER BY d.due_on""",
        (user_id, floor, horizon),
    ).fetchall()


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------

def matter_overview(conn: sqlite3.Connection, matter_id: int) -> dict[str, Any]:
    """Everything the file view needs, in one call."""
    matter = get_matter(conn, matter_id)
    if matter is None:
        raise MatterError(f"no matter {matter_id}")
    return {
        "matter": dict(matter),
        "cases": [dict(r) for r in list_cases(conn, matter_id)],
        "notes": [dict(r) for r in list_notes(conn, matter_id, limit=20)],
        "documents": [dict(r) for r in list_documents(conn, matter_id)],
        "time": time_summary(conn, matter_id).to_dict(),
        "deadlines": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM matter_deadlines WHERE matter_id = ? ORDER BY due_on",
                (matter_id,),
            ).fetchall()
        ],
    }


def practice_summary(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Chamber-level counts for the dashboard."""
    counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM matters WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    }
    unbilled = conn.execute(
        """SELECT COALESCE(SUM(t.minutes), 0) AS m FROM time_entries t
           JOIN matters mt ON mt.id = t.matter_id
           WHERE mt.user_id = ? AND t.billed = 0""",
        (user_id,),
    ).fetchone()["m"]
    return {
        "matters": {status: counts.get(status, 0) for status in MATTER_STATUSES},
        "total_matters": sum(counts.values()),
        "unbilled_hours": round(int(unbilled) / 60.0, 2),
        "deadlines_30d": len(upcoming_deadlines(conn, user_id, within_days=30)),
    }
