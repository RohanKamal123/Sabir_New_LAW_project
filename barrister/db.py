"""SQLite storage.

One file, no ORM, no migration framework. The schema is small and the whole
point of Tier 0 is that a solo practitioner can run this on a cheap box and
copy the database off as a backup.

Snapshot tables store a content hash per (source, key) so that "did anything
change since yesterday?" is a hash comparison rather than a re-parse.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings as default_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    chamber       TEXT,
    telegram_chat_id TEXT,
    email         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What a barrister wants to be told about. `kind` is 'advocate' (their name as
-- it appears in the cause list), 'party', or 'case' (a case number).
CREATE TABLE IF NOT EXISTS watches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('advocate', 'party', 'case')),
    value      TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, kind, value)
);

-- One row per case appearing on one bench's list on one day.
CREATE TABLE IF NOT EXISTS cause_list_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    list_date    TEXT NOT NULL,
    division     TEXT NOT NULL,
    court_id     TEXT,
    bench_id     TEXT,
    court_name   TEXT,
    judges       TEXT,
    section      TEXT,
    serial       INTEGER,
    case_type    TEXT,
    case_number  TEXT,
    case_year    TEXT,
    district     TEXT,
    parties      TEXT,
    petitioner   TEXT,
    respondent   TEXT,
    advocates    TEXT,
    raw          TEXT,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (list_date, bench_id, serial, case_type, case_number, case_year)
);
CREATE INDEX IF NOT EXISTS idx_cle_date ON cause_list_entries(list_date);
CREATE INDEX IF NOT EXISTS idx_cle_case ON cause_list_entries(case_type, case_number, case_year);

-- Alerts already delivered, so a re-run of the cron job is idempotent.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    watch_id     INTEGER REFERENCES watches(id) ON DELETE SET NULL,
    dedupe_key   TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    delivered_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, dedupe_key)
);

-- A matter is the file a barrister keeps: one brief, one client, one or more
-- court cases. Cause-list hits and status changes attach here, which is what
-- turns four separate tools into one desk.
CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    phone      TEXT,
    email      TEXT,
    address    TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_user ON clients(user_id);

CREATE TABLE IF NOT EXISTS matters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id    INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    reference    TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'reserved', 'disposed', 'closed')),
    court        TEXT,
    opened_on    TEXT NOT NULL DEFAULT (date('now')),
    closed_on    TEXT,
    fee_agreed   REAL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, reference)
);
CREATE INDEX IF NOT EXISTS idx_matters_user ON matters(user_id, status);

-- The court cases belonging to a matter. `watch_id` links the file to the
-- cause-list watch, so an alert can name the matter, not just the case number.
CREATE TABLE IF NOT EXISTS matter_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    case_type   TEXT NOT NULL,
    case_number TEXT NOT NULL,
    case_year   TEXT NOT NULL,
    division_id INTEGER,
    watch_id    INTEGER REFERENCES watches(id) ON DELETE SET NULL,
    UNIQUE (matter_id, case_type, case_number, case_year)
);
CREATE INDEX IF NOT EXISTS idx_matter_cases_ref
    ON matter_cases(case_type, case_number, case_year);

CREATE TABLE IF NOT EXISTS matter_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id  INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note'
               CHECK (kind IN ('note', 'attendance', 'advice', 'hearing')),
    noted_on   TEXT NOT NULL DEFAULT (date('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_matter ON matter_notes(matter_id);

CREATE TABLE IF NOT EXISTS matter_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    kind        TEXT,
    path        TEXT,
    filed_on    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_docs_matter ON matter_documents(matter_id);

CREATE TABLE IF NOT EXISTS time_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    worked_on   TEXT NOT NULL DEFAULT (date('now')),
    minutes     INTEGER NOT NULL CHECK (minutes > 0),
    description TEXT NOT NULL,
    rate        REAL,
    billed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_time_matter ON time_entries(matter_id);

-- Deadlines computed by the limitation calculator, pinned to a matter so they
-- show up on the diary instead of living in a terminal's scrollback.
CREATE TABLE IF NOT EXISTS matter_deadlines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    due_on      TEXT NOT NULL,
    basis       TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    completed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deadlines_due ON matter_deadlines(due_on, completed);

-- Generic snapshot store used by the case-status differ.
CREATE TABLE IF NOT EXISTS snapshots (
    source       TEXT NOT NULL,
    key          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    captured_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS statutes (
    act_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    act_number   TEXT,
    year         TEXT,
    url          TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS statute_sections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    act_id       TEXT NOT NULL REFERENCES statutes(act_id) ON DELETE CASCADE,
    section_id   TEXT NOT NULL,
    section_no   TEXT,
    part         TEXT,
    heading      TEXT,
    body         TEXT NOT NULL,
    url          TEXT NOT NULL,
    UNIQUE (act_id, section_id)
);

-- Exact-text search. FTS5 keeps the "no generation, no hallucination" promise:
-- statute answers are retrieved verbatim, never synthesised.
-- A regular (not contentless) FTS5 table: it keeps its own copy of the text so
-- re-indexing a section is a plain DELETE. A contentless table would require
-- replaying the original column values to delete a row, and getting that wrong
-- silently corrupts the index (bm25 starts returning NULL).
CREATE VIRTUAL TABLE IF NOT EXISTS statute_fts USING fts5(
    heading, body, act_title,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS statute_fts_map (
    rowid      INTEGER PRIMARY KEY,
    section_pk INTEGER NOT NULL UNIQUE REFERENCES statute_sections(id) ON DELETE CASCADE
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_settings.db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# -- snapshot helpers ---------------------------------------------------

def record_snapshot(
    conn: sqlite3.Connection, source: str, key: str, payload: Any
) -> tuple[bool, dict | None]:
    """Store a snapshot; report whether it changed and what it replaced.

    Returns ``(changed, previous_payload)``. A first-ever snapshot counts as
    changed with ``previous_payload`` of ``None`` — callers decide whether a
    first sighting is worth an alert.
    """
    import hashlib

    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    row = conn.execute(
        "SELECT content_hash, payload FROM snapshots WHERE source = ? AND key = ?",
        (source, key),
    ).fetchone()

    if row is not None and row["content_hash"] == digest:
        return False, json.loads(row["payload"])

    previous = json.loads(row["payload"]) if row is not None else None
    conn.execute(
        """INSERT INTO snapshots (source, key, content_hash, payload, captured_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(source, key) DO UPDATE SET
             content_hash = excluded.content_hash,
             payload      = excluded.payload,
             captured_at  = excluded.captured_at""",
        (source, key, digest, blob),
    )
    return True, previous
