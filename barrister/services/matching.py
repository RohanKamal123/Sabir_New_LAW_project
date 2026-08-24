"""Matching a barrister's watch terms against cause list entries.

Name matching on this data is genuinely hard and the failure modes are
asymmetric: a false positive costs a barrister ten seconds of reading, a false
negative costs them a missed hearing. So matching is deliberately generous,
and every match reports *why* it matched so the alert can show its working.

The normalisation handles what the cause list actually does to names:
honorifics (Mr./Mrs./Mst./Md./Dr.), inconsistent spacing around initials,
and the fact that the same advocate appears as "Mr. Md. Abu Hanif" one day and
"Md. Abu Hanif" the next.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..scrapers.cause_list import CauseListEntry

_HONORIFICS = {
    "mr", "mrs", "ms", "mst", "mosammat", "most", "dr", "adv", "advocate",
    "justice", "barrister", "hon", "honble", "honourable", "sri", "sree",
    "alhaj", "md", "mohammad", "mohammed", "muhammad",
}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    """Fold a name to a comparable form.

    ``"Mr. Md. Abu  Hanif"`` and ``"MD ABU HANIF"`` both become ``"abu hanif"``.
    """
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.sub(" ", text).split() if t]
    stripped = [t for t in tokens if t not in _HONORIFICS]
    # Never normalise a name out of existence: "Md. Md." is still a name.
    return " ".join(stripped or tokens)


def normalize_case_number(value: str) -> str:
    """``"Civil Revision 2347/2007"`` -> ``"civil revision 2347/2007"``.

    Also tolerates the shorthand practitioners actually type, like
    ``"CR 2347 of 2007"``, by collapsing "of" to "/".
    """
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = re.sub(r"\s+of\s+", "/", text)
    text = re.sub(r"[^\w/()]+", " ", text)
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class Match:
    entry: CauseListEntry
    kind: str          # advocate | party | case
    term: str          # what the user asked to watch
    matched_on: str    # the text in the listing that matched
    reason: str        # human-readable, shown in the alert

    @property
    def dedupe_key(self) -> str:
        e = self.entry
        return "|".join(
            [e.list_date, e.bench_id or "", str(e.serial), e.case_ref, self.kind, self.term]
        )


def _tokens(value: str) -> set[str]:
    return set(value.split())


def _name_matches(term_norm: str, candidate: str) -> bool:
    """True when a watched name plausibly refers to this listed name.

    Substring first (cheap, catches the common case), then a token-subset test
    so a watch on "Abu Hanif" still fires on "Md. Abu Hanif Sarker".
    """
    cand_norm = normalize_name(candidate)
    if not term_norm or not cand_norm:
        return False
    if term_norm in cand_norm or cand_norm in term_norm:
        return True
    term_tokens, cand_tokens = _tokens(term_norm), _tokens(cand_norm)
    if len(term_tokens) < 2:
        return False
    return term_tokens.issubset(cand_tokens)


def match_entry(entry: CauseListEntry, watches: Sequence[tuple[str, str]]) -> list[Match]:
    """Match one listing against ``(kind, value)`` watch pairs."""
    matches: list[Match] = []

    for kind, term in watches:
        if kind == "advocate":
            term_norm = normalize_name(term)
            for advocate in entry.advocates:
                if _name_matches(term_norm, advocate):
                    matches.append(
                        Match(
                            entry, kind, term, advocate,
                            f"listed as advocate: {advocate}",
                        )
                    )
                    break

        elif kind == "party":
            term_norm = normalize_name(term)
            for side, text in (("petitioner", entry.petitioner), ("respondent", entry.respondent)):
                if text and _name_matches(term_norm, text):
                    matches.append(
                        Match(entry, kind, term, text, f"appears as {side}: {text}")
                    )
                    break

        elif kind == "case":
            term_norm = normalize_case_number(term)
            candidates = [normalize_case_number(entry.case_ref)]
            if entry.case_number and entry.case_year:
                candidates.append(normalize_case_number(f"{entry.case_number}/{entry.case_year}"))
            for candidate in candidates:
                if term_norm and (term_norm in candidate or candidate in term_norm):
                    matches.append(
                        Match(entry, kind, term, entry.case_ref, f"case listed: {entry.case_ref}")
                    )
                    break

    return matches


def match_all(
    entries: Iterable[CauseListEntry], watches: Sequence[tuple[str, str]]
) -> list[Match]:
    results: list[Match] = []
    for entry in entries:
        results.extend(match_entry(entry, watches))
    return results
