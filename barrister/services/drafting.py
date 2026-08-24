"""Template-based drafting.

The shape of a Bangladeshi Supreme Court petition is fixed by convention — the
cause title, the "SHEWETH", the numbered grounds, the prayer, the "act of
kindness" close. That structure is a *template*, not something a model should be
inventing each time, so the templates own the form and the model only writes the
narrative paragraphs from facts the barrister supplies.

That split is the whole safety argument for this feature:

* the model never chooses the document's structure;
* the model is never asked for case law, and the system prompt forbids citing
  any authority the barrister did not supply — an invented DLR citation is the
  single fastest way to lose a practitioner's trust;
* with no model configured at all, the templates still render with the facts
  laid out and the narrative left as a marked gap, which is a usable skeleton.

Two providers are supported. Claude is the default and goes through the official
`anthropic` SDK; DeepSeek goes through its OpenAI-compatible HTTP endpoint. They
are separate backends behind one protocol — neither borrows the other's client.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ..config import Settings, settings as default_settings

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "drafts"

NARRATIVE_PLACEHOLDER = (
    "[[ NARRATIVE NOT GENERATED — no drafting model is configured. "
    "Set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY, or write this section by hand. ]]"
)

SYSTEM_PROMPT = """\
You are assisting a barrister of the Supreme Court of Bangladesh in drafting the \
narrative portion of a court document. You are not giving legal advice and you are \
not deciding the document's structure — that is fixed by the template.

Rules you must follow without exception:

1. Use ONLY the facts supplied to you. Do not add facts, dates, sums, names or \
   procedural history that were not given. If something needed is missing, write \
   "[TO BE SUPPLIED: <what is missing>]" inline rather than inventing it.
2. NEVER cite a case, judgment or law report (DLR, BLD, BLC, BLT, MLR or any \
   other) unless the barrister supplied that citation verbatim in the facts. Do \
   not paraphrase from memory. If a proposition needs authority that was not \
   supplied, write "[AUTHORITY TO BE SUPPLIED]".
3. You may refer to statutes and constitutional articles only where they were \
   supplied to you. Quote them exactly as given.
4. Write in the register of Bangladeshi Supreme Court practice: formal, \
   third-person, numbered paragraphs, "the petitioner" / "the respondent \
   No. 1", past tense for facts, present for the grievance.
5. Number every paragraph sequentially starting at 1. Do not add headings, a \
   cause title, a prayer, or a signature block — the template supplies those.
6. Be concise. A fact stated once does not need restating.

