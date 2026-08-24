"""Cause list scraping for supremecourt.gov.bd.

The site has no API, but it does have a stable, guessable URL shape:

* bench list for a division and date
  ``?page=bench_list.php&menu=00&div_id=2&lang=``
  Each row links to that bench's list with ``court_id``, ``bench_id`` and
  ``date1=DD/MM/YYYY``.
* the printable per-bench list
  ``cause_list_print_without_result.php?court_id=..&date1=..&bench_id=..``
  which is the same data as the framed page without the site chrome, so it is
  what we parse.

Parsing notes that cost real debugging time:

* A row with an empty serial is a *connected matter* — the previous numbered
  entry ended with "(with)" and these rows are heard along with it. They must
  inherit the parent serial or they get dropped from a barrister's alerts.
* Single-cell rows are section headings ("For Hearing", "For Judgment",
  "Fixing a date of hearing"), which matter enormously: being at serial 3
  "For Judgment" is a very different day than serial 3 "For Hearing".
* Case numbers are not plain integers — ``Civil Rule 832(FM)/2006`` is real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from ..config import Settings, settings as default_settings
from ..http import PoliteClient

DIVISIONS = {1: "Appellate Division", 2: "High Court Division"}

# "First Misc Appeal 153/2007 Tangail (with)" ->
#   type="First Misc Appeal" number="153" year="2007" rest=" Tangail (with)"
_CASE_REF = re.compile(
    r"^(?P<type>.+?)\s+(?P<number>\d+\s*\([^)]*\)|\d+)\s*/\s*(?P<year>\d{4})(?P<rest>.*)$"
)
_ADVOCATE = re.compile(r"\[\s*Adv\s*[:.]?\s*(?P<adv>[^\]]*)\]", re.IGNORECASE)
_VS = re.compile(r"\s+(?:-\s*)?v[s/]\.?(?:\s*-)?\s+", re.IGNORECASE)
_PARENS = re.compile(r"\(([^)]*)\)")
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", text.replace("﻿", "")).strip()


@dataclass
class Bench:
    """One court sitting on one day."""

    division_id: int
    court_id: str
    bench_id: str
    court_name: str
    judges: str
    jurisdiction: str
    list_date: str  # DD/MM/YYYY, as the site expresses it
    url: str

    @property
    def division(self) -> str:
        return DIVISIONS.get(self.division_id, f"Division {self.division_id}")


@dataclass
class CauseListEntry:
    """One case listed before one bench on one day."""

    list_date: str
    division: str
    court_id: str | None
    bench_id: str | None
    court_name: str | None
    judges: str | None
    section: str | None
    serial: int | None
    case_type: str | None
    case_number: str | None
    case_year: str | None
    district: str | None
    parties: str
    petitioner: str | None
    respondent: str | None
    advocates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    connected_to: int | None = None
    raw: str = ""

    @property
    def case_ref(self) -> str:
        if not self.case_type:
            return self.raw
        ref = f"{self.case_type} {self.case_number}/{self.case_year}"
        return f"{ref} {self.district}" if self.district else ref

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_case_reference(text: str) -> dict[str, Any]:
    """Split ``First Misc Appeal 153/2007 Tangail (with)`` into components."""
    cleaned = _clean(text)
    match = _CASE_REF.match(cleaned)
    if not match:
        return {
            "case_type": None,
            "case_number": None,
            "case_year": None,
            "district": None,
            "notes": [],
        }

    rest = match.group("rest") or ""
    notes = [_clean(n) for n in _PARENS.findall(rest) if _clean(n)]
    district = _clean(_PARENS.sub(" ", rest)) or None

    return {
        "case_type": _clean(match.group("type")),
        "case_number": _clean(match.group("number")).replace(" ", ""),
        "case_year": match.group("year"),
        "district": district,
        "notes": notes,
    }


def parse_parties(text: str) -> dict[str, Any]:
    """Pull advocates out of ``[Adv : ...]`` and split petitioner vs respondent."""
    cleaned = _clean(text)
    advocates: list[str] = []
    for raw in _ADVOCATE.findall(cleaned):
        # "Mr. X with Mr. Y with Mr. Z...For the appellant." -> individual names
        for name in re.split(r"\s+with\s+|\s*;\s*", raw):
            name = _clean(re.sub(r"\.{2,}.*$", "", name))
            name = _clean(re.sub(r"[-,]?\s*(?:Adv\.?,?\s*)?[Ff]or\s+(?:the\s+)?.*$", "", name))
            name = _clean(re.sub(r"[,\s]+(?:adv|advocate)\.?$", "", name, flags=re.IGNORECASE))
            name = name.strip(" .,-")
            if name:
                advocates.append(name)

    without_adv = _clean(_ADVOCATE.sub(" ", cleaned))
    parts = _VS.split(without_adv, maxsplit=1)
    petitioner = _clean(parts[0]) or None
    respondent = _clean(parts[1]) if len(parts) > 1 else None

    return {
        "petitioner": petitioner,
        "respondent": respondent,
        "advocates": list(dict.fromkeys(advocates)),  # de-dupe, keep order
    }


# Link labels the site repeats in every bench row; they are navigation, not data.
_ROW_CHROME = re.compile(r"\s*(?:Printable View|Page by page|Print)\s*", re.IGNORECASE)


def _row_cells(row: Tag) -> list[str]:
    return [_clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]


def _strip_chrome(text: str) -> str:
    return _clean(_ROW_CHROME.sub(" ", text))


def parse_bench_list(html: str, division_id: int, base_url: str | None = None) -> list[Bench]:
    """Parse the per-division bench list into :class:`Bench` records."""
    base_url = base_url or default_settings.supreme_court_base
    soup = BeautifulSoup(html, "lxml")
    benches: list[Bench] = []
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "cause_list.php" not in href:
            continue
        query = parse_qs(urlparse(href).query)
        court_id = (query.get("court_id") or [""])[0]
        bench_id = (query.get("bench_id") or [""])[0]
        list_date = (query.get("date1") or [""])[0]
        if not (court_id and bench_id) or (court_id, bench_id) in seen:
            continue
        seen.add((court_id, bench_id))

        row = anchor.find_parent("tr")
        cells = _row_cells(row) if row else []
        judges = _strip_chrome(cells[2]) if len(cells) > 2 else ""
        jurisdiction = _strip_chrome(cells[3]) if len(cells) > 3 else ""

        benches.append(
            Bench(
                division_id=int((query.get("div_id") or [division_id])[0]),
                court_id=court_id,
                bench_id=bench_id,
                court_name=_clean(anchor.get_text(" ", strip=True)),
                judges=judges,
                jurisdiction=jurisdiction,
                list_date=list_date,
                url=urljoin(base_url, href),
            )
        )
    return benches


def _parse_header(soup: BeautifulSoup) -> dict[str, str | None]:
    """Read division / judges / court / date off the printable list header."""
    tables = soup.find_all("table")
    if not tables:
        return {}
    lines = [
        _clean(row.get_text(" ", strip=True))
        for row in tables[0].find_all("tr")
        if _clean(row.get_text(" ", strip=True))
    ]

    header: dict[str, str | None] = {
        "division": None,
        "judges": None,
        "list_date": None,
        "court_name": None,
    }
    for line in lines:
        if "Division" in line and header["division"] is None:
            for name in DIVISIONS.values():
                if name in line:
                    header["division"] = name
                    break
        if line.startswith("Justice") and header["judges"] is None:
            header["judges"] = line
        if "Date" in line and header["list_date"] is None:
            date_match = re.search(r"(\d{2}/\d{2}/\d{4})", line)
            if date_match:
                header["list_date"] = date_match.group(1)
            court_match = re.search(r"\[([^\]]+)\]", line)
            if court_match:
                header["court_name"] = _clean(court_match.group(1))
    return header


def parse_cause_list(
    html: str,
    *,
    bench: Bench | None = None,
    list_date: str | None = None,
    division: str | None = None,
) -> list[CauseListEntry]:
    """Parse a printable per-bench cause list page."""
    soup = BeautifulSoup(html, "lxml")
    header = _parse_header(soup)

    resolved_date = list_date or (bench.list_date if bench else None) or header.get("list_date")
    resolved_div = division or (bench.division if bench else None) or header.get("division")
    court_name = (bench.court_name if bench else None) or header.get("court_name")
    judges = (bench.judges if bench else None) or header.get("judges")

    entries: list[CauseListEntry] = []
    section: str | None = None
    last_serial: int | None = None

    tables = soup.find_all("table")
    body = tables[1] if len(tables) > 1 else (tables[0] if tables else None)
    if body is None:
        return entries

    for row in body.find_all("tr"):
        cells = _row_cells(row)
        non_empty = [c for c in cells if c]

        # A one-cell row is a section heading ("For Judgment", "For Hearing").
        if len(cells) <= 2 or (len(non_empty) == 1 and not cells[0].isdigit()):
            if len(non_empty) == 1:
                section = non_empty[0]
            continue

        serial_text, case_text = cells[0], cells[1]
        parties_text = cells[2] if len(cells) > 2 else ""
        if not case_text:
            continue

        if serial_text.isdigit():
            serial: int | None = int(serial_text)
            last_serial = serial
            connected_to = None
        else:
            # Connected matter listed "(with)" the preceding serial.
            serial = last_serial
            connected_to = last_serial

        ref = parse_case_reference(case_text)
        parties = parse_parties(parties_text)

        entries.append(
            CauseListEntry(
                list_date=resolved_date or "",
                division=resolved_div or "",
                court_id=bench.court_id if bench else None,
                bench_id=bench.bench_id if bench else None,
                court_name=court_name,
                judges=judges,
                section=section,
                serial=serial,
                case_type=ref["case_type"],
                case_number=ref["case_number"],
                case_year=ref["case_year"],
                district=ref["district"],
                parties=parties_text,
                petitioner=parties["petitioner"],
                respondent=parties["respondent"],
                advocates=parties["advocates"],
                notes=ref["notes"],
                connected_to=connected_to,
                raw=f"{case_text} | {parties_text}",
            )
        )

    return entries


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def bench_list_url(division_id: int, settings: Settings | None = None) -> str:
    settings = settings or default_settings
    page = "bench_list_app.php" if division_id == 1 else "bench_list.php"
    menu = "01" if division_id == 1 else "00"
    return urljoin(settings.supreme_court_base, f"?page={page}&menu={menu}&div_id={division_id}&lang=")


def printable_list_url(bench: Bench, settings: Settings | None = None) -> str:
    settings = settings or default_settings
    return urljoin(
        settings.supreme_court_base,
        "cause_list_print_without_result.php"
        f"?court_id={bench.court_id}&date1={bench.list_date}&bench_id={bench.bench_id}",
    )


def fetch_benches(
    client: PoliteClient, division_id: int, *, force_refresh: bool = False
) -> list[Bench]:
    url = bench_list_url(division_id, client.settings)
    response = client.fetch(url, force_refresh=force_refresh)
    return parse_bench_list(response.text, division_id, client.settings.supreme_court_base)


def fetch_cause_list(
    client: PoliteClient, bench: Bench, *, force_refresh: bool = False
) -> list[CauseListEntry]:
    url = printable_list_url(bench, client.settings)
    response = client.fetch(url, force_refresh=force_refresh)
    return parse_cause_list(response.text, bench=bench)


def fetch_all(
    client: PoliteClient,
    *,
    divisions: Iterable[int] = (1, 2),
    force_refresh: bool = False,
) -> Iterator[CauseListEntry]:
    """Sweep every bench in the given divisions, yielding entries as they parse.

    Yielding rather than returning a list means a partial sweep still delivers
    alerts for the benches already read if a later page times out.
    """
    for division_id in divisions:
        for bench in fetch_benches(client, division_id, force_refresh=force_refresh):
            yield from fetch_cause_list(client, bench, force_refresh=force_refresh)


def today_ddmmyyyy(when: date | None = None) -> str:
    return (when or date.today()).strftime("%d/%m/%Y")
