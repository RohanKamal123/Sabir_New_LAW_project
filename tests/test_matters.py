"""Matter and case-file management — the CRUD that makes the rest a habit."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from barrister.scrapers.cause_list import parse_cause_list
from barrister.services import matters as M
from barrister.services.notify import NullNotifier
from barrister.services.watchlist import add_user, list_watches, run_sweep


@pytest.fixture
def user(conn):
    return add_user(conn, "Sabir Rahman", telegram_chat_id="1")


@pytest.fixture
def matter(conn, user):
    client = M.add_client(conn, user, "Md. Karim Uddin", phone="+8801700000000")
    return M.open_matter(
        conn, user, "Karim v Bangladesh — acquisition", client_id=client, fee_agreed=150000
    )


class TestClients:
    def test_add_and_list(self, conn, user):
        M.add_client(conn, user, "Zubair Ahmed")
        M.add_client(conn, user, "Abdul Karim")
        names = [row["name"] for row in M.list_clients(conn, user)]
        assert names == ["Abdul Karim", "Zubair Ahmed"]   # alphabetical

    def test_a_client_needs_a_name(self, conn, user):
        with pytest.raises(M.MatterError):
            M.add_client(conn, user, "   ")

    def test_clients_are_scoped_to_the_barrister(self, conn, user):
        other = add_user(conn, "Someone Else")
        M.add_client(conn, user, "Mine")
        assert M.list_clients(conn, other) == []


class TestReferences:
    def test_first_reference_of_the_year(self, conn, user):
        assert M.next_reference(conn, user, year=2026) == "MAT-2026-001"

    def test_references_increment(self, conn, user):
        M.open_matter(conn, user, "First")
        assert M.next_reference(conn, user).endswith("-002")

    def test_references_are_per_barrister(self, conn, user):
        other = add_user(conn, "Other")
        M.open_matter(conn, user, "Mine")
        assert M.next_reference(conn, other).endswith("-001")

    def test_an_explicit_reference_is_honoured(self, conn, user):
        matter_id = M.open_matter(conn, user, "Special", reference="CHM/2026/A1")
        assert M.get_matter(conn, matter_id)["reference"] == "CHM/2026/A1"

    def test_duplicate_reference_is_rejected(self, conn, user):
        M.open_matter(conn, user, "First", reference="X-1")
        with pytest.raises(M.MatterError, match="already in use"):
            M.open_matter(conn, user, "Second", reference="X-1")


class TestMatters:
    def test_open_and_read_back(self, conn, matter):
        row = M.get_matter(conn, matter)
        assert row["status"] == "open"
        assert row["client_name"] == "Md. Karim Uddin"
        assert row["fee_agreed"] == 150000

    def test_a_matter_needs_a_title(self, conn, user):
        with pytest.raises(M.MatterError):
            M.open_matter(conn, user, "  ")

    def test_open_matters_sort_first(self, conn, user):
        closed = M.open_matter(conn, user, "Old")
        M.set_status(conn, closed, "closed")
        M.open_matter(conn, user, "Current")
        assert M.list_matters(conn, user)[0]["title"] == "Current"

    def test_filter_by_status(self, conn, user):
        first = M.open_matter(conn, user, "One")
        M.open_matter(conn, user, "Two")
        M.set_status(conn, first, "disposed")
        assert len(M.list_matters(conn, user, status="open")) == 1

    def test_closing_records_the_date(self, conn, matter):
        M.set_status(conn, matter, "disposed")
        assert M.get_matter(conn, matter)["closed_on"] == date.today().isoformat()

    def test_reopening_clears_the_closed_date(self, conn, matter):
        M.set_status(conn, matter, "closed")
        M.set_status(conn, matter, "open")
        assert M.get_matter(conn, matter)["closed_on"] is None

    def test_unknown_status_is_rejected(self, conn, matter):
        with pytest.raises(M.MatterError):
            M.set_status(conn, matter, "adjourned-forever")

    def test_lookup_by_reference(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        assert M.find_matter_by_reference(conn, user, reference)["id"] == matter


class TestCases:
    def test_linking_a_case_creates_the_watch(self, conn, user, matter):
        M.link_case(conn, matter, case_type="First Appeal", case_number="226", case_year="2013")
        watches = list_watches(conn, user)
        assert [(w["kind"], w["value"]) for w in watches] == [
            ("case", "First Appeal 226/2013")
        ]

    def test_watching_can_be_declined(self, conn, user, matter):
        M.link_case(
            conn, matter, case_type="First Appeal", case_number="226",
            case_year="2013", watch=False,
        )
        assert list_watches(conn, user) == []

    def test_linking_twice_does_not_duplicate(self, conn, matter):
        for _ in range(2):
            M.link_case(conn, matter, case_type="Writ Petition", case_number="1", case_year="2026")
        assert len(M.list_cases(conn, matter)) == 1

    def test_maps_a_listed_case_back_to_its_file(self, conn, user, matter):
        M.link_case(conn, matter, case_type="First Appeal", case_number="226", case_year="2013")
        found = M.matter_for_case(
            conn, user, case_type="First Appeal", case_number="226", case_year="2013"
        )
        assert found["id"] == matter

    def test_an_unrelated_case_maps_to_nothing(self, conn, user, matter):
        assert M.matter_for_case(
            conn, user, case_type="Writ Petition", case_number="999", case_year="1999"
        ) is None

    def test_linking_to_a_missing_matter_fails(self, conn):
        with pytest.raises(M.MatterError):
            M.link_case(conn, 9999, case_type="Writ Petition", case_number="1", case_year="2026")


class TestNotesAndDocuments:
    def test_notes_are_newest_first(self, conn, matter):
        M.add_note(conn, matter, "Older", noted_on=date(2026, 1, 1))
        M.add_note(conn, matter, "Newer", noted_on=date(2026, 6, 1))
        assert M.list_notes(conn, matter)[0]["body"] == "Newer"

    def test_note_kinds_are_constrained(self, conn, matter):
        with pytest.raises(M.MatterError):
            M.add_note(conn, matter, "Body", kind="gossip")

    def test_an_empty_note_is_rejected(self, conn, matter):
        with pytest.raises(M.MatterError):
            M.add_note(conn, matter, "   ")

    def test_documents_are_recorded(self, conn, matter):
        M.add_document(conn, matter, "Writ petition (as filed)", kind="petition",
                       filed_on=date(2026, 3, 1))
        document = M.list_documents(conn, matter)[0]
        assert document["title"] == "Writ petition (as filed)"
        assert document["filed_on"] == "2026-03-01"


class TestTime:
    def test_summary_totals_hours_and_value(self, conn, matter):
        M.log_time(conn, matter, 150, "Drafting", rate=5000)
        M.log_time(conn, matter, 60, "Conference", rate=5000)
        summary = M.time_summary(conn, matter)
        assert summary.minutes == 210
        assert summary.hours == 3.5
        assert summary.billable == 17500.0

    def test_everything_starts_unbilled(self, conn, matter):
        M.log_time(conn, matter, 60, "Drafting", rate=1000)
        assert M.time_summary(conn, matter).unbilled_minutes == 60

    def test_marking_billed_reduces_the_unbilled_total(self, conn, matter):
        entry = M.log_time(conn, matter, 60, "Drafting", rate=1000)
        assert M.mark_billed(conn, [entry]) == 1
        assert M.time_summary(conn, matter).unbilled_minutes == 0

    def test_marking_nothing_is_a_no_op(self, conn, matter):
        assert M.mark_billed(conn, []) == 0

    def test_zero_minutes_is_rejected(self, conn, matter):
        with pytest.raises(M.MatterError):
            M.log_time(conn, matter, 0, "Nothing")

    def test_entry_without_a_rate_counts_time_but_no_value(self, conn, matter):
        M.log_time(conn, matter, 60, "Pro bono advice")
        summary = M.time_summary(conn, matter)
        assert summary.hours == 1.0
        assert summary.billable == 0.0


class TestDeadlines:
    def test_upcoming_deadline_appears_in_the_diary(self, conn, user, matter):
        M.add_deadline(conn, matter, "File CPLA", date.today() + timedelta(days=10))
        diary = M.upcoming_deadlines(conn, user)
        assert len(diary) == 1
        assert diary[0]["reference"].startswith("MAT-")

    def test_deadlines_beyond_the_horizon_are_excluded(self, conn, user, matter):
        M.add_deadline(conn, matter, "Far off", date.today() + timedelta(days=200))
        assert M.upcoming_deadlines(conn, user, within_days=30) == []

    def test_overdue_deadlines_are_included_by_default(self, conn, user, matter):
        M.add_deadline(conn, matter, "Missed", date.today() - timedelta(days=3))
        assert len(M.upcoming_deadlines(conn, user)) == 1

    def test_overdue_can_be_excluded(self, conn, user, matter):
        M.add_deadline(conn, matter, "Missed", date.today() - timedelta(days=3))
        assert M.upcoming_deadlines(conn, user, include_overdue=False) == []

    def test_completed_deadlines_drop_off(self, conn, user, matter):
        deadline = M.add_deadline(conn, matter, "File CPLA", date.today() + timedelta(days=5))
        M.complete_deadline(conn, deadline)
        assert M.upcoming_deadlines(conn, user) == []

    def test_verified_flag_carries_through(self, conn, matter):
        M.add_deadline(conn, matter, "Unverified basis", date.today(), verified=False)
        overview = M.matter_overview(conn, matter)
        assert overview["deadlines"][0]["verified"] == 0


class TestOverview:
    def test_overview_gathers_the_whole_file(self, conn, matter):
        M.link_case(conn, matter, case_type="Writ Petition", case_number="1", case_year="2026")
        M.add_note(conn, matter, "Conference with client")
        M.add_document(conn, matter, "Brief")
        M.log_time(conn, matter, 30, "Reading in", rate=2000)
        M.add_deadline(conn, matter, "File", date.today())

        overview = M.matter_overview(conn, matter)

        assert overview["matter"]["title"].startswith("Karim v Bangladesh")
        assert len(overview["cases"]) == 1
        assert len(overview["notes"]) == 1
        assert len(overview["documents"]) == 1
        assert len(overview["deadlines"]) == 1
        assert overview["time"]["hours"] == 0.5

    def test_overview_of_a_missing_matter_fails(self, conn):
        with pytest.raises(M.MatterError):
            M.matter_overview(conn, 9999)

    def test_practice_summary_counts_by_status(self, conn, user, matter):
        second = M.open_matter(conn, user, "Second")
        M.set_status(conn, second, "disposed")
        M.log_time(conn, matter, 120, "Drafting", rate=1000)

        summary = M.practice_summary(conn, user)

        assert summary["total_matters"] == 2
        assert summary["matters"]["open"] == 1
        assert summary["matters"]["disposed"] == 1
        assert summary["unbilled_hours"] == 2.0


class TestAlertsNameTheFile:
    def test_cause_list_alert_carries_the_file_reference(self, conn, user, cause_list_html):
        matter = M.open_matter(conn, user, "Ruhul Amin v Aiyub Ali")
        M.link_case(conn, matter, case_type="First Appeal", case_number="226", case_year="2013")
        notifier = NullNotifier()

        run_sweep(conn, parse_cause_list(cause_list_html), notifier)

        body = notifier.sent[0][2]
        assert M.get_matter(conn, matter)["reference"] in body

    def test_a_watch_without_a_matter_still_alerts(self, conn, user, cause_list_html):
        from barrister.services.watchlist import add_watch

        add_watch(conn, user, "case", "First Appeal 226/2013")
        notifier = NullNotifier()

        run_sweep(conn, parse_cause_list(cause_list_html), notifier)

        assert len(notifier.sent) == 1
        assert "MAT-" not in notifier.sent[0][2]
