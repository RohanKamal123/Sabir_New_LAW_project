"""Case status and history for supremecourt.gov.bd.

The public "Case Search" page is a JS-driven form, but underneath it are three
plain POST/GET endpoints, which is what we talk to:

* ``body/case_category_combo.php``  POST division_id -> case natures
* ``body/case_type_combo.php``      POST nature_id   -> case types
* ``case_history/case_history.php`` GET  div_id, case_type_id, case_number, year

The two combo endpoints only populate dropdowns, and their contents change
rarely, so the full 110-entry registry is shipped in ``data/case_types.json``.
That turns a status check into a single request instead of three, and means a
user can type "Writ Petition" rather than looking up ``case_type_id=13``.

The history page is the valuable one: it carries every listing the case has
ever had, with bench, date and — critically — the *result* of that hearing
("Adjourned for this week", "Not today", "Allowed"). Diffing that list is how
the tracker knows something happened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..config import Settings, settings as default_settings
from ..http import PoliteClient

_DATA = Path(__file__).resolve().parent.parent / "data" / "case_types.json"
_WS = re.compile(r"\s+")
# "24/08/26 Annex Building Court No. 18 Justice Sheikh Abdul Awal, Justice ..."
_HEARING_HEAD = re.compile(r"Hearing\s+(?P<no>\d+)\s*:?\s*$", re.IGNORECASE)
_DATE = re.compile(r"(\d{2}/\d{2}/\d{2,4})")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ").replace("﻿", "")).strip()


# --------------------------------------------------------------------------
# case type registry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseType:
    division_id: int
    division: str
    nature_id: int
    nature: str
    case_type_id: int
    name: str


@lru_cache(maxsize=1)
def load_case_types() -> list[CaseType]:
    """Every (division, nature, case type) the court's own dropdowns offer."""
    if not _DATA.exists():
        return []
    registry = json.loads(_DATA.read_text(encoding="utf-8"))
    out: list[CaseType] = []
    for entry in registry.values():
        for nature in entry["natures"]:
            for case_type in nature["case_types"]:
                out.append(
                    CaseType(
                        division_id=entry["division_id"],
                        division=entry["division"],
                        nature_id=nature["nature_id"],
                        nature=nature["name"],
                        case_type_id=case_type["case_type_id"],
                        name=case_type["name"],
                    )
                )
    return out


def find_case_type(name: str, division_id: int | None = None) -> CaseType | None:
    """Resolve a human-typed case type name, e.g. "writ petition".

    Exact (case-folded) match wins; otherwise the shortest name containing the
    query, so "writ petition" does not resolve to "In re : VC Writ Petition".
    """
    query = _clean(name).casefold()
    if not query:
        return None
    candidates = [
        ct for ct in load_case_types()
        if division_id is None or ct.division_id == division_id
    ]
    for candidate in candidates:
        if candidate.name.casefold() == query:
            return candidate
    partial = [c for c in candidates if query in c.name.casefold()]
    return min(partial, key=lambda c: len(c.name)) if partial else None


# --------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------

@dataclass
class Hearing:
    number: int | None
    date: str | None
    court: str | None
    judges: str | None
    result: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseStatus:
    case_type: str | None
    case_number: str | None
    case_year: str | None
    division_id: int | None
    petitioner: str | None = None
    respondent: str | None = None
    petitioner_lawyer: str | None = None
    respondent_lawyer: str | None = None
    related_case: str | None = None
    judgment_files: str | None = None
    hearings: list[Hearing] = field(default_factory=list)
    source_url: str = ""

    @property
    def case_ref(self) -> str:
        if not self.case_type:
            return "unknown case"
        return f"{self.case_type} {self.case_number}/{self.case_year}"

    @property
    def latest(self) -> Hearing | None:
        return self.hearings[0] if self.hearings else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hearings"] = [h.to_dict() for h in self.hearings]
        return data


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _split_court_and_judges(text: str) -> tuple[str | None, str | None, str | None]:
    """``"24/08/26 Annex Building Court No. 18 Justice A, Justice B"``."""
    cleaned = _clean(text)
    date_match = _DATE.search(cleaned)
    hearing_date = date_match.group(1) if date_match else None
    remainder = cleaned[date_match.end():].strip() if date_match else cleaned

    judge_at = remainder.find("Justice")
    if judge_at >= 0:
        return hearing_date, _clean(remainder[:judge_at]) or None, _clean(remainder[judge_at:]) or None
    return hearing_date, _clean(remainder) or None, None


