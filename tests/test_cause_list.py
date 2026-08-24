"""Cause list parsing, against a real printable list saved from the Court's site."""

from __future__ import annotations

from barrister.scrapers.cause_list import (
    parse_bench_list, parse_case_reference, parse_cause_list, parse_parties,
)


class TestCaseReference:
    def test_plain_reference(self):
        parsed = parse_case_reference("First Appeal 110/2010")
        assert parsed["case_type"] == "First Appeal"
        assert parsed["case_number"] == "110"
        assert parsed["case_year"] == "2010"
        assert parsed["district"] is None

    def test_reference_with_district(self):
        parsed = parse_case_reference("First Appeal 442/2014 Chattogram")
        assert parsed["case_type"] == "First Appeal"
        assert parsed["district"] == "Chattogram"

    def test_number_containing_parentheses(self):
        # "Civil Rule 832(FM)/2006" is a real listing format; a naive \d+ breaks it.
        parsed = parse_case_reference("Civil Rule 832(FM)/2006 Tangail")
        assert parsed["case_type"] == "Civil Rule"
        assert parsed["case_number"] == "832(FM)"
        assert parsed["case_year"] == "2006"
        assert parsed["district"] == "Tangail"

    def test_annotations_are_notes_not_district(self):
        parsed = parse_case_reference("First Appeal 312/2018 Barishal (heard in part)")
        assert parsed["district"] == "Barishal"
        assert parsed["notes"] == ["heard in part"]

    def test_unparseable_reference_is_not_fatal(self):
        parsed = parse_case_reference("Some heading with no case number")
        assert parsed["case_type"] is None


class TestParties:
    def test_splits_on_vs(self):
        parsed = parse_parties("SMA Majed and others vs Agrani Bank and others")
        assert parsed["petitioner"] == "SMA Majed and others"
        assert parsed["respondent"] == "Agrani Bank and others"
        assert parsed["advocates"] == []

    def test_extracts_advocates_from_both_sides(self):
        parsed = parse_parties(
            "G Ltd. [Adv : Mr. Subrata Kumar Kundu-For the petitioners] "
            "vs BB [Adv : Mr. Ziaul Haque Sarker-For the applicant]"
        )
        assert parsed["advocates"] == ["Mr. Subrata Kumar Kundu", "Mr. Ziaul Haque Sarker"]
        assert "[Adv" not in parsed["petitioner"]

    def test_splits_advocates_joined_by_with(self):
        parsed = parse_parties(
            "X [Adv : Mr. Nurul Huda with Mr. Rashedul Haque...For the appellant.] vs Y"
        )
        assert parsed["advocates"] == ["Mr. Nurul Huda", "Mr. Rashedul Haque"]

    def test_strips_trailing_role_suffix(self):
        parsed = parse_parties("X [Adv : Mr. N.M. Ahasanul Haque, adv. for the respondent] vs Y")
        assert parsed["advocates"] == ["Mr. N.M. Ahasanul Haque"]

    def test_missing_respondent_is_none(self):
        assert parse_parties("Only one party named")["respondent"] is None


class TestCauseListPage:
    def test_parses_every_listed_matter(self, cause_list_html):
        entries = parse_cause_list(cause_list_html)
        assert len(entries) == 26

    def test_reads_header_metadata(self, cause_list_html):
        entry = parse_cause_list(cause_list_html)[0]
        assert entry.list_date == "24/08/2026"
        assert entry.division == "High Court Division"
        assert entry.court_name == "Annex Building Court No. 18"
        assert entry.judges.startswith("Justice Sheikh Abdul Awal")

    def test_tracks_section_headings(self, cause_list_html):
        entries = parse_cause_list(cause_list_html)
        sections = {e.section for e in entries}
        # Being 3rd "For Judgment" is a different day than 3rd "For Hearing".
        assert {"Fixing a date of hearing", "For Judgment", "For Hearing"} <= sections

    def test_connected_matters_inherit_parent_serial(self, cause_list_html):
        entries = parse_cause_list(cause_list_html)
        connected = [e for e in entries if e.connected_to is not None]
        assert connected, "expected at least one '(with)' connected matter"
        for entry in connected:
            assert entry.serial == entry.connected_to

    def test_no_entry_loses_its_case_reference(self, cause_list_html):
        for entry in parse_cause_list(cause_list_html):
            assert entry.case_type, f"unparsed listing: {entry.raw!r}"
            assert entry.case_year

    def test_empty_page_yields_no_entries(self):
        assert parse_cause_list("<html><body></body></html>") == []


class TestBenchList:
    def test_finds_every_sitting_bench(self, bench_list_html):
        benches = parse_bench_list(bench_list_html, division_id=2)
        assert len(benches) == 60

    def test_captures_the_ids_needed_to_fetch_a_list(self, bench_list_html):
        bench = parse_bench_list(bench_list_html, division_id=2)[0]
        assert bench.court_id == "42"
        assert bench.bench_id == "10294"
        assert bench.list_date == "24/08/2026"
        assert bench.division == "High Court Division"
        assert bench.judges.startswith("Justice")

    def test_judges_field_excludes_navigation_link_text(self, bench_list_html):
        # Each bench row carries "Printable View" / "Page by page" links whose
        # text lands in the same cell as the judges' names.
        for bench in parse_bench_list(bench_list_html, division_id=2):
            assert "Printable View" not in bench.judges
            assert "Page by page" not in bench.judges

    def test_benches_are_unique(self, bench_list_html):
        benches = parse_bench_list(bench_list_html, division_id=2)
        keys = {(b.court_id, b.bench_id) for b in benches}
        assert len(keys) == len(benches)
