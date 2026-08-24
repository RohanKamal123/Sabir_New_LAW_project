"""Rule-based limitation and filing-deadline calculation.

Deliberately *not* an LLM feature. A limitation period is a computation over
encoded rules, and a wrong answer costs a client their cause of action, so
every figure this module produces is either derived from statutory text quoted
in the working, or it is not produced at all.

Three layers, in descending order of confidence:

1. **Structural rules** (:func:`compute`) — sections 4 and 12 of the Limitation
   Act 1908. These are pure date arithmetic over text quoted verbatim in
   ``RULE_CITATIONS``, and are safe to rely on.
2. **Article periods** — the First Schedule, machine-extracted from the
   Ministry's PDF by ``tools/extract_limitation_schedule.py``. Extraction from
   a three-column PDF table is imperfect, so every article is flagged
   ``verified: false`` and :func:`deadline_for_article` refuses to use one
   until a lawyer has checked it and flipped the flag.
3. **Section 5 condonation** — never computed. Whether there is "sufficient
   cause" for delay is a judgment call, so an expired period is reported as
   expired, with a pointer to section 5, and no opinion offered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

_DATA = Path(__file__).resolve().parent.parent / "data" / "limitation_schedule.json"

# Bangladesh's weekend. The Supreme Court also observes vacations and public
# holidays, which callers supply as explicit dates — there is no reliable
# machine-readable source for them.
DEFAULT_WEEKLY_CLOSED = frozenset({4, 5})  # Monday=0 ... Friday=4, Saturday=5

RULE_CITATIONS = {
    "s4": (
        "Limitation Act 1908, s. 4 — \"Where the period of limitation prescribed "
        "for any suit, appeal or application expires on a day when the Court is "
        "closed, the suit, appeal or application may be instituted, preferred or "
        "made on the day that the Court re-opens.\""
    ),
    "s12(1)": (
        "Limitation Act 1908, s. 12(1) — \"In computing the period of limitation "
        "prescribed for any suit, appeal or application, the day from which such "
        "period is to be reckoned shall be excluded.\""
    ),
    "s12(2)": (
        "Limitation Act 1908, s. 12(2) — \"In computing the period of limitation "
        "prescribed for an appeal, an application for leave to appeal and an "
        "application for a review of judgment, the day on which the judgment "
        "complained of was pronounced, and the time requisite for obtaining a copy "
        "of the decree, sentence or order appealed from or sought to be reviewed, "
        "shall be excluded.\""
    ),
    "s12(3)": (
        "Limitation Act 1908, s. 12(3) — \"Where a decree is appealed from or sought "
        "to be reviewed, the time requisite for obtaining a copy of the judgment on "
        "which it is founded shall also be excluded.\""
    ),
    "s5": (
        "Limitation Act 1908, s. 5 — an appeal or application may be admitted after "
        "the prescribed period where the appellant or applicant satisfies the Court "
        "that they had sufficient cause for not preferring it in time."
    ),
}

# Proceedings to which s. 12(2) applies, per its own words.
COPY_TIME_PROCEEDINGS = frozenset({"appeal", "leave_to_appeal", "review"})


class UnverifiedRuleError(RuntimeError):
    """Raised when a deadline is requested from a rule no lawyer has checked."""


# --------------------------------------------------------------------------
# court calendar
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CourtCalendar:
    """When the Court is closed, for the purposes of s. 4."""

    weekly_closed: frozenset[int] = DEFAULT_WEEKLY_CLOSED
    holidays: frozenset[date] = frozenset()

    @classmethod
    def with_holidays(cls, holidays: Iterable[date], weekly_closed: Iterable[int] | None = None):
        return cls(
            weekly_closed=frozenset(weekly_closed) if weekly_closed is not None else DEFAULT_WEEKLY_CLOSED,
            holidays=frozenset(holidays),
        )

    def is_closed(self, day: date) -> bool:
        return day.weekday() in self.weekly_closed or day in self.holidays

    def next_open_day(self, day: date, *, max_lookahead: int = 400) -> date:
        """The day the Court re-opens on or after ``day``."""
        cursor = day
        for _ in range(max_lookahead):
            if not self.is_closed(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise ValueError(f"court appears closed for over {max_lookahead} days from {day}")


# --------------------------------------------------------------------------
# schedule rules
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    value: int
    unit: str  # days | months | years

    def add_to(self, start: date) -> date:
        if self.unit == "days":
            return start + timedelta(days=self.value)
        if self.unit == "months":
            return _add_months(start, self.value)
        if self.unit == "years":
            return _add_months(start, self.value * 12)
        raise ValueError(f"unknown period unit: {self.unit!r}")

    def __str__(self) -> str:
        unit = self.unit.rstrip("s") + ("s" if self.value != 1 else "")
        return f"{self.value} {unit}"


@dataclass(frozen=True)
class ArticleRule:
    article: str
    description: str
    period: Period | None
    period_text: str
    trigger: str
    verified: bool


def _add_months(start: date, months: int) -> date:
    """Calendar-month arithmetic, clamping to the end of a short month."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    last_day = (next_month_start - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


