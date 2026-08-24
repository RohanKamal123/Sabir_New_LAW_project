"""Watch matching. False negatives cost a missed hearing, so these are the
tests that matter most in the whole suite."""

from __future__ import annotations

import pytest

from barrister.scrapers.cause_list import parse_cause_list
from barrister.services.matching import match_all, match_entry, normalize_case_number, normalize_name


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Mr. Md. Abu  Hanif", "abu hanif"),
            ("MD ABU HANIF", "abu hanif"),
            ("Mst. Ismath Ara", "ismath ara"),
            ("Advocate Sabir Rahman", "sabir rahman"),
        ],
    )
    def test_honorifics_and_spacing_fold_away(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_a_name_of_only_honorifics_survives(self):
        # Folding "Md." to nothing would make this name unmatchable.
        assert normalize_name("Md.") == "md"

    def test_case_number_shorthand(self):
        assert normalize_case_number("CR 2347 of 2007") == "cr 2347/2007"


class TestMatching:
    @pytest.fixture
    def entries(self, cause_list_html):
        return parse_cause_list(cause_list_html)

    def test_matches_advocate_despite_honorific_difference(self, entries):
        matches = match_all(entries, [("advocate", "Abu Hanif")])
        assert len(matches) == 1
        assert "Mr. Md. Abu Hanif" in matches[0].matched_on

    def test_matches_party_on_partial_name(self, entries):
        matches = match_all(entries, [("party", "Asia Khatun")])
        assert matches
        assert all(m.kind == "party" for m in matches)

    def test_matches_case_by_full_reference(self, entries):
        matches = match_all(entries, [("case", "First Appeal 226/2013")])
        assert len(matches) == 1
        assert matches[0].entry.serial == 10

    def test_matches_case_by_shorthand(self, entries):
        matches = match_all(entries, [("case", "CR 2347 of 2007")])
        assert matches
        assert matches[0].entry.case_number == "2347"

    def test_unrelated_watch_does_not_match(self, entries):
        assert match_all(entries, [("advocate", "Nobody At All")]) == []

    def test_single_token_watch_does_not_match_by_token_subset(self, entries):
        # "Md" alone must not match every advocate on the list.
        matches = match_all(entries, [("advocate", "Md")])
        assert len(matches) < len(entries)

    def test_match_reports_why_it_matched(self, entries):
        match = match_all(entries, [("advocate", "Abu Hanif")])[0]
        assert "listed as advocate" in match.reason

    def test_dedupe_key_is_stable(self, entries):
        first = match_all(entries, [("case", "First Appeal 226/2013")])[0]
        second = match_all(entries, [("case", "First Appeal 226/2013")])[0]
        assert first.dedupe_key == second.dedupe_key

    def test_empty_watch_list_matches_nothing(self, entries):
        assert match_all(entries, []) == []

    def test_entry_with_no_advocates_is_not_matched(self, entries):
        bare = next(e for e in entries if not e.advocates)
        assert match_entry(bare, [("advocate", "Anyone")]) == []
