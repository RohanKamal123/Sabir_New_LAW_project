"""Drafting: templates own the structure, the model only writes narrative,
and no unsupplied citation gets through without a warning."""

from __future__ import annotations

import json

import httpx
import pytest

from barrister.config import Settings
from barrister.services.drafting import (
    AnthropicBackend, DeepSeekBackend, DraftRequest, DraftingError, NullBackend,
    available_templates, build_backend, build_prompt, check_citations, draft,
)


@pytest.fixture
def request_():
    return DraftRequest(
        template="writ_petition",
        year="2026",
        petitioners=["Md. Karim Uddin of Dhaka"],
        respondents=["Bangladesh, represented by the Secretary, Ministry of Land",
                     "The Deputy Commissioner, Dhaka"],
        subject_matter="Challenge to the acquisition notice dated 12.03.2026.",
        facts="The petitioner owns 3 katha at Mirpur. No hearing was given.",
        grounds=["That the notice violates natural justice."],
        prayers=["Issue a Rule Nisi", "Stay the notice dated 12.03.2026"],
    )


class TestTemplates:
    def test_templates_are_discoverable(self):
        assert "writ_petition" in available_templates()

    def test_renders_the_fixed_court_form(self, request_):
        text = draft(request_, backend=NullBackend()).text
        assert "IN THE SUPREME COURT OF BANGLADESH" in text
        assert "HIGH COURT DIVISION" in text
        assert "S H E W E T H" in text
        assert "shall ever pray" in text

    def test_pluralises_parties_correctly(self, request_):
        text = draft(request_, backend=NullBackend()).text
        assert "... PETITIONER\n" in text        # one petitioner
        assert "... RESPONDENTS" in text          # two respondents

    def test_prayers_are_lettered_in_roman(self, request_):
        text = draft(request_, backend=NullBackend()).text
        assert "(i) Issue a Rule Nisi" in text
        assert "(ii) Stay the notice" in text

    def test_grounds_are_numbered(self, request_):
        text = draft(request_, backend=NullBackend()).text
        assert "1. That the notice violates natural justice." in text

    def test_leave_to_appeal_template_needs_its_own_fields(self, request_):
        request_.template = "leave_to_appeal"
        request_.extra = {"impugned_date": "01.02.2026", "impugned_case": "Writ Petition 1/2020"}
        text = draft(request_, backend=NullBackend()).text
        assert "APPELLATE DIVISION" in text
        assert "CIVIL PETITION FOR LEAVE TO APPEAL" in text
        assert "01.02.2026" in text

    def test_a_missing_template_variable_fails_loudly(self, request_):
        request_.template = "leave_to_appeal"   # needs impugned_date / impugned_case
        with pytest.raises(Exception):
            draft(request_, backend=NullBackend())


class TestWithoutAModel:
    def test_narrative_is_an_explicit_gap(self, request_):
        result = draft(request_, backend=NullBackend())
        assert "NARRATIVE NOT GENERATED" in result.text
        assert result.provider == "none"

    def test_it_still_warns_that_it_is_a_draft(self, request_):
        result = draft(request_, backend=NullBackend())
        assert any("verify every date" in w for w in result.warnings)


class TestPrompt:
    def test_prompt_carries_the_facts_and_parties(self, request_):
        prompt = build_prompt(request_)
        assert "Md. Karim Uddin of Dhaka" in prompt
        assert "3 katha at Mirpur" in prompt

    def test_prompt_forbids_citations_when_none_supplied(self, request_):
        assert "Do not cite any case or law report" in build_prompt(request_)

    def test_prompt_lists_supplied_authorities_when_given(self, request_):
        request_.supplied_authorities = ["45 DLR (AD) 123"]
        prompt = build_prompt(request_)
        assert "45 DLR (AD) 123" in prompt
        assert "these and only these" in prompt


