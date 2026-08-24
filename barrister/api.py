"""HTTP API.

Thin: every endpoint delegates to a service. The API exists so the eventual
Telegram bot and any web UI share one implementation rather than each growing
their own copy of the matching and diffing logic.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import settings
from .db import connect, init_db
from .http import PoliteClient
from .scrapers import case_status as case_status_scraper
from .scrapers import cause_list as cause_list_scraper
from .services import drafting, limitation, statutes, watchlist

from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="Barrister Tools",
    version="0.1.0",
    description=(
        "Cause-list alerts, case tracking, statute lookup, limitation "
        "calculation and template drafting for Supreme Court of Bangladesh practice."
    ),
)


# The web UI and the JSON API are one application: same services, same
# database session, so neither can drift from the other.
from . import web as _web  # noqa: E402  (imported after `app` for clarity)

app.mount("/static", StaticFiles(directory=str(_web.STATIC_DIR)), name="static")
app.include_router(_web.router)


def get_conn():
    conn = connect()
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class UserIn(BaseModel):
    name: str
    chamber: str | None = None
    telegram_chat_id: str | None = None
    email: str | None = None


class WatchIn(BaseModel):
    kind: str = Field(pattern="^(advocate|party|case)$")
    value: str


class LimitationIn(BaseModel):
    start_date: date
    days: int | None = None
    unit: str = "days"
    article: str | None = None
    proceeding: str = "suit"
    copy_applied_on: date | None = None
    copy_ready_on: date | None = None
    holidays: list[date] = Field(default_factory=list)
    allow_unverified: bool = False


class DraftIn(BaseModel):
    template: str
    petitioners: list[str]
    respondents: list[str]
    facts: str = ""
    year: str = ""
    subject_matter: str = ""
    grounds: list[str] = Field(default_factory=list)
    prayers: list[str] = Field(default_factory=list)
    supplied_authorities: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "drafting_provider": settings.resolve_provider(),
        "sources": {
            "supreme_court": settings.supreme_court_base,
            "bdlaws": settings.bdlaws_base,
        },
    }


# --------------------------------------------------------------------------
# users and watches
# --------------------------------------------------------------------------

@app.post("/users", status_code=201)
def create_user(payload: UserIn, conn=Depends(get_conn)) -> dict[str, Any]:
    user_id = watchlist.add_user(
        conn, payload.name, chamber=payload.chamber,
        telegram_chat_id=payload.telegram_chat_id, email=payload.email,
    )
    return {"id": user_id, "name": payload.name}


@app.post("/users/{user_id}/watches", status_code=201)
def create_watch(user_id: int, payload: WatchIn, conn=Depends(get_conn)) -> dict[str, Any]:
    try:
        watch_id = watchlist.add_watch(conn, user_id, payload.kind, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": watch_id, "kind": payload.kind, "value": payload.value}


@app.get("/users/{user_id}/watches")
def get_watches(user_id: int, conn=Depends(get_conn)) -> list[dict[str, Any]]:
    return [dict(row) for row in watchlist.list_watches(conn, user_id)]


@app.get("/users/{user_id}/alerts")
def get_alerts(user_id: int, limit: int = 20, conn=Depends(get_conn)) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM alerts WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# cause lists
# --------------------------------------------------------------------------

@app.get("/cause-list/benches")
def get_benches(division: int = Query(2, ge=1, le=2)) -> list[dict[str, Any]]:
    with PoliteClient() as client:
        benches = cause_list_scraper.fetch_benches(client, division)
    return [
        {
            "court_id": b.court_id, "bench_id": b.bench_id, "court_name": b.court_name,
            "judges": b.judges, "list_date": b.list_date, "division": b.division,
        }
        for b in benches
    ]


@app.get("/cause-list")
def get_cause_list(
    division: int = Query(2, ge=1, le=2),
    court_id: str | None = None,
    bench_id: str | None = None,
) -> list[dict[str, Any]]:
    with PoliteClient() as client:
        benches = cause_list_scraper.fetch_benches(client, division)
        if court_id:
            benches = [b for b in benches if b.court_id == court_id]
        if bench_id:
            benches = [b for b in benches if b.bench_id == bench_id]
        if not benches:
            raise HTTPException(status_code=404, detail="no matching bench")
        entries = [
            entry
            for bench in benches
            for entry in cause_list_scraper.fetch_cause_list(client, bench)
        ]
    return [entry.to_dict() for entry in entries]


# --------------------------------------------------------------------------
# case status
# --------------------------------------------------------------------------

@app.get("/cases/{case_type}/{number}/{year}")
def get_case(
    case_type: str, number: str, year: str, division: int | None = None
) -> dict[str, Any]:
    with PoliteClient() as client:
        try:
            status = case_status_scraper.fetch_case_status(
                client, case_type=case_type, case_number=number, year=year, division_id=division
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return status.to_dict()


@app.get("/case-types")
def get_case_types(division: int | None = None) -> list[dict[str, Any]]:
    return [
        {
            "division_id": ct.division_id, "division": ct.division, "nature": ct.nature,
            "case_type_id": ct.case_type_id, "name": ct.name,
        }
        for ct in case_status_scraper.load_case_types()
        if division is None or ct.division_id == division
    ]


# --------------------------------------------------------------------------
# statutes
# --------------------------------------------------------------------------

@app.get("/statutes/search")
def search_statutes(q: str, limit: int = 10, conn=Depends(get_conn)) -> list[dict[str, Any]]:
    return [
        {
            "citation": hit.citation, "act_title": hit.act_title, "section_no": hit.section_no,
            "part": hit.part, "heading": hit.heading, "body": hit.body,
            "snippet": hit.snippet, "url": hit.url,
        }
        for hit in statutes.search(conn, q, limit=limit)
    ]


@app.get("/statutes/stats")
def statute_stats(conn=Depends(get_conn)) -> dict[str, int]:
    return statutes.corpus_stats(conn)


# --------------------------------------------------------------------------
# limitation
# --------------------------------------------------------------------------

@app.post("/limitation")
def compute_limitation(payload: LimitationIn) -> dict[str, Any]:
    calendar = limitation.CourtCalendar.with_holidays(payload.holidays)
    try:
        if payload.article:
            result = limitation.deadline_for_article(
                payload.article,
                start_date=payload.start_date,
                proceeding=payload.proceeding,
                calendar=calendar,
                allow_unverified=payload.allow_unverified,
                copy_applied_on=payload.copy_applied_on,
                copy_ready_on=payload.copy_ready_on,
            )
        elif payload.days is not None:
            result = limitation.compute(
                start_date=payload.start_date,
                period=limitation.Period(payload.days, payload.unit),
                proceeding=payload.proceeding,
                calendar=calendar,
                copy_applied_on=payload.copy_applied_on,
                copy_ready_on=payload.copy_ready_on,
            )
        else:
            raise HTTPException(status_code=400, detail="give either `article` or `days`")
    except limitation.UnverifiedRuleError as exc:
        # 409: the rule exists but is not fit to rely on yet.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "deadline": result.deadline.isoformat(),
        "start_date": result.start_date.isoformat(),
        "period": str(result.period),
        "excluded_days": result.excluded_days,
        "days_remaining": result.days_remaining(),
        "expired": result.is_expired(),
        "steps": [
            {"rule": s.rule, "explanation": s.explanation,
             "result": s.result.isoformat() if s.result else None}
            for s in result.steps
        ],
        "warnings": result.warnings,
        "citations": result.citations,
        "explanation": result.explain(),
    }


@app.get("/limitation/review-queue")
def limitation_review_queue() -> list[dict[str, Any]]:
    return [
        {
            "article": rule.article, "description": rule.description,
            "period_text": rule.period_text, "trigger": rule.trigger,
        }
        for rule in limitation.unverified_articles()
    ]


# --------------------------------------------------------------------------
# drafting
# --------------------------------------------------------------------------

@app.get("/drafting/templates")
def list_templates() -> list[str]:
    return drafting.available_templates()


@app.post("/drafting/draft")
def create_draft(payload: DraftIn) -> dict[str, Any]:
    request = drafting.DraftRequest(
        template=payload.template,
        petitioners=payload.petitioners,
        respondents=payload.respondents,
        facts=payload.facts,
        year=payload.year or str(date.today().year),
        subject_matter=payload.subject_matter,
        grounds=payload.grounds,
        prayers=payload.prayers,
        supplied_authorities=payload.supplied_authorities,
        extra=payload.extra,
    )
    try:
        result = drafting.draft(request)
    except drafting.DraftingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # a missing template variable surfaces here
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "text": result.text, "template": result.template, "provider": result.provider,
        "model": result.model, "warnings": result.warnings,
    }


# Both surfaces resolve the database through the same dependency, so a test (or
# a future connection pool) overriding one overrides both.
app.dependency_overrides[_web.get_conn] = get_conn