@lru_cache(maxsize=1)
def load_articles() -> dict[str, ArticleRule]:
    if not _DATA.exists():
        return {}
    payload = json.loads(_DATA.read_text(encoding="utf-8"))
    rules: dict[str, ArticleRule] = {}
    for entry in payload.get("articles", []):
        period = entry.get("period")
        rules[entry["article"]] = ArticleRule(
            article=entry["article"],
            description=entry.get("description", ""),
            period=Period(period["value"], period["unit"]) if period else None,
            period_text=entry.get("period_text", ""),
            trigger=entry.get("trigger", ""),
            verified=bool(entry.get("verified", False)),
        )
    return rules


# --------------------------------------------------------------------------
# computation
# --------------------------------------------------------------------------

@dataclass
class Step:
    """One line of the working, so the arithmetic can be audited."""

    rule: str
    explanation: str
    result: date | None = None


@dataclass
class LimitationResult:
    deadline: date
    period: Period
    start_date: date
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    excluded_days: int = 0

    @property
    def days_remaining_from_today(self) -> int:
        return (self.deadline - date.today()).days

    def days_remaining(self, as_of: date | None = None) -> int:
        return (self.deadline - (as_of or date.today())).days

    def is_expired(self, as_of: date | None = None) -> bool:
        return (as_of or date.today()) > self.deadline

    def explain(self) -> str:
        lines = [
            f"Limitation period: {self.period}",
            f"Running from:      {self.start_date.isoformat()}",
            f"Filing deadline:   {self.deadline.isoformat()} ({self.deadline:%A})",
            "",
            "Working:",
        ]
        for index, step in enumerate(self.steps, start=1):
            suffix = f" -> {step.result.isoformat()}" if step.result else ""
            lines.append(f"  {index}. [{step.rule}] {step.explanation}{suffix}")
        if self.warnings:
            lines += ["", "Warnings:"] + [f"  ! {w}" for w in self.warnings]
        if self.citations:
            lines += ["", "Authority:"] + [f"  - {c}" for c in self.citations]
        lines += [
            "",
            "This is a computation, not advice. Check it against the Act and the "
            "Supreme Court Rules before you file.",
        ]
        return "\n".join(lines)


def compute(
    *,
    start_date: date,
    period: Period,
    proceeding: str = "suit",
    calendar: CourtCalendar | None = None,
    copy_applied_on: date | None = None,
    copy_ready_on: date | None = None,
    judgment_copy_applied_on: date | None = None,
    judgment_copy_ready_on: date | None = None,
) -> LimitationResult:
    """Compute a filing deadline from a start date and a prescribed period.

    ``start_date`` is the date the period runs from — the date of the decree,
    order or judgment. It is *excluded* from the count by s. 12(1).

    For an appeal, an application for leave to appeal, or a review, pass the
    certified-copy dates: s. 12(2) excludes the "time requisite for obtaining a
    copy", which in practice is the span from applying for the copy to it being
    ready. ``judgment_copy_*`` covers the additional exclusion in s. 12(3) where
    a decree is appealed and the judgment copy was obtained separately.
    """
    calendar = calendar or CourtCalendar()
    steps: list[Step] = []
    citations: list[str] = []
    warnings: list[str] = []

    # s. 12(1) / 12(2): the starting day is excluded, so the clock starts the
    # next day. Both subsections have the same arithmetic effect here; which one
    # we cite depends on the proceeding.
    if proceeding in COPY_TIME_PROCEEDINGS:
        steps.append(
            Step("s. 12(2)", f"Exclude the day judgment was pronounced ({start_date.isoformat()})")
        )
        citations.append(RULE_CITATIONS["s12(2)"])
    else:
        steps.append(
            Step("s. 12(1)", f"Exclude the day the period runs from ({start_date.isoformat()})")
        )
        citations.append(RULE_CITATIONS["s12(1)"])

    clock_starts = start_date + timedelta(days=1)
    raw_deadline = period.add_to(start_date)
    steps.append(
        Step("period", f"Add the prescribed period of {period} from {start_date.isoformat()}", raw_deadline)
    )

    # s. 12(2)/(3): add back the time requisite for obtaining copies.
    excluded_days = 0
    if proceeding in COPY_TIME_PROCEEDINGS:
        excluded_days += _copy_time(
            copy_applied_on, copy_ready_on, "decree/order", steps, warnings, "s. 12(2)"
        )
        if judgment_copy_applied_on or judgment_copy_ready_on:
            excluded_days += _copy_time(
                judgment_copy_applied_on, judgment_copy_ready_on, "judgment", steps, warnings, "s. 12(3)"
            )
            citations.append(RULE_CITATIONS["s12(3)"])
        if copy_applied_on is None and copy_ready_on is None:
            warnings.append(
                "No certified-copy dates supplied. Section 12(2) excludes the time "
                "requisite for obtaining a copy — supply the dates or this deadline "
                "is earlier than the law allows."
            )
    elif copy_applied_on or copy_ready_on:
        warnings.append(
            f"Copy dates were supplied but s. 12(2) applies only to an appeal, an "
            f"application for leave to appeal, or a review — not to a {proceeding}. "
            "They have been ignored."
        )

    deadline = raw_deadline + timedelta(days=excluded_days)
    if excluded_days:
        steps.append(Step("s. 12", f"Add back {excluded_days} excluded day(s)", deadline))

    # s. 4: a deadline on a closed day rolls to the day the Court re-opens.
    if calendar.is_closed(deadline):
        reopens = calendar.next_open_day(deadline)
        steps.append(
            Step(
                "s. 4",
                f"{deadline.isoformat()} ({deadline:%A}) is a day the Court is closed; "
                f"time runs to the day it re-opens",
                reopens,
            )
        )
        citations.append(RULE_CITATIONS["s4"])
        deadline = reopens

    if proceeding in COPY_TIME_PROCEEDINGS:
        warnings.append(
            "If this period has expired, s. 5 may still permit admission on "
            "sufficient cause — that is a matter of judgment, not computation."
        )
        citations.append(RULE_CITATIONS["s5"])

    return LimitationResult(
        deadline=deadline,
        period=period,
        start_date=start_date,
        steps=steps,
        warnings=warnings,
        citations=citations,
        excluded_days=excluded_days,
    )