def _labelled_rows(soup: BeautifulSoup) -> dict[str, str]:
    """Collect two-cell ``label: value`` rows from the whole page."""
    found: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) != 2:
            continue
        label = _clean(cells[0].get_text(" ", strip=True)).rstrip(":").strip().casefold()
        if label and label not in found:
            found[label] = _clean(cells[1].get_text(" ", strip=True))
    return found


def parse_case_history(html: str, *, source_url: str = "") -> CaseStatus:
    """Parse ``case_history.php`` into a :class:`CaseStatus`."""
    soup = BeautifulSoup(html, "lxml")
    page_text = _clean(soup.get_text(" ", strip=True))

    header = re.search(
        r"Case Number\s*:\s*(?P<type>.+?)\s+(?P<num>\d+)\s*/\s*(?P<year>\d{4})", page_text
    )
    status = CaseStatus(
        case_type=_clean(header.group("type")) if header else None,
        case_number=header.group("num") if header else None,
        case_year=header.group("year") if header else None,
        division_id=None,
        source_url=source_url,
    )

    # -- hearings: alternating "Hearing N :" / "Result :" row pairs --------
    hearings: list[Hearing] = []
    pending: Hearing | None = None
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) != 2:
            continue
        label = _clean(cells[0].get_text(" ", strip=True))
        value = _clean(cells[1].get_text(" ", strip=True))

        head = _HEARING_HEAD.match(label)
        if head:
            if pending is not None:
                hearings.append(pending)
            hearing_date, court, judges = _split_court_and_judges(value)
            pending = Hearing(
                number=int(head.group("no")), date=hearing_date,
                court=court, judges=judges, result=None,
            )
        elif label.rstrip(":").strip().casefold() == "result" and pending is not None:
            pending.result = value or None
            hearings.append(pending)
            pending = None
    if pending is not None:
        hearings.append(pending)

    # De-duplicate: the page nests the same history table inside an outer one.
    seen: set[tuple] = set()
    unique: list[Hearing] = []
    for hearing in hearings:
        key = (hearing.number, hearing.date, hearing.court, hearing.result)
        if key not in seen:
            seen.add(key)
            unique.append(hearing)
    status.hearings = sorted(
        unique, key=lambda h: (h.number is None, -(h.number or 0))
    )

    # -- basic information -------------------------------------------------
    rows = _labelled_rows(soup)

    def _split_lawyer(value: str | None) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        parts = re.split(r"\bLawyer\s*:", value, maxsplit=1)
        party = _clean(parts[0]) or None
        lawyer = _clean(parts[1]) if len(parts) > 1 else None
        return party, (lawyer or None)

    status.petitioner, status.petitioner_lawyer = _split_lawyer(rows.get("petitioner"))
    status.respondent, status.respondent_lawyer = _split_lawyer(rows.get("respondent"))
    related = rows.get("related case info")
    status.related_case = related if related and related.strip(" /") else None
    status.judgment_files = rows.get("files")

    return status


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def case_history_url(
    *,
    division_id: int,
    case_type_id: int,
    case_number: str | int,
    year: str | int,
    settings: Settings | None = None,
) -> str:
    settings = settings or default_settings
    query = urlencode(
        {
            "div_id": division_id,
            "case_type_id": case_type_id,
            "case_number": case_number,
            "year": year,
            "tender_no": "",
        }
    )
    return urljoin(settings.supreme_court_base, f"case_history/case_history.php?{query}")


def fetch_case_status(
    client: PoliteClient,
    *,
    case_type: str,
    case_number: str | int,
    year: str | int,
    division_id: int | None = None,
    force_refresh: bool = False,
) -> CaseStatus:
    """Look up one case by human-readable type name."""
    resolved = find_case_type(case_type, division_id)
    if resolved is None:
        raise ValueError(
            f"unknown case type {case_type!r}; see barrister.scrapers.case_status.load_case_types()"
        )
    url = case_history_url(
        division_id=resolved.division_id,
        case_type_id=resolved.case_type_id,
        case_number=case_number,
        year=year,
        settings=client.settings,
    )
    response = client.fetch(url, force_refresh=force_refresh)
    status = parse_case_history(response.text, source_url=url)
    status.division_id = resolved.division_id
    if not status.case_type:
        status.case_type = resolved.name
        status.case_number = str(case_number)
        status.case_year = str(year)
    return status
