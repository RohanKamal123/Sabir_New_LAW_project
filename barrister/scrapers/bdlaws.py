"""Bangladesh Code scraping for bdlaws.minlaw.gov.bd.

This is the safest data source in the product: the corpus is finite (~1,675
acts), stable, official, and free. Nothing here is generated — statute answers
are retrieved verbatim, which is why statute lookup can ship on day one while
case-law answers cannot.

Structure of the site:

* ``/laws-of-bangladesh-chronological-index.html`` — every act, with its
  official number and year, linking to ``/act-<id>.html``.
* ``/act-<id>.html`` — an act's table of contents; ``.act-section-name``
  anchors link to ``/act-<id>/section-<sid>.html``, and ``.act-part-group``
  headings carry the Part the following sections belong to.
* ``/act-<id>/section-<sid>.html`` — ``.txt-head`` is the section heading and
  ``.txt-details`` the operative text.

Every page is served as UTF-16, which :func:`barrister.http.decode_html`
handles; reading these bytes as UTF-8 produces mojibake that silently defeats
every selector below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Settings, settings as default_settings
from ..http import PoliteClient

_ACT_HREF = re.compile(r"/act-(\d+)\.html")
_SECTION_HREF = re.compile(r"/act-(\d+)/section-(\d+)\.html")
_WS = re.compile(r"\s+")
# "5. Extension of period in certain cases" -> number "5"
_SECTION_NO = re.compile(r"^\s*(\d+[A-Za-z]*(?:\s*[-–]\s*\d+[A-Za-z]*)?)\s*[.．]\s*")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ").replace("﻿", "")).strip()


@dataclass
class ActRef:
    act_id: str
    title: str
    act_number: str | None
    year: str | None
    url: str


@dataclass
class StatuteSection:
    act_id: str
    section_id: str
    section_no: str | None
    part: str | None
    heading: str
    body: str
    url: str


@dataclass
class Act:
    ref: ActRef
    sections: list[StatuteSection] = field(default_factory=list)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_act_index(html: str, base_url: str | None = None) -> list[ActRef]:
    """Parse the chronological index into act references.

    Each act occupies one row whose three cells are title, official number
    (a Roman numeral, e.g. "XIX") and year, all three linking to the same act.
    """
    base_url = base_url or default_settings.bdlaws_base
    soup = BeautifulSoup(html, "lxml")
    acts: dict[str, ActRef] = {}

    for row in soup.find_all("tr"):
        anchor = row.find("a", href=_ACT_HREF)
        if anchor is None:
            continue
        match = _ACT_HREF.search(anchor["href"])
        if match is None:
            continue
        act_id = match.group(1)
        if act_id in acts:
            continue

        cells = [_clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        title = cells[0] if cells else _clean(anchor.get_text(" ", strip=True))
        acts[act_id] = ActRef(
            act_id=act_id,
            title=title,
            act_number=cells[1] if len(cells) > 1 and cells[1] else None,
            year=cells[2] if len(cells) > 2 and cells[2] else None,
            url=urljoin(base_url, f"/act-{act_id}.html"),
        )
    return list(acts.values())


def parse_act_page(html: str, act_id: str, base_url: str | None = None) -> list[StatuteSection]:
    """Parse an act's table of contents into section stubs (no body text yet).

    Part headings appear as ``.act-part-group`` blocks *between* the section
    links, so we walk the document in order and carry the current Part forward.
    """
    base_url = base_url or default_settings.bdlaws_base
    soup = BeautifulSoup(html, "lxml")
    sections: list[StatuteSection] = []
    current_part: str | None = None

    for node in soup.find_all(class_=["act-part-group", "act-section-name"]):
        classes = node.get("class") or []
        if "act-part-group" in classes:
            current_part = _clean(node.get_text(" ", strip=True)) or None
            continue

        anchor = node.find("a", href=True)
        if anchor is None:
            continue
        match = _SECTION_HREF.search(anchor["href"])
        if match is None:
            continue

        label = _clean(node.get_text(" ", strip=True))
        number_match = _SECTION_NO.match(label)
        sections.append(
            StatuteSection(
                act_id=act_id,
                section_id=match.group(2),
                section_no=number_match.group(1).replace(" ", "") if number_match else None,
                part=current_part,
                heading=_clean(_SECTION_NO.sub("", label)) or label,
                body="",
                url=urljoin(base_url, anchor["href"]),
            )
        )
    return sections


def parse_section_page(html: str) -> tuple[str, str, str | None]:
    """Return ``(heading, body, part)`` from a section page."""
    soup = BeautifulSoup(html, "lxml")
    head = soup.select_one(".txt-head")
    body = soup.select_one(".txt-details")
    part = soup.select_one(".act-part-no")
    return (
        _clean(head.get_text(" ", strip=True)) if head else "",
        _clean(body.get_text(" ", strip=True)) if body else "",
        _clean(part.get_text(" ", strip=True)) if part else None,
    )


def parse_act_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.find("h3")
    if heading:
        return _clean(heading.get_text(" ", strip=True))
    return _clean(soup.title.get_text()) if soup.title else None


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def index_url(settings: Settings | None = None) -> str:
    settings = settings or default_settings
    return urljoin(settings.bdlaws_base, "/laws-of-bangladesh-chronological-index.html")


def fetch_act_index(client: PoliteClient, *, force_refresh: bool = False) -> list[ActRef]:
    response = client.fetch(index_url(client.settings), force_refresh=force_refresh)
    return parse_act_index(response.text, client.settings.bdlaws_base)


def fetch_act(
    client: PoliteClient,
    act: ActRef | str,
    *,
    with_bodies: bool = True,
    force_refresh: bool = False,
) -> Act:
    """Fetch one act and, by default, the full text of each of its sections.

    ``with_bodies=False`` fetches only the table of contents — one request
    instead of one-per-section — which is enough to check whether a cached act
    has gained or lost sections since the last sync.
    """
    settings = client.settings
    if isinstance(act, str):
        act_ref = ActRef(
            act_id=act, title="", act_number=None, year=None,
            url=urljoin(settings.bdlaws_base, f"/act-{act}.html"),
        )
    else:
        act_ref = act

    response = client.fetch(act_ref.url, force_refresh=force_refresh)
    if not act_ref.title:
        act_ref.title = parse_act_title(response.text) or f"Act {act_ref.act_id}"

    sections = parse_act_page(response.text, act_ref.act_id, settings.bdlaws_base)
    if with_bodies:
        for section in sections:
            section_page = client.fetch(section.url, force_refresh=force_refresh)
            heading, body, part = parse_section_page(section_page.text)
            section.body = body
            if heading:
                section.heading = heading
            if part and not section.part:
                section.part = part

    return Act(ref=act_ref, sections=sections)


def iter_acts(
    client: PoliteClient, *, limit: int | None = None, force_refresh: bool = False
) -> Iterator[Act]:
    """Walk the whole Code. Slow by design — this is a one-off corpus build."""
    for position, act_ref in enumerate(fetch_act_index(client, force_refresh=force_refresh)):
        if limit is not None and position >= limit:
            return
        yield fetch_act(client, act_ref, force_refresh=force_refresh)
