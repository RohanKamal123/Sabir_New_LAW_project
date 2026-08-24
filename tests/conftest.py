from __future__ import annotations

from pathlib import Path

import pytest

from barrister.db import session as db_session
from barrister.http import decode_html

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    """Read a fixture through the same decoder the scrapers use."""
    return decode_html((FIXTURES / name).read_bytes())


@pytest.fixture
def conn():
    with db_session(":memory:") as connection:
        yield connection


@pytest.fixture
def cause_list_html() -> str:
    return fixture("cause_list_print.html")


@pytest.fixture
def bench_list_html() -> str:
    return fixture("hcd_bench_list.html")


@pytest.fixture
def case_history_html() -> str:
    return fixture("case_history.html")


@pytest.fixture
def act_index_html() -> str:
    return fixture("bdlaws_chronological_index.html")


@pytest.fixture
def act_page_html() -> str:
    return fixture("bdlaws_act88.html")


@pytest.fixture
def section_page_html() -> str:
    return fixture("bdlaws_section_6447.html")