class TestCitationGuard:
    def test_flags_a_citation_the_barrister_did_not_supply(self):
        warnings = check_citations("As held in 45 DLR (AD) 123, the notice is bad.", [])
        assert len(warnings) == 1
        assert "45 DLR (AD) 123" in warnings[0]

    def test_accepts_a_supplied_citation(self):
        assert check_citations("As held in 45 DLR (AD) 123.", ["45 DLR (AD) 123"]) == []

    def test_flags_only_the_unsupplied_one(self):
        warnings = check_citations("See 12 BLC 45 and 3 MLR 210.", ["12 BLC 45"])
        assert len(warnings) == 1
        assert "3 MLR 210" in warnings[0]

    def test_prose_without_citations_is_clean(self):
        assert check_citations("The petitioner was not heard.", []) == []

    def test_guard_runs_on_generated_drafts(self, request_):
        class Hallucinating:
            name, model = "test", "test-model"

            def generate(self, system, prompt, *, max_tokens):
                return "1. As held in 99 DLR (AD) 1, the notice is bad."

        result = draft(request_, backend=Hallucinating())
        assert any("UNVERIFIED CITATION" in w for w in result.warnings)


class TestProviderSelection:
    def test_auto_falls_back_to_templates_only(self):
        backend = build_backend(Settings(drafting_provider="auto"))
        assert isinstance(backend, NullBackend)

    def test_auto_prefers_anthropic_when_both_keys_are_set(self):
        settings = Settings(anthropic_api_key="sk-a", deepseek_api_key="sk-d")
        assert settings.resolve_provider() == "anthropic"

    def test_auto_uses_deepseek_when_only_that_key_is_set(self):
        settings = Settings(deepseek_api_key="sk-d", drafting_provider="auto")
        assert isinstance(build_backend(settings), DeepSeekBackend)

    def test_explicit_provider_overrides_auto_detection(self):
        settings = Settings(
            drafting_provider="deepseek", anthropic_api_key="sk-a", deepseek_api_key="sk-d"
        )
        assert isinstance(build_backend(settings), DeepSeekBackend)

    def test_anthropic_without_a_key_is_rejected(self):
        with pytest.raises(DraftingError, match="ANTHROPIC_API_KEY"):
            AnthropicBackend(Settings(anthropic_api_key=""))

    def test_deepseek_without_a_key_is_rejected(self):
        with pytest.raises(DraftingError, match="DEEPSEEK_API_KEY"):
            DeepSeekBackend(Settings(deepseek_api_key=""))

    def test_unknown_provider_is_rejected(self):
        with pytest.raises(DraftingError, match="unknown drafting provider"):
            build_backend(Settings(drafting_provider="llama-at-home"))


class TestDeepSeekBackend:
    def _backend(self, handler, **overrides):
        settings = Settings(deepseek_api_key="sk-test", **overrides)
        return DeepSeekBackend(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_posts_to_the_openai_compatible_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "1. Text."}}]})

        assert self._backend(handler).generate("SYS", "PROMPT", max_tokens=64000) == "1. Text."
        assert seen["url"] == "https://api.deepseek.com/chat/completions"
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}

    def test_clamps_max_tokens_to_the_provider_ceiling(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        self._backend(handler).generate("s", "p", max_tokens=64000)
        assert seen["body"]["max_tokens"] == 8192

    def test_respects_a_custom_base_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        self._backend(handler, deepseek_base_url="https://proxy.example/v1").generate(
            "s", "p", max_tokens=100
        )
        assert seen["url"] == "https://proxy.example/v1/chat/completions"

    def test_http_error_becomes_a_drafting_error(self):
        def handler(request):
            return httpx.Response(401, text="Invalid API key")

        with pytest.raises(DraftingError, match="401"):
            self._backend(handler).generate("s", "p", max_tokens=100)

    def test_unexpected_payload_shape_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={"unexpected": True})

        with pytest.raises(DraftingError, match="unexpected DeepSeek response"):
            self._backend(handler).generate("s", "p", max_tokens=100)

    def test_network_failure_becomes_a_drafting_error(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        with pytest.raises(DraftingError, match="DeepSeek request failed"):
            self._backend(handler).generate("s", "p", max_tokens=100)