Return only the numbered narrative paragraphs. No preamble, no commentary."""


class DraftingError(RuntimeError):
    """Raised when a configured drafting backend fails."""


# --------------------------------------------------------------------------
# template rendering
# --------------------------------------------------------------------------

_ROMAN = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _to_roman(number: int) -> str:
    """Lower-case Roman numerals, as prayers are lettered in BD practice."""
    if number <= 0:
        return str(number)
    out: list[str] = []
    remaining = number
    for value, numeral in _ROMAN:
        count, remaining = divmod(remaining, value)
        out.append(numeral * count)
    return "".join(out)


def _environment(template_dir: Path | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        undefined=StrictUndefined,       # a missing fact should fail loudly
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        trim_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["roman"] = _to_roman
    return env


def available_templates(template_dir: Path | None = None) -> list[str]:
    directory = template_dir or TEMPLATE_DIR
    return sorted(p.stem for p in directory.glob("*.j2"))


@dataclass
class DraftRequest:
    """Everything a draft needs: the form, the parties, and the facts."""

    template: str
    petitioners: Sequence[str]
    respondents: Sequence[str]
    facts: str
    year: str = ""
    subject_matter: str = ""
    prayers: Sequence[str] = field(default_factory=list)
    grounds: Sequence[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    supplied_authorities: Sequence[str] = field(default_factory=list)

    def context(self, body: str) -> dict[str, Any]:
        context: dict[str, Any] = {
            "petitioners": list(self.petitioners),
            "respondents": list(self.respondents),
            "year": self.year,
            "subject_matter": self.subject_matter,
            "prayers": list(self.prayers),
            "grounds": list(self.grounds),
            "body": body,
        }
        context.update(self.extra)
        return context


@dataclass
class Draft:
    text: str
    template: str
    provider: str
    model: str | None
    narrative: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

class DraftingBackend(Protocol):
    name: str
    model: str

    def generate(self, system: str, prompt: str, *, max_tokens: int) -> str:
        """Return the narrative paragraphs for a draft."""


class NullBackend:
    """Renders the template with the narrative left as an explicit gap."""

    name = "none"
    model = ""

    def generate(self, system: str, prompt: str, *, max_tokens: int) -> str:
        return NARRATIVE_PLACEHOLDER


class AnthropicBackend:
    """Claude via the official `anthropic` SDK.

    Streams because a full petition narrative can be long, and a long
    non-streaming request risks an HTTP timeout. Refusal fallbacks are enabled
    so a policy decline is retried on another model inside the same call rather
    than returning an empty draft.
    """

    name = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self.model = self.settings.drafting_model
        if not self.settings.anthropic_api_key:
            raise DraftingError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise DraftingError("the `anthropic` package is not installed") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)

    def generate(self, system: str, prompt: str, *, max_tokens: int) -> str:
        try:
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except self._anthropic.APIError as exc:
            raise DraftingError(f"Claude request failed: {exc}") from exc

        if message.stop_reason == "refusal":
            detail = getattr(message, "stop_details", None)
            raise DraftingError(
                "Claude declined to draft this document"
                + (f" ({detail.category})" if detail is not None else "")
            )

        return "".join(
            block.text for block in message.content if block.type == "text"
        ).strip()


class DeepSeekBackend:
    """DeepSeek via its OpenAI-compatible chat-completions endpoint.

    DeepSeek ships no first-party Python SDK; the documented integration is the
    OpenAI-compatible REST surface, so this talks to it over plain HTTP rather
    than pulling in an SDK shim.
    """

    name = "deepseek"

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or default_settings
        self.model = self.settings.deepseek_model
        if not self.settings.deepseek_api_key:
            raise DraftingError("DEEPSEEK_API_KEY is not set")
        self._client = client or httpx.Client(timeout=300.0)

    @property
    def _url(self) -> str:
        return self.settings.deepseek_base_url.rstrip("/") + "/chat/completions"

    def generate(self, system: str, prompt: str, *, max_tokens: int) -> str:
        # DeepSeek caps max_tokens well below Claude's; clamp rather than 400.
        payload = {
            "model": self.model,
            "max_tokens": min(max_tokens, 8192),
            "temperature": 0.2,   # drafting wants consistency, not flourish
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = self._client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise DraftingError(f"DeepSeek request failed: {exc}") from exc

        if response.status_code >= 400:
            raise DraftingError(
                f"DeepSeek returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise DraftingError(f"unexpected DeepSeek response shape: {exc}") from exc


def build_backend(settings: Settings | None = None) -> DraftingBackend:
    """Pick a backend from configuration, falling back to the template-only one."""
    settings = settings or default_settings
    provider = settings.resolve_provider()

    if provider == "anthropic":
        return AnthropicBackend(settings)
    if provider == "deepseek":
        return DeepSeekBackend(settings)
    if provider == "none":
        return NullBackend()
    raise DraftingError(
        f"unknown drafting provider {provider!r}; expected 'anthropic', 'deepseek' or 'auto'"
    )


# --------------------------------------------------------------------------
# drafting
# --------------------------------------------------------------------------

def build_prompt(request: DraftRequest) -> str:
    """Assemble the user turn: the form wanted, the parties, and the facts."""
    lines = [
        f"Document type: {request.template.replace('_', ' ')}",
        "",
        "Petitioner(s): " + "; ".join(request.petitioners),
        "Respondent(s): " + "; ".join(request.respondents),
    ]
    if request.subject_matter:
        lines += ["", f"Subject matter: {request.subject_matter}"]
    if request.supplied_authorities:
        lines += [
            "",
            "Authorities supplied by the barrister — you may cite these and only these, "
            "exactly as written:",
        ]
        lines += [f"  - {a}" for a in request.supplied_authorities]
    else:
        lines += [
            "",
            "No authorities have been supplied. Do not cite any case or law report.",
        ]
    if request.prayers:
        lines += ["", "The prayer will ask the Court to:"]
        lines += [f"  - {p}" for p in request.prayers]

    lines += [
        "",
        "Facts as supplied by the barrister:",
        "---",
        request.facts.strip(),
        "---",
        "",
        "Write the numbered narrative paragraphs for this document.",
    ]
    return "\n".join(lines)


# "45 DLR (AD) 123", "12 BLC 45", "3 MLR 210" — volume, reporter, optional
# division in parentheses, page. Bounded so a match is the citation itself and
# not the sentence it sits in.
_CITATION = re.compile(
    r"\b\d{1,3}\s*(?:DLR|BLD|BLC|BLT|MLR|ADC|BSCR)\s*(?:\([^)\n]{1,12}\))?\s*\d{1,4}\b",
    re.IGNORECASE,
)


def check_citations(narrative: str, supplied: Sequence[str]) -> list[str]:
    """Flag any law-report citation that the barrister did not supply.

    A last line of defence behind the system prompt. It is a string check, not
    a verification of the citation's existence — this product cannot verify
    Bangladeshi case law, which is exactly why it must not emit any.
    """
    supplied_normalised = [re.sub(r"\s+", " ", s).casefold() for s in supplied]
    warnings: list[str] = []
    for found in _CITATION.findall(narrative):
        cleaned = re.sub(r"\s+", " ", found).strip()
        if not any(cleaned.casefold() in s for s in supplied_normalised):
            warnings.append(
                f"UNVERIFIED CITATION in the draft: {cleaned!r}. It was not among the "
                "authorities you supplied — check it against the report before filing."
            )
    return warnings


def draft(
    request: DraftRequest,
    *,
    backend: DraftingBackend | None = None,
    settings: Settings | None = None,
    template_dir: Path | None = None,
) -> Draft:
    """Render a full document: template structure + generated narrative."""
    settings = settings or default_settings
    backend = backend or build_backend(settings)

    warnings: list[str] = []
    if isinstance(backend, NullBackend):
        warnings.append(
            "No drafting model configured — the narrative section is a placeholder."
        )
        narrative = NARRATIVE_PLACEHOLDER
    else:
        narrative = backend.generate(
            SYSTEM_PROMPT, build_prompt(request), max_tokens=settings.drafting_max_tokens
        )
        warnings.extend(check_citations(narrative, request.supplied_authorities))

    env = _environment(template_dir)
    template = env.get_template(f"{request.template}.j2")
    text = template.render(**request.context(narrative))

    warnings.append(
        "AI-assisted draft. Read every paragraph and verify every date, figure and "
        "authority before it is settled or filed."
    )

    return Draft(
        text=text,
        template=request.template,
        provider=backend.name,
        model=backend.model or None,
        narrative=narrative,
        warnings=warnings,
    )
