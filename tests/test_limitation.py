"""Limitation arithmetic. These figures decide whether a client keeps a cause
of action, so the tests check the working, not just the answer."""

from __future__ import annotations

from datetime import date

import pytest

from barrister.services.limitation import (
    CourtCalendar, Period, UnverifiedRuleError, compute, deadline_for_article,
    load_articles, unverified_articles,
)


class TestPeriod:
    def test_days(self):
        assert Period(90, "days").add_to(date(2026, 1, 15)) == date(2026, 4, 15)

    def test_months(self):
        assert Period(3, "months").add_to(date(2026, 1, 15)) == date(2026, 4, 15)

    def test_years(self):
        assert Period(3, "years").add_to(date(2026, 1, 15)) == date(2029, 1, 15)

    def test_month_arithmetic_clamps_to_a_short_month(self):
        # 31 January + 1 month has no 31st to land on.
        assert Period(1, "months").add_to(date(2026, 1, 31)) == date(2026, 2, 28)

    def test_leap_year_end_of_february(self):
        assert Period(1, "years").add_to(date(2024, 2, 29)) == date(2025, 2, 28)

    def test_unknown_unit_is_rejected(self):
        with pytest.raises(ValueError):
            Period(1, "fortnights").add_to(date(2026, 1, 1))


class TestCourtCalendar:
    def test_friday_and_saturday_are_closed(self):
        calendar = CourtCalendar()
        assert calendar.is_closed(date(2026, 8, 21))   # Friday
        assert calendar.is_closed(date(2026, 8, 22))   # Saturday
        assert not calendar.is_closed(date(2026, 8, 23))  # Sunday is a working day

    def test_explicit_holiday_is_closed(self):
        calendar = CourtCalendar.with_holidays([date(2026, 3, 26)])
        assert calendar.is_closed(date(2026, 3, 26))

    def test_next_open_day_skips_the_weekend(self):
        assert CourtCalendar().next_open_day(date(2026, 8, 21)) == date(2026, 8, 23)

    def test_next_open_day_skips_a_holiday_run(self):
        calendar = CourtCalendar.with_holidays([date(2026, 8, 23), date(2026, 8, 24)])
        assert calendar.next_open_day(date(2026, 8, 21)) == date(2026, 8, 25)


class TestSection12:
    def test_starting_day_is_excluded_for_a_suit(self):
        result = compute(start_date=date(2026, 1, 1), period=Period(30, "days"))
        assert any(step.rule == "s. 12(1)" for step in result.steps)

    def test_appeal_cites_subsection_two(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal"
        )
        assert any(step.rule == "s. 12(2)" for step in result.steps)

    def test_copy_time_is_added_back_inclusively(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal",
            copy_applied_on=date(2026, 1, 20), copy_ready_on=date(2026, 2, 3),
        )
        # 20 Jan to 3 Feb inclusive is 15 days.
        assert result.excluded_days == 15
        assert result.deadline == date(2026, 4, 30)

    def test_judgment_copy_time_adds_under_subsection_three(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal",
            copy_applied_on=date(2026, 1, 20), copy_ready_on=date(2026, 1, 24),
            judgment_copy_applied_on=date(2026, 1, 25),
            judgment_copy_ready_on=date(2026, 1, 29),
        )
        assert result.excluded_days == 10
        assert any(step.rule == "s. 12(3)" for step in result.steps)

    def test_missing_copy_dates_warn_rather_than_silently_shorten(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal"
        )
        assert any("certified-copy" in w for w in result.warnings)

    def test_only_one_copy_date_is_ignored_with_a_warning(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal",
            copy_applied_on=date(2026, 1, 20),
        )
        assert result.excluded_days == 0
        assert any("both the" in w for w in result.warnings)

    def test_copy_dates_on_a_suit_are_ignored_with_a_warning(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(3, "years"), proceeding="suit",
            copy_applied_on=date(2026, 1, 20), copy_ready_on=date(2026, 2, 3),
        )
        assert result.excluded_days == 0
        assert any("applies only to an appeal" in w for w in result.warnings)

    def test_reversed_copy_dates_are_rejected(self):
        with pytest.raises(ValueError):
            compute(
                start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal",
                copy_applied_on=date(2026, 2, 3), copy_ready_on=date(2026, 1, 20),
            )


class TestSection4:
    def test_deadline_on_a_closed_day_rolls_forward(self):
        # 30 days from 22 July 2026 lands on Friday 21 August.
        result = compute(start_date=date(2026, 7, 22), period=Period(30, "days"))
        assert result.deadline == date(2026, 8, 23)
        assert any(step.rule == "s. 4" for step in result.steps)

    def test_deadline_on_an_open_day_is_untouched(self):
        result = compute(start_date=date(2026, 7, 24), period=Period(30, "days"))
        assert not any(step.rule == "s. 4" for step in result.steps)

    def test_a_holiday_run_rolls_to_the_reopening(self):
        calendar = CourtCalendar.with_holidays([date(2026, 8, 23), date(2026, 8, 24)])
        result = compute(
            start_date=date(2026, 7, 22), period=Period(30, "days"), calendar=calendar
        )
        assert result.deadline == date(2026, 8, 25)


class TestExplanation:
    def test_working_is_shown_with_authority(self):
        result = compute(
            start_date=date(2026, 1, 15), period=Period(90, "days"), proceeding="appeal",
            copy_applied_on=date(2026, 1, 20), copy_ready_on=date(2026, 2, 3),
        )
        text = result.explain()
        assert "Working:" in text
        assert "Limitation Act 1908, s. 12(2)" in text
        assert "not advice" in text

    def test_section_five_is_flagged_but_never_computed(self):
        result = compute(
            start_date=date(2020, 1, 15), period=Period(90, "days"), proceeding="appeal"
        )
        assert result.is_expired(as_of=date(2026, 1, 1))
        assert any("s. 5" in c for c in result.citations)

    def test_days_remaining_is_relative_to_the_given_date(self):
        result = compute(start_date=date(2026, 1, 1), period=Period(30, "days"))
        assert result.days_remaining(as_of=date(2026, 1, 21)) == 11


class TestScheduleArticles:
    def test_articles_were_extracted(self):
        assert len(load_articles()) > 50

    def test_every_article_starts_unverified(self):
        assert len(unverified_articles()) == len(load_articles())

    def test_unverified_article_is_refused(self):
        with pytest.raises(UnverifiedRuleError):
            deadline_for_article("152", start_date=date(2026, 1, 15), proceeding="appeal")

    def test_override_computes_but_warns_loudly(self):
        result = deadline_for_article(
            "152", start_date=date(2026, 1, 15), proceeding="appeal", allow_unverified=True
        )
        assert result.warnings[0].startswith("UNVERIFIED RULE")

    def test_unknown_article_raises(self):
        with pytest.raises(KeyError):
            deadline_for_article("9999", start_date=date(2026, 1, 15), allow_unverified=True)
