"""bdlaws parsing, corpus storage and exact-text search."""

from __future__ import annotations

import pytest

from barrister.http import decode_html
from barrister.scrapers.bdlaws import (
    Act, ActRef, StatuteSection, parse_act_index, parse_act_page, parse_act_title,
    parse_section_page,
)
from barrister.services.statutes import corpus_stats, lookup_section, search, store_act


class TestDecoding:
    def test_utf16_pages_decode(self, act_page_html):
        # bdlaws serves UTF-16; reading it as UTF-8 yields mojibake that
        # silently breaks every selector downstream.
        assert "Limitation Act" in act_page_html
        assert "\x00" not in act_page_html

    def test_bom_is_detected_without_a_declared_charset(self):
        assert decode_html("hello".encode("utf-16")) == "hello"

    def test_utf8_is_left_alone(self):
        assert decode_html("plain ascii".encode("utf-8")) == "plain ascii"


class TestActParsing:
    def test_reads_the_act_title(self, act_page_html):
        assert parse_act_title(act_page_html) == "The Limitation Act, 1908"

    def test_lists_every_section(self, act_page_html):
        assert len(parse_act_page(act_page_html, "88")) == 30

    def test_captures_section_number_heading_and_part(self, act_page_html):
        section = parse_act_page(act_page_html, "88")[4]
        assert section.section_no == "5"
        assert section.heading == "Extension of period in certain cases"
        assert section.part.startswith("Part II")
        assert section.url.endswith("/act-88/section-6447.html")

    def test_part_headings_carry_forward(self, act_page_html):
        sections = parse_act_page(act_page_html, "88")
        assert sections[0].part.startswith("Part I")
        assert sections[3].part.startswith("Part II")

    def test_section_page_yields_head_body_and_part(self, section_page_html):
        heading, body, part = parse_section_page(section_page_html)
        assert heading == "Extension of period in certain cases"
        assert "sufficient cause" in body
        assert part == "Part II"


class TestIndexParsing:
    def test_reads_acts_with_number_and_year(self, act_index_html):
        acts = parse_act_index(act_index_html)
        assert acts
        first = acts[0]
        assert first.act_id == "1315"
        assert first.year == "1799"
        assert first.act_number == "V"
        assert first.url.endswith("/act-1315.html")

    def test_each_act_appears_once(self, act_index_html):
        acts = parse_act_index(act_index_html)
        assert len({a.act_id for a in acts}) == len(acts)


class TestSearch:
    @pytest.fixture
    def corpus(self, conn, act_page_html, section_page_html):
        sections = parse_act_page(act_page_html, "88")
        heading, body, part = parse_section_page(section_page_html)
        for section in sections:
            if section.section_no == "5":
                section.heading, section.body = heading, body
            else:
                section.body = f"Text of section {section.section_no}. {section.heading}."
        act = Act(
            ref=ActRef("88", "The Limitation Act, 1908", "IX", "1908",
                       "http://bdlaws.minlaw.gov.bd/act-88.html"),
            sections=sections,
        )
        store_act(conn, act)
        return conn

    def test_corpus_stats(self, corpus):
        assert corpus_stats(corpus) == {"acts": 1, "sections": 30}

    def test_citation_query_returns_that_section(self, corpus):
        hits = search(corpus, "s. 5 of the Limitation Act")
        assert hits[0].section_no == "5"
        assert hits[0].citation == "section 5, The Limitation Act, 1908"

    def test_bare_section_query_works(self, corpus):
        assert search(corpus, "section 12")[0].section_no == "12"

    def test_keyword_query_finds_the_operative_section(self, corpus):
        hits = search(corpus, "sufficient cause for not preferring the appeal")
        assert hits[0].section_no == "5"

    def test_every_hit_carries_a_verifiable_url(self, corpus):
        for hit in search(corpus, "limitation", limit=5):
            assert hit.url.startswith("http")

    def test_result_body_is_verbatim_not_generated(self, corpus, section_page_html):
        _, body, _ = parse_section_page(section_page_html)
        hit = search(corpus, "s. 5 of the Limitation Act")[0]
        assert hit.body == body

    def test_no_match_returns_empty(self, corpus):
        assert search(corpus, "zzzznotawordzzzz") == []

    def test_empty_query_returns_empty(self, corpus):
        assert search(corpus, "   ") == []

    def test_query_with_fts_metacharacters_does_not_error(self, corpus):
        # A user pasting `"limitation" OR (x)` must not produce an FTS5 syntax error.
        assert isinstance(search(corpus, 'limitation" OR (appeal*)'), list)

    def test_lookup_section_can_filter_by_act(self, corpus):
        assert lookup_section(corpus, "5", "Limitation")
        assert lookup_section(corpus, "5", "Companies") == []

    def test_restoring_an_act_does_not_duplicate_sections(self, corpus, act_page_html):
        sections = parse_act_page(act_page_html, "88")
        for section in sections:
            section.body = "updated text"
        store_act(corpus, Act(
            ref=ActRef("88", "The Limitation Act, 1908", "IX", "1908", "http://x"),
            sections=sections,
        ))
        assert corpus_stats(corpus) == {"acts": 1, "sections": 30}
        hits = search(corpus, "updated text")
        assert hits and len(hits) <= 10
