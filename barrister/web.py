"""Server-rendered web UI.

Server-rendered on purpose. The whole product is a few hundred rows of SQLite
and some scraped HTML; a single-page app would add a build step, a second set
of models and a loading spinner to a page that renders in one query. It also
means the cause list prints correctly, which matters — barristers print things
and take them to court.

Routes mount onto the same FastAPI app as the JSON API (:mod:`barrister.api`),
so both surfaces share exactly one implementation of the matching, diffing and
limitation logic.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import connect, init_db
from .http import PoliteClient
from .scrapers import cause_list as cause_list_scraper
from .services import drafting, limitation, matters, statutes

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "web"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
router = APIRouter()

PROCEEDINGS = [
    ("suit", "Suit"),
    ("appeal", "Appeal"),
    ("leave_to_appeal", "Application for leave to appeal"),
    ("review", "Review of judgment"),
    ("application", "Application"),
]


def get_conn():
    conn = connect()
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def current_user(conn) -> Any:
    """The barrister using this install.

    Single-tenant by design: this runs on one barrister's machine or their
    chamber's box. Multi-tenancy would mean authentication, sessions and a
    password reset flow, none of which earns its keep before there are users.
    """
    return conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()


def _base_context(request: Request, conn, nav: str) -> dict[str, Any]:
    today = date.today()
    return {
        "request": request,
        "nav": nav,
        "user": current_user(conn),
        "today": today.strftime("%d/%m/%Y"),
        "long_date": today.strftime("%A, %d %B %Y"),
    }


def _days_left(due_on: str) -> int:
    return (date.fromisoformat(due_on) - date.today()).days


def _decorate_deadlines(rows) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["days_left"] = _days_left(item["due_on"])
        out.append(item)
    return out


def _lines(value: str | None) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# today
# --------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def page_today(request: Request, conn=Depends(get_conn)):
    context = _base_context(request, conn, "today")
    user = context["user"]
    if user is None:
        context.update(listings=[], listings_by_court=[], deadlines=[], urgent_count=0,
                       summary=matters.practice_summary(conn, 0))
        return templates.TemplateResponse(request, "today.html", context)

    listed = date.today().strftime("%d/%m/%Y")
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

    listings: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        matter = matters.matter_for_case(
            conn, int(user["id"]),
            case_type=entry["case_type"], case_number=entry["case_number"],
            case_year=entry["case_year"],
        )
        entry["matter_reference"] = matter["reference"] if matter else None
        entry["matter_id"] = matter["id"] if matter else None
        listings.append(entry)

    by_court: dict[str, list[dict[str, Any]]] = {}
    for entry in listings:
        by_court.setdefault(entry["court_name"] or "", []).append(entry)

    deadlines = _decorate_deadlines(matters.upcoming_deadlines(conn, int(user["id"])))
    context.update(
        listings=listings,
        listings_by_court=list(by_court.items()),
        deadlines=deadlines,
        urgent_count=sum(1 for d in deadlines if d["days_left"] <= 7),
        summary=matters.practice_summary(conn, int(user["id"])),
    )
    return templates.TemplateResponse(request, "today.html", context)


# --------------------------------------------------------------------------
# cause list
# --------------------------------------------------------------------------

@router.get("/cause-list", response_class=HTMLResponse)
def page_cause_list(
    request: Request, division: int = 2, bench_id: str | None = None, conn=Depends(get_conn)
):
    context = _base_context(request, conn, "causelist")
    context.update(
        division=division,
        division_name=cause_list_scraper.DIVISIONS.get(division, ""),
        bench_id=bench_id, benches=[], entries=[], error=None,
        court_name=None, judges=None, list_date=None,
    )

    try:
        with PoliteClient() as client:
            benches = cause_list_scraper.fetch_benches(client, division)
            context["benches"] = benches
            if benches:
                context["list_date"] = benches[0].list_date

            if bench_id:
                selected = next((b for b in benches if b.bench_id == bench_id), None)
                if selected is None:
                    context["error"] = "That bench is not sitting on this list."
                else:
                    entries = cause_list_scraper.fetch_cause_list(client, selected)
                    context.update(court_name=selected.court_name, judges=selected.judges)
                    context["entries"] = _annotate_entries(conn, context["user"], entries)
    except Exception as exc:  # a court-site outage should not 500 the page
        log.warning("cause list fetch failed: %s", exc)
        context["error"] = (
            f"Could not reach the Court's site ({exc.__class__.__name__}). "
            "It publishes lists the evening before hearings and is often slow then."
        )

    return templates.TemplateResponse(request, "cause_list.html", context)


def _annotate_entries(conn, user, entries) -> list[dict[str, Any]]:
    """Mark the entries that belong to one of this barrister's files."""
    out = []
    for entry in entries:
        item = entry.to_dict()
        item["matter_reference"] = None
        item["matter_id"] = None
        if user is not None and entry.case_type and entry.case_number and entry.case_year:
            matter = matters.matter_for_case(
                conn, int(user["id"]),
                case_type=entry.case_type, case_number=entry.case_number,
                case_year=entry.case_year,
            )
            if matter is not None:
                item["matter_reference"] = matter["reference"]
                item["matter_id"] = matter["id"]
        out.append(item)
    return out


# --------------------------------------------------------------------------
# matters
# --------------------------------------------------------------------------

@router.get("/matters", response_class=HTMLResponse)
def page_matters(request: Request, status: str | None = None, conn=Depends(get_conn)):
    context = _base_context(request, conn, "matters")
    user = context["user"]
    user_id = int(user["id"]) if user else 0
    context.update(
        matters=matters.list_matters(conn, user_id, status=status or None) if user else [],
        summary=matters.practice_summary(conn, user_id),
        status=status, statuses=list(matters.MATTER_STATUSES),
    )
    return templates.TemplateResponse(request, "matters.html", context)


