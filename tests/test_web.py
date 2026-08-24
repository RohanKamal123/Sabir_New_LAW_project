"""The web UI. Checks that each page renders the real data, not that it is pretty."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from barrister import api, web
from barrister.db import connect, init_db
from barrister.http import decode_html
from barrister.scrapers.cause_list import parse_cause_list
from barrister.services import matters as M
from barrister.services.notify import NullNotifier
from barrister.services.watchlist import add_user, run_sweep


@pytest.fixture
def db(tmp_path):
    return tmp_path / "web.db"


@pytest.fixture
def client(db):
    def override():
        conn = connect(db)
        try:
            init_db(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    api.app.dependency_overrides[api.get_conn] = override
    api.app.dependency_overrides[web.get_conn] = override
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides[web.get_conn] = api.get_conn


@pytest.fixture
def seeded(db, cause_list_html):
    """A barrister with a file, a listing today and a deadline."""
    conn = connect(db)
    init_db(conn)
    user = add_user(conn, "Sabir Rahman", telegram_chat_id="1")
    matter = M.open_matter(conn, user, "Ruhul Amin v Aiyub Ali")
    M.link_case(conn, matter, case_type="First Appeal", case_number="226", case_year="2013")
    M.add_note(conn, matter, "Conference with client")
    M.log_time(conn, matter, 90, "Settling grounds", rate=6000)
    M.add_deadline(conn, matter, "File paper book", date.today() + timedelta(days=3))

    entries = parse_cause_list(cause_list_html)
    for entry in entries:
        entry.list_date = date.today().strftime("%d/%m/%Y")
    run_sweep(conn, entries, NullNotifier())
    conn.commit()
    conn.close()
    return {"user": user, "matter": matter}


class TestChrome:
    def test_stylesheet_is_served(self, client):
        response = client.get("/static/app.css")
        assert response.status_code == 200
        assert "--oxblood" in response.text

    def test_every_page_renders(self, client):
        for path in ("/", "/matters", "/diary", "/statutes", "/limitation", "/drafting"):
            assert client.get(path).status_code == 200, path

    def test_nav_marks_the_current_page(self, client):
        assert 'aria-current="page"' in client.get("/matters").text

    def test_pages_carry_the_provenance_footer(self, client):
        assert "supremecourt.gov.bd" in client.get("/").text


class TestTodayPage:
    def test_without_a_user_it_explains_how_to_register(self, client):
        assert "adduser" in client.get("/").text

    def test_shows_todays_listings(self, client, seeded):
        body = client.get("/").text
        assert "First Appeal 226/2013" in body
        assert "Annex Building Court No. 18" in body

    def test_names_the_file_against_the_listing(self, client, seeded):
        assert "MAT-" in client.get("/").text

    def test_shows_parsed_parties_not_the_raw_cell(self, client, seeded):
        body = client.get("/").text
        assert "Md. Ruhul Amin and ors." in body
        assert "[Adv :" not in body

    def test_shows_the_diary(self, client, seeded):
        assert "File paper book" in client.get("/").text


class TestMatters:
    def test_lists_files(self, client, seeded):
        assert "Ruhul Amin v Aiyub Ali" in client.get("/matters").text

    def test_empty_state_explains_the_command(self, client):
        assert "matter open" in client.get("/matters").text

    def test_status_filter(self, client, seeded):
        assert "Ruhul Amin" in client.get("/matters?status=open").text
        assert "Ruhul Amin" not in client.get("/matters?status=closed").text

    def test_file_page_shows_the_whole_file(self, client, seeded):
        body = client.get(f"/matters/{seeded['matter']}").text
        assert "First Appeal 226/2013" in body
        assert "Conference with client" in body
        assert "Settling grounds" in body
        assert "File paper book" in body

    def test_missing_file_is_a_404(self, client):
        assert client.get("/matters/9999").status_code == 404


class TestDiary:
    def test_shows_deadlines_with_a_countdown(self, client, seeded):
        body = client.get("/diary").text
        assert "File paper book" in body
        assert "3d" in body

    def test_horizon_is_respected(self, client, seeded):
        assert "File paper book" in client.get("/diary?days=30").text

    def test_empty_diary_says_so(self, client):
        assert "Nothing falls due" in client.get("/diary").text


class TestStatutes:
    def test_empty_corpus_offers_the_sync_command(self, client):
        assert "statutes sync" in client.get("/statutes").text

    def test_no_results_is_reported(self, client):
        assert "Nothing matched" in client.get("/statutes?q=zzzznotaword").text


class TestLimitation:
    def test_form_renders_without_input(self, client):
        assert "Prescribed period" in client.get("/limitation").text

    def test_computes_and_shows_working(self, client):
        body = client.get(
            "/limitation?start=2026-01-15&days=90&proceeding=appeal"
            "&copy_applied=2026-01-20&copy_ready=2026-02-03"
        ).text
        assert "2026-04-30" in body
        assert "s. 12(2)" in body
        assert "Limitation Act 1908" in body

    def test_expired_period_is_marked(self, client):
        body = client.get("/limitation?start=2020-01-15&days=90&proceeding=appeal").text
        assert "expired" in body
        assert "days ago" in body

    def test_reversed_copy_dates_are_reported_not_crashed(self, client):
        response = client.get(
            "/limitation?start=2026-01-15&days=90&proceeding=appeal"
            "&copy_applied=2026-02-03&copy_ready=2026-01-20"
        )
        assert response.status_code == 200
        assert "Cannot compute" in response.text


class TestDrafting:
    def test_form_lists_templates(self, client):
        assert "Writ Petition" in client.get("/drafting").text

    def test_submitting_renders_a_draft(self, client):
        response = client.post("/drafting", data={
            "template": "writ_petition",
            "petitioners": "Md. Karim Uddin",
            "respondents": "Bangladesh\nThe Deputy Commissioner",
            "subject": "Acquisition notice",
            "facts": "The petitioner owns 3 katha at Mirpur.",
            "prayers": "Issue a Rule Nisi",
            "grounds": "",
            "authorities": "",
        })
        assert response.status_code == 200
        assert "IN THE SUPREME COURT OF BANGLADESH" in response.text
        assert "Issue a Rule Nisi" in response.text

    def test_a_bad_template_is_reported_on_the_page(self, client):
        response = client.post("/drafting", data={
            "template": "no_such_template", "petitioners": "A", "respondents": "B",
            "subject": "", "facts": "", "prayers": "", "grounds": "", "authorities": "",
        })
        assert response.status_code == 200
        assert "Could not draft" in response.text

    def test_missing_parties_get_explicit_placeholders(self, client):
        response = client.post("/drafting", data={
            "template": "writ_petition", "petitioners": "", "respondents": "",
            "subject": "", "facts": "x", "prayers": "", "grounds": "", "authorities": "",
        })
        assert "[PETITIONER TO BE SUPPLIED]" in response.text