def _copy_time(
    applied: date | None,
    ready: date | None,
    label: str,
    steps: list[Step],
    warnings: list[str],
    rule: str,
) -> int:
    """Days requisite for obtaining a copy, counted inclusively."""
    if applied is None or ready is None:
        if applied or ready:
            warnings.append(
                f"Only one of the {label} copy dates was supplied; both the "
                "application date and the ready date are needed to compute the "
                "time requisite, so it has been ignored."
            )
        return 0
    if ready < applied:
        raise ValueError(f"{label} copy ready date {ready} precedes the application date {applied}")

    days = (ready - applied).days + 1
    steps.append(
        Step(rule, f"Exclude {days} day(s) requisite for the {label} copy "
                   f"({applied.isoformat()} to {ready.isoformat()})")
    )
    return days


def deadline_for_article(
    article: str,
    *,
    start_date: date,
    proceeding: str = "suit",
    calendar: CourtCalendar | None = None,
    allow_unverified: bool = False,
    **copy_dates: date | None,
) -> LimitationResult:
    """Compute a deadline using a First Schedule article.

    Refuses unverified articles by default. The Schedule was machine-extracted
    from a PDF table; until a lawyer has checked an article against the official
    text, using it to compute a filing deadline is exactly the kind of confident
    wrong answer this product exists to avoid.
    """
    rules = load_articles()
    rule = rules.get(article)
    if rule is None:
        raise KeyError(f"no Schedule article {article!r} in the extracted rule set")
    if rule.period is None:
        raise ValueError(
            f"article {article} has no machine-readable period (text: {rule.period_text!r})"
        )
    if not rule.verified and not allow_unverified:
        raise UnverifiedRuleError(
            f"Schedule article {article} has not been verified by a lawyer. "
            f"Extracted period: {rule.period_text!r}; trigger: {rule.trigger!r}. "
            f"Check it against {load_source_url()} and set \"verified\": true, or "
            f"pass allow_unverified=True to compute anyway."
        )

    result = compute(
        start_date=start_date,
        period=rule.period,
        proceeding=proceeding,
        calendar=calendar,
        **copy_dates,
    )
    result.steps.insert(
        0,
        Step(f"Art. {rule.article}", f"{rule.description[:160]} — period: {rule.period_text}"),
    )
    if not rule.verified:
        result.warnings.insert(
            0,
            f"UNVERIFIED RULE: Schedule article {article} was machine-extracted and "
            "has not been checked by a lawyer. Do not rely on this deadline.",
        )
    return result


@lru_cache(maxsize=1)
def load_source_url() -> str:
    if not _DATA.exists():
        return "http://bdlaws.minlaw.gov.bd/act-88.html"
    return json.loads(_DATA.read_text(encoding="utf-8")).get(
        "source_url", "http://bdlaws.minlaw.gov.bd/act-88.html"
    )


def unverified_articles() -> list[ArticleRule]:
    """The review queue: what still needs a lawyer's eye."""
    return [rule for rule in load_articles().values() if not rule.verified]