@router.get("/matters/{matter_id}", response_class=HTMLResponse)
def page_matter(request: Request, matter_id: int, conn=Depends(get_conn)):
    context = _base_context(request, conn, "matters")
    try:
        overview = matters.matter_overview(conn, matter_id)
    except matters.MatterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    deadlines = []
    for item in overview["deadlines"]:
        item = dict(item)
        item["days_left"] = _days_left(item["due_on"])
        deadlines.append(item)

    context.update(
        matter=overview["matter"],
        cases=overview["cases"],
        notes=overview["notes"],
        documents=overview["documents"],
        time=overview["time"],
        time_entries=matters.list_time(conn, matter_id),
        deadlines=deadlines,
        open_deadlines=[d for d in deadlines if not d["completed"]],
    )
    return templates.TemplateResponse(request, "matter.html", context)


@router.get("/diary", response_class=HTMLResponse)
def page_diary(request: Request, days: int = 30, conn=Depends(get_conn)):
    context = _base_context(request, conn, "diary")
    user = context["user"]
    rows = matters.upcoming_deadlines(conn, int(user["id"]), within_days=days) if user else []
    context.update(deadlines=_decorate_deadlines(rows), days=days)
    return templates.TemplateResponse(request, "diary.html", context)


# --------------------------------------------------------------------------
# statutes
# --------------------------------------------------------------------------

@router.get("/statutes", response_class=HTMLResponse)
def page_statutes(request: Request, q: str | None = None, conn=Depends(get_conn)):
    context = _base_context(request, conn, "statutes")
    context.update(
        query=q,
        hits=statutes.search(conn, q, limit=10) if q else [],
        stats=statutes.corpus_stats(conn),
    )
    return templates.TemplateResponse(request, "statutes.html", context)


# --------------------------------------------------------------------------
# limitation
# --------------------------------------------------------------------------

@router.get("/limitation", response_class=HTMLResponse)
def page_limitation(
    request: Request,
    start: str | None = None,
    days: int | None = None,
    proceeding: str = "suit",
    copy_applied: str | None = None,
    copy_ready: str | None = None,
    conn=Depends(get_conn),
):
    context = _base_context(request, conn, "limitation")
    context.update(
        proceedings=PROCEEDINGS, result=None, error=None,
        form={
            "start": start, "days": days, "proceeding": proceeding,
            "copy_applied": copy_applied, "copy_ready": copy_ready,
        },
    )

    if not (start and days):
        return templates.TemplateResponse(request, "limitation.html", context)

    def _parse(value: str | None) -> date | None:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None

    try:
        result = limitation.compute(
            start_date=_parse(start),
            period=limitation.Period(int(days), "days"),
            proceeding=proceeding,
            copy_applied_on=_parse(copy_applied),
            copy_ready_on=_parse(copy_ready),
        )
    except (ValueError, TypeError) as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "limitation.html", context)

    context["result"] = {
        "deadline": result.deadline.isoformat(),
        "weekday": result.deadline.strftime("%A"),
        "days_remaining": result.days_remaining(),
        "expired": result.is_expired(),
        "steps": [
            {"rule": s.rule, "explanation": s.explanation,
             "result": s.result.isoformat() if s.result else None}
            for s in result.steps
        ],
        "warnings": result.warnings,
        "citations": result.citations,
    }
    return templates.TemplateResponse(request, "limitation.html", context)


# --------------------------------------------------------------------------
# drafting
# --------------------------------------------------------------------------

@router.get("/drafting", response_class=HTMLResponse)
def page_drafting(request: Request, conn=Depends(get_conn)):
    context = _base_context(request, conn, "drafting")
    context.update(
        templates=drafting.available_templates(),
        provider=settings.resolve_provider(),
        form={}, draft=None, error=None,
    )
    return templates.TemplateResponse(request, "drafting.html", context)


@router.post("/drafting", response_class=HTMLResponse)
def submit_drafting(
    request: Request,
    template: str = Form(...),
    petitioners: str = Form(""),
    respondents: str = Form(""),
    subject: str = Form(""),
    facts: str = Form(""),
    grounds: str = Form(""),
    prayers: str = Form(""),
    authorities: str = Form(""),
    conn=Depends(get_conn),
):
    context = _base_context(request, conn, "drafting")
    form = {
        "template": template, "petitioners": petitioners, "respondents": respondents,
        "subject": subject, "facts": facts, "grounds": grounds,
        "prayers": prayers, "authorities": authorities,
    }
    context.update(
        templates=drafting.available_templates(),
        provider=settings.resolve_provider(),
        form=form, draft=None, error=None,
    )

    request_obj = drafting.DraftRequest(
        template=template,
        petitioners=_lines(petitioners) or ["[PETITIONER TO BE SUPPLIED]"],
        respondents=_lines(respondents) or ["[RESPONDENT TO BE SUPPLIED]"],
        subject_matter=subject,
        facts=facts,
        year=str(date.today().year),
        grounds=_lines(grounds),
        prayers=_lines(prayers),
        supplied_authorities=_lines(authorities),
    )

    try:
        result = drafting.draft(request_obj)
    except drafting.DraftingError as exc:
        context["error"] = str(exc)
    except Exception as exc:
        context["error"] = f"{exc.__class__.__name__}: {exc}"
    else:
        context["draft"] = {"text": result.text, "warnings": result.warnings}

    return templates.TemplateResponse(request, "drafting.html", context)
