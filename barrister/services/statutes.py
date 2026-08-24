"""Statute storage and exact-text search.

Deliberately retrieval-only. Every result is a verbatim span of the official
Bangladesh Code with the URL it came from, so a barrister can click through and
confirm. No model sees this text on the way to the user, which is what makes
statute lookup shippable while grounded case-law answers are not.

Search is SQLite FTS5 with an external-content-free index, plus a small
mapping table so a hit maps back to the row it came from.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..scrapers.bdlaws import Act, ActRef, StatuteSection

_FTS_SPECIAL = re.compile(r'["*():^-]')
# "s. 5 of the Limitation Act" / "section 12(2)" / "art 3"
_SECTION_QUERY = re.compile(
    r"^\s*(?:s|sec|section|art|article)\.?\s*(?P<no>\d+[A-Za-z]*)\s*(?:\(\d+\))?\s*(?:of\s+(?:the\s+)?(?P<act>.+?))?\s*$",
    re.IGNORECASE,
)


@dataclass
class SearchHit:
    act_id: str
    act_title: str
    section_no: str | None
    part: str | None
    heading: str
    body: str
    url: str
    snippet: str
    rank: float

    @property
    def citation(self) -> str:
        """How a barrister would write this in a filing."""
        section = f"section {self.section_no}" if self.section_no else self.heading
        return f"{section}, {self.act_title}"


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def store_act(conn: sqlite3.Connection, act: Act) -> int:
    """Upsert an act and its sections, keeping the FTS index in step."""
    ref: ActRef = act.ref
    conn.execute(
        """INSERT INTO statutes (act_id, title, act_number, year, url)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(act_id) DO UPDATE SET
             title = excluded.title, act_number = excluded.act_number,
             year = excluded.year, url = excluded.url,
             fetched_at = datetime('now')""",
        (ref.act_id, ref.title, ref.act_number, ref.year, ref.url),
    )

    stored = 0
    for section in act.sections:
        stored += _store_section(conn, section, ref.title)
    return stored


def _store_section(conn: sqlite3.Connection, section: StatuteSection, act_title: str) -> int:
    conn.execute(
        """INSERT INTO statute_sections (act_id, section_id, section_no, part, heading, body, url)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(act_id, section_id) DO UPDATE SET
             section_no = excluded.section_no, part = excluded.part,
             heading = excluded.heading, body = excluded.body, url = excluded.url""",
        (
            section.act_id, section.section_id, section.section_no,
            section.part, section.heading, section.body, section.url,
        ),
    )
    row = conn.execute(
        "SELECT id FROM statute_sections WHERE act_id = ? AND section_id = ?",
        (section.act_id, section.section_id),
    ).fetchone()
    section_pk = int(row["id"])

    # Re-index: drop any previous FTS row for this section, then insert fresh.
    existing = conn.execute(
        "SELECT rowid FROM statute_fts_map WHERE section_pk = ?", (section_pk,)
    ).fetchone()
    if existing is not None:
        conn.execute("DELETE FROM statute_fts WHERE rowid = ?", (existing["rowid"],))
        conn.execute("DELETE FROM statute_fts_map WHERE rowid = ?", (existing["rowid"],))

    cursor = conn.execute(
        "INSERT INTO statute_fts (heading, body, act_title) VALUES (?, ?, ?)",
        (section.heading, section.body, act_title),
    )
    conn.execute(
        "INSERT INTO statute_fts_map (rowid, section_pk) VALUES (?, ?)",
        (int(cursor.lastrowid), section_pk),
    )
    return 1


def store_acts(conn: sqlite3.Connection, acts: Iterable[Act]) -> int:
    return sum(store_act(conn, act) for act in acts)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _fts_query(text: str) -> str:
    """Turn a user phrase into a safe FTS5 MATCH expression."""
    cleaned = _FTS_SPECIAL.sub(" ", text)
    terms = [t for t in cleaned.split() if t]
    return " OR ".join(f'"{t}"' for t in terms)


def lookup_section(
    conn: sqlite3.Connection, section_no: str, act_query: str | None = None
) -> list[SearchHit]:
    """Exact section lookup: "section 5 of the Limitation Act"."""
    sql = """SELECT s.*, a.title AS act_title FROM statute_sections s
             JOIN statutes a ON a.act_id = s.act_id
             WHERE s.section_no = ?"""
    params: list[object] = [section_no]
    if act_query:
        sql += " AND a.title LIKE ?"
        params.append(f"%{act_query.strip()}%")
    sql += " ORDER BY a.year, a.title LIMIT 20"

    return [
        SearchHit(
            act_id=row["act_id"], act_title=row["act_title"], section_no=row["section_no"],
            part=row["part"], heading=row["heading"], body=row["body"], url=row["url"],
            snippet=row["body"][:300], rank=0.0,
        )
        for row in conn.execute(sql, params).fetchall()
    ]


def search(conn: sqlite3.Connection, query: str, *, limit: int = 10) -> list[SearchHit]:
    """Search the Code.

    A query that reads like a citation ("s. 5 of the Limitation Act") is
    answered by exact lookup first, because a barrister who names a section
    wants *that* section, not the ten sections that mention it.
    """
    citation = _SECTION_QUERY.match(query)
    if citation:
        hits = lookup_section(conn, citation.group("no"), citation.group("act"))
        if hits:
            return hits[:limit]

    match_expr = _fts_query(query)
    if not match_expr:
        return []

    rows = conn.execute(
        """SELECT m.section_pk AS section_pk,
                  bm25(statute_fts, 4.0, 1.0, 2.0) AS rank,
                  snippet(statute_fts, 1, '', '', ' … ', 24) AS snip
           FROM statute_fts
           JOIN statute_fts_map m ON m.rowid = statute_fts.rowid
           WHERE statute_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (match_expr, limit),
    ).fetchall()

    hits: list[SearchHit] = []
    for row in rows:
        detail = conn.execute(
            """SELECT s.*, a.title AS act_title FROM statute_sections s
               JOIN statutes a ON a.act_id = s.act_id WHERE s.id = ?""",
            (row["section_pk"],),
        ).fetchone()
        if detail is None:
            continue
        hits.append(
            SearchHit(
                act_id=detail["act_id"], act_title=detail["act_title"],
                section_no=detail["section_no"], part=detail["part"],
                heading=detail["heading"], body=detail["body"], url=detail["url"],
                snippet=row["snip"] or detail["body"][:300],
                rank=float(row["rank"]) if row["rank"] is not None else 0.0,
            )
        )
    return hits


def corpus_stats(conn: sqlite3.Connection) -> dict[str, int]:
    acts = conn.execute("SELECT COUNT(*) AS n FROM statutes").fetchone()["n"]
    sections = conn.execute("SELECT COUNT(*) AS n FROM statute_sections").fetchone()["n"]
    return {"acts": acts, "sections": sections}
