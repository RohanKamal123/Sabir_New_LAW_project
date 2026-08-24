"""Case status parsing and change detection."""

from __future__ import annotations

import copy

import pytest

from barrister.scrapers.case_status import (
    Hearing, case_history_url, find_case_type, load_case_types, parse_case_history,
)
from barrister.services.notify import NullNotifier
from barrister.services.tracker import diff_status, track_case
from barrister.services.watchlist import add_user


class TestCaseTypeRegistry:
    def test_registry_is_populated(self):
        assert len(load_case_types()) == 110

    def test_resolves_a_typed_name(self):
        found = find_case_type("writ petition")
        assert found.case_type_id == 13
        assert found.division_id == 2

    def test_exact_match_beats_a_longer_partial(self):
        # "In re : VC Writ Petition" also contains "writ petition".
        assert find_case_type("Writ Petition").name == "Writ Petition"

    def test_unknown_name_returns_none(self):
        assert find_case_type("Interplanetary Appeal") is None

    def test_division_filter_applies(self):
        assert find_case_type("Writ Petition", division_id=1) is None

    def test_url_shape(self):
        url = case_history_url(division_id=2, case_type_id=1, case_number=226, year=2013)
        assert "case_history/case_history.php" in url
        assert "case_type_id=1" in url and "case_number=226" in url


class TestHistoryParsing:
    def test_reads_the_case_reference(self, case_history_html):
        status = parse_case_history(case_history_html)
        assert status.case_ref == "First Appeal 226/2013"

    def test_reads_every_hearing(self, case_history_html):
        assert len(parse_case_history(case_history_html).hearings) == 22

    def test_hearings_are_newest_first(self, case_history_html):
        hearings = parse_case_history(case_history_html).hearings
        assert hearings[0].number == 22
        assert hearings[-1].number == 1

    def test_captures_bench_and_result(self, case_history_html):
        hearing = parse_case_history(case_history_html).hearings[1]
        assert hearing.date == "01/02/26"
        assert hearing.court.startswith("Bijoy 71")
        assert "Justice" in hearing.judges
        assert hearing.result == "Allowewd"   # the Court's own spelling

    def test_separates_party_from_their_advocate(self, case_history_html):
        status = parse_case_history(case_history_html)
        assert status.petitioner == "Md. Ruhul Amin and ors."
        assert status.petitioner_lawyer == "Mr. Abdur Rahim"

    def test_nested_history_table_is_not_double_counted(self, case_history_html):
        numbers = [h.number for h in parse_case_history(case_history_html).hearings]
        assert len(numbers) == len(set(numbers))


class TestDiffing:
    @pytest.fixture
    def status(self, case_history_html):
        return parse_case_history(case_history_html, source_url="http://example/hist")

    def test_first_sighting_reports_only_the_latest_listing(self, status):
        changes = diff_status(None, status)
        assert [c.kind for c in changes] == ["first_seen"]

    def test_identical_snapshot_yields_no_change(self, status):
        assert diff_status(status.to_dict(), status) == []

    def test_new_hearing_is_detected(self, status):
        updated = copy.deepcopy(status)
        updated.hearings.insert(0, Hearing(23, "07/09/26", "Court No. 18", "Justice X", None))
        changes = diff_status(status.to_dict(), updated)
        assert [c.kind for c in changes] == ["new_hearing"]

    def test_result_being_filled_in_is_detected(self, status):
        updated = copy.deepcopy(status)
        updated.hearings[0].result = "Adjourned till 07.09.2026"
        changes = diff_status(status.to_dict(), updated)
        assert [c.kind for c in changes] == ["result_added"]

    def test_amended_result_is_reported_with_both_values(self, status):
        updated = copy.deepcopy(status)
        updated.hearings[1].result = "Dismissed"
        change = diff_status(status.to_dict(), updated)[0]
        assert change.kind == "result_changed"
        assert change.previous_result == "Allowewd"
        assert "Allowewd" in change.describe() and "Dismissed" in change.describe()


class TestTracking:
    def test_first_seen_does_not_alert_by_default(self, conn, case_history_html):
        user = add_user(conn, "Sabir", telegram_chat_id="1")
        status = parse_case_history(case_history_html)
        notifier = NullNotifier()

        track_case(conn, user, status, notifier)

        assert notifier.sent == []

    def test_a_later_change_alerts(self, conn, case_history_html):
        user = add_user(conn, "Sabir", telegram_chat_id="1")
        status = parse_case_history(case_history_html)
        notifier = NullNotifier()
        track_case(conn, user, status, notifier)

        updated = copy.deepcopy(status)
        updated.hearings[0].result = "Adjourned"
        track_case(conn, user, updated, notifier)

        assert len(notifier.sent) == 1
        assert "Adjourned" in notifier.sent[0][2]

    def test_unchanged_recheck_is_silent(self, conn, case_history_html):
        user = add_user(conn, "Sabir", telegram_chat_id="1")
        status = parse_case_history(case_history_html)
        notifier = NullNotifier()

        track_case(conn, user, status, notifier)
        assert track_case(conn, user, status, notifier) == []
        assert notifier.sent == []
