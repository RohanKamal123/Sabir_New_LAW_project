"""One-off extractor: the First Schedule to the Limitation Act 1908 -> JSON rules.

The Schedule is published only as a PDF (a three-column table), so this walks
the PDF's text boxes, clusters them into rows by vertical position, and assigns
each box to a column by horizontal position.

The output is *unverified by construction*. Every article is emitted with
``"verified": false``; a barrister must check each one against the official
text before it is allowed to produce a filing deadline. See
``barrister/services/limitation.py`` for how that flag is enforced.

Usage:  python3 tools/extract_limitation_schedule.py <schedule.pdf> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextContainer

# Column boundaries in PDF points, read off the rendered layout.
COL_DESCRIPTION = (150, 320)
COL_PERIOD = (320, 395)
COL_TRIGGER = (395, 560)

ARTICLE = re.compile(r"^(?P<no>\d+[A-Z]*)\s*[.．]\s*(?P<rest>.*)$", re.DOTALL)
NOISE = re.compile(r"Copyright @|Ministry of Law|^\d+$|^Limitation$|^\[1908", re.IGNORECASE)

PERIOD_UNITS = {
    "year": 365, "years": 365,
    "month": 30, "months": 30,
    "day": 1, "days": 1,
}
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90,
    "hundred": 100,
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_period(text: str) -> dict | None:
    """"Ninety days." -> {'value': 90, 'unit': 'days', 'approx_days': 90}."""
    cleaned = _clean(text).lower().rstrip(". ")
    if not cleaned:
        return None

    digits = re.search(r"(\d+)\s+(year|years|month|months|day|days)", cleaned)
    if digits:
        value, unit = int(digits.group(1)), digits.group(2)
    else:
        words = re.search(
            r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|sixty|ninety)\b"
            r"(?:[\s-]+(hundred)[\s-]+(?:and[\s-]+)?"
            r"(one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|sixty|ninety)?)?"
            r"\s+(year|years|month|months|day|days)",
            cleaned,
        )
        if not words:
            return None
        value = NUMBER_WORDS[words.group(1)]
        if words.group(2):
            value *= 100
            if words.group(3):
                value += NUMBER_WORDS[words.group(3)]
        unit = words.group(4)

    unit = unit.rstrip("s") + "s"
    return {"value": value, "unit": unit, "approx_days": value * PERIOD_UNITS[unit]}


def extract(pdf_path: str) -> list[dict]:
    rows_by_page: list[dict] = []

    for page in extract_pages(pdf_path, laparams=LAParams(detect_vertical=False, boxes_flow=None)):
        boxes = [
            (round(el.y1), round(el.x0), _clean(el.get_text()))
            for el in page
            if isinstance(el, LTTextContainer) and _clean(el.get_text())
        ]
        # Cluster boxes into rows: same table row shares a baseline within a few points.
        rows: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for y, x, text in boxes:
            if NOISE.search(text):
                continue
            bucket = y // 8  # tolerate small baseline jitter
            if COL_DESCRIPTION[0] <= x < COL_DESCRIPTION[1]:
                rows[bucket]["description"].append((y, x, text))
            elif COL_PERIOD[0] <= x < COL_PERIOD[1]:
                rows[bucket]["period"].append((y, x, text))
            elif COL_TRIGGER[0] <= x < COL_TRIGGER[1]:
                rows[bucket]["trigger"].append((y, x, text))

        for bucket in sorted(rows, reverse=True):
            cells = rows[bucket]
            rows_by_page.append(
                {
                    "y": bucket * 8,
                    "description": " ".join(t for _, _, t in cells.get("description", [])),
                    "period": " ".join(t for _, _, t in cells.get("period", [])),
                    "trigger": " ".join(t for _, _, t in cells.get("trigger", [])),
                }
            )

    # Stitch: a row starting "NN." opens an article; rows without a number are
    # continuation lines of the article above (the PDF wraps long descriptions).
    articles: list[dict] = []
    for row in rows_by_page:
        match = ARTICLE.match(row["description"])
        if match:
            articles.append(
                {
                    "article": match.group("no"),
                    "description": _clean(match.group("rest")),
                    "period_text": _clean(row["period"]),
                    "trigger": _clean(row["trigger"]),
                }
            )
        elif articles:
            current = articles[-1]
            if row["description"]:
                current["description"] = _clean(current["description"] + " " + row["description"])
            if row["period"] and not current["period_text"]:
                current["period_text"] = _clean(row["period"])
            if row["trigger"]:
                current["trigger"] = _clean(current["trigger"] + " " + row["trigger"])

    # De-duplicate repeated article numbers, keeping the fullest capture.
    best: dict[str, dict] = {}
    for article in articles:
        key = article["article"]
        scored = len(article["description"]) + len(article["period_text"])
        if key not in best or scored > best[key]["_score"]:
            article["_score"] = scored
            best[key] = article

    output: list[dict] = []
    for key in sorted(best, key=lambda k: (int(re.sub(r"\D", "", k) or 0), k)):
        article = best.pop(key)
        article.pop("_score", None)
        _unmerge_period_and_trigger(article)
        article["period"] = parse_period(article["period_text"])
        article["verified"] = False
        output.append(article)
    return output


# A narrow table row sometimes lands the period and the trigger in one text
# box, e.g. "Seven days The date of the sentence.". Split the period phrase off
# the front and treat the remainder as the trigger, but only when the trigger
# column came back empty — otherwise we would be overwriting real data.
_PERIOD_PHRASE = re.compile(
    r"^\s*(?P<period>(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|"
    r"fifteen|twenty|thirty|sixty|ninety)(?:[\s-]+hundred(?:[\s-]+and)?"
    r"(?:[\s-]+\w+)?)?\s+(?:year|month|day)s?)\s*[.．]*\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _unmerge_period_and_trigger(article: dict) -> None:
    if article["trigger"]:
        return
    match = _PERIOD_PHRASE.match(article["period_text"])
    if not match:
        return
    rest = _clean(match.group("rest"))
    if rest:
        article["period_text"] = _clean(match.group("period"))
        article["trigger"] = rest


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    articles = extract(sys.argv[1])
    payload = {
        "source": "The Limitation Act, 1908 — First Schedule",
        "source_url": "http://bdlaws.minlaw.gov.bd/act-88.html",
        "extracted_by": "tools/extract_limitation_schedule.py",
        "warning": (
            "Machine-extracted from a PDF table. Every article is unverified "
            "until a lawyer checks it against the official text."
        ),
        "articles": articles,
    }
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    parsed = sum(1 for a in articles if a["period"])
    print(f"extracted {len(articles)} articles; {parsed} with a machine-readable period")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
