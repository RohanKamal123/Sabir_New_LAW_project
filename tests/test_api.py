"""API surface. Network-touching endpoints are covered by the scraper tests;
these check the contract the eventual bot and UI depend on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from barrister import api
from barrister.db import connect, init_db


@pytest.fixture
def client(tmp_path):
    def override():
        conn = connect(tmp_path / "api.db")
        try:
            init_db(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    api.app.dependency_overrides[api.get_conn] = override
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


class TestMeta:
    def test_health_reports_the_active_provider(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "drafting_provider" in body
        assert "supremecourt.gov.bd" in body["sources"]["supreme_court"]


class TestUsersAndWatches:
    def test_create_user_and_watch(self, client):
        user = client.post("/users", json={"name": "Sabir", "telegram_chat_id": "1"}).json()
        created = client.post(
            f"/users/{user['id']}/watches", json={"kind": "advocate", "value": "Abu Hanif"}
        )
        assert created.status_code == 201
        watches = client.get(f"/users/{user['id']}/watches").json()
        assert len(watches) == 1
        assert watches[0]["value"] == "Abu Hanif"

    def test_invalid_watch_kind_is_rejected(self, client):
        user = client.post("/users", json={"name": "Sabir"}).json()
        response = client.post(f"/users/{user['id']}/watches", json={"kind": "judge", "value": "X"})
        assert response.status_code == 422

    def test_alerts_start_empty(self, client):
        user = client.post("/users", json={"name": "Sabir"}).json()
        assert client.get(f"/users/{user['id']}/alerts").json() == []


class TestCaseTypes:
    def test_lists_the_full_registry(self, client):
        assert len(client.get("/case-types").json()) == 110

    def test_filters_by_division(self, client):
        appellate = client.get("/case-types?division=1").json()
        assert appellate
        assert all(ct["division_id"] == 1 for ct in appellate)


class TestLimitation:
    def test_computes_a_deadline_with_its_working(self, client):
        response = client.post("/limitation", json={
            "start_date": "2026-01-15", "days": 90, "proceeding": "appeal",
            "copy_applied_on": "2026-01-20", "copy_ready_on": "2026-02-03",
        })
        body = response.json()
        assert body["deadline"] == "2026-04-30"
        assert body["excluded_days"] == 15
        assert any(step["rule"] == "s. 12(2)" for step in body["steps"])
        assert body["citations"]

    def test_unverified_article_is_a_conflict_not_a_silent_answer(self, client):
        response = client.post("/limitation", json={
            "start_date": "2026-01-15", "article": "152", "proceeding": "appeal",
        })
        assert response.status_code == 409
        assert "not been verified" in response.json()["detail"]

    def test_unverified_article_can_be_overridden(self, client):
        response = client.post("/limitation", json={
            "start_date": "2026-01-15", "article": "152",
            "proceeding": "appeal", "allow_unverified": True,
        })
        assert response.status_code == 200
        assert response.json()["warnings"][0].startswith("UNVERIFIED RULE")

    def test_missing_period_is_a_bad_request(self, client):
        assert client.post("/limitation", json={"start_date": "2026-01-15"}).status_code == 400

    def test_review_queue_is_exposed(self, client):
        queue = client.get("/limitation/review-queue").json()
        assert queue
        assert "article" in queue[0]


class TestStatutes:
    def test_search_on_an_empty_corpus_returns_nothing(self, client):
        assert client.get("/statutes/search?q=limitation").json() == []

    def test_stats_on_an_empty_corpus(self, client):
        assert client.get("/statutes/stats").json() == {"acts": 0, "sections": 0}


class TestDrafting:
    def test_lists_templates(self, client):
        assert "writ_petition" in client.get("/drafting/templates").json()

    def test_drafts_without_a_model_configured(self, client):
        response = client.post("/drafting/draft", json={
            "template": "writ_petition",
            "petitioners": ["A"], "respondents": ["B"],
            "facts": "Some facts.", "year": "2026",
            "prayers": ["Issue a Rule Nisi"],
        })
        body = response.json()
        assert response.status_code == 200
        assert "IN THE SUPREME COURT OF BANGLADESH" in body["text"]
        assert body["warnings"]

    def test_a_bad_template_is_a_bad_request(self, client):
        response = client.post("/drafting/draft", json={
            "template": "no_such_template", "petitioners": ["A"], "respondents": ["B"],
        })
        assert response.status_code == 400
