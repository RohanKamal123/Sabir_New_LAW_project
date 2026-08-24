"""Telegram bot routing. The transport is mocked; the commands are real."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from barrister.config import Settings
from barrister.scrapers.cause_list import parse_cause_list
from barrister.services import matters as M
from barrister.services.bot import Reply, TelegramTransport, handle, run
from barrister.services.notify import NullNotifier
from barrister.services.watchlist import add_user, add_watch, list_watches, run_sweep


@pytest.fixture
def user(conn):
    return add_user(conn, "Sabir Rahman", telegram_chat_id="123")


@pytest.fixture
def matter(conn, user):
    return M.open_matter(conn, user, "Karim v Bangladesh")


class TestRouting:
    def test_non_command_gets_a_nudge(self, conn):
        assert "/help" in handle(conn, "123", "good morning").text

    def test_help_needs_no_registration(self, conn):
        assert "Barrister Tools" in handle(conn, "999", "/help").text

    def test_unknown_command_is_reported(self, conn, user):
        assert "I don't know" in handle(conn, "123", "/nonsense").text

    def test_unregistered_chat_is_told_how_to_register(self, conn):
        reply = handle(conn, "999", "/today")
        assert "adduser" in reply.text and "999" in reply.text

    def test_group_chat_suffix_is_stripped(self, conn, user):
        add_watch(conn, user, "case", "First Appeal 226/2013")
        assert "First Appeal 226/2013" in handle(conn, "123", "/watches@ChamberBot").text

    def test_a_failing_command_does_not_raise(self, conn, user, monkeypatch):
        import barrister.services.bot as bot

        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(bot.COMMANDS, "matters", boom)
        assert "went wrong" in handle(conn, "123", "/matters").text


class TestWatches:
    def test_add_a_watch(self, conn, user):
        reply = handle(conn, "123", "/watch advocate Sabir Rahman")
        assert "Watching advocate" in reply.text
        assert len(list_watches(conn, user)) == 1

    def test_watch_needs_a_valid_kind(self, conn, user):
        assert "Use" in handle(conn, "123", "/watch judge Someone").text

    def test_watch_needs_a_value(self, conn, user):
        assert "Use" in handle(conn, "123", "/watch advocate").text

    def test_list_watches(self, conn, user):
        add_watch(conn, user, "case", "First Appeal 226/2013")
        assert "First Appeal 226/2013" in handle(conn, "123", "/watches").text

    def test_empty_watch_list_suggests_a_first_watch(self, conn, user):
        assert "not watching anything" in handle(conn, "123", "/watches").text

    def test_unwatch_removes_it(self, conn, user):
        watch = add_watch(conn, user, "case", "First Appeal 226/2013")
        assert "Stopped watching" in handle(conn, "123", f"/unwatch {watch}").text
        assert list_watches(conn, user) == []

    def test_cannot_unwatch_another_barristers_watch(self, conn, user):
        other = add_user(conn, "Other", telegram_chat_id="456")
        watch = add_watch(conn, other, "case", "Writ Petition 1/2026")
        assert "No watch" in handle(conn, "123", f"/unwatch {watch}").text
        assert len(list_watches(conn, other)) == 1

    def test_unwatch_needs_an_id(self, conn, user):
        assert "see /watches" in handle(conn, "123", "/unwatch everything").text


class TestToday:
    def test_reads_from_the_last_sweep(self, conn, user, cause_list_html):
        add_watch(conn, user, "case", "First Appeal 226/2013")
        run_sweep(conn, parse_cause_list(cause_list_html), NullNotifier())

        reply = handle(conn, "123", "/today 24/08/2026")

        assert "First Appeal 226/2013" in reply.text
        assert "Annex Building Court No. 18" in reply.text
        assert "serial 10" in reply.text

    def test_names_the_file_when_one_exists(self, conn, user, cause_list_html):
        matter = M.open_matter(conn, user, "Ruhul Amin v Aiyub Ali")
        M.link_case(conn, matter, case_type="First Appeal", case_number="226", case_year="2013")
        run_sweep(conn, parse_cause_list(cause_list_html), NullNotifier())

        reply = handle(conn, "123", "/today 24/08/2026")

        assert M.get_matter(conn, matter)["reference"] in reply.text

    def test_nothing_listed_says_so(self, conn, user):
        assert "Nothing of yours listed" in handle(conn, "123", "/today 01/01/2020").text

    def test_a_bad_date_is_explained(self, conn, user):
        assert "couldn't read" in handle(conn, "123", "/today last Tuesday").text


class TestDiary:
    def test_shows_days_remaining(self, conn, user, matter):
        M.add_deadline(conn, matter, "File CPLA", date.today() + timedelta(days=2))
        reply = handle(conn, "123", "/diary")
        assert "File CPLA" in reply.text
        assert "2d left" in reply.text

    def test_overdue_is_flagged(self, conn, user, matter):
        M.add_deadline(conn, matter, "Missed it", date.today() - timedelta(days=4))
        assert "OVERDUE by 4d" in handle(conn, "123", "/diary").text

    def test_unverified_basis_is_flagged(self, conn, user, matter):
        M.add_deadline(conn, matter, "From an article", date.today(), verified=False)
        assert "not verified" in handle(conn, "123", "/diary").text

    def test_verified_basis_is_not_flagged(self, conn, user, matter):
        M.add_deadline(conn, matter, "Checked", date.today(), verified=True)
        assert "not verified" not in handle(conn, "123", "/diary").text

    def test_horizon_is_configurable(self, conn, user, matter):
        M.add_deadline(conn, matter, "Far", date.today() + timedelta(days=50))
        assert "No deadlines" in handle(conn, "123", "/diary 30").text
        assert "Far" in handle(conn, "123", "/diary 90").text


class TestMatters:
    def test_lists_files(self, conn, user, matter):
        assert "Karim v Bangladesh" in handle(conn, "123", "/matters").text

    def test_no_files_says_so(self, conn, user):
        assert "No files yet" in handle(conn, "123", "/matters").text

    def test_opens_a_file_by_reference(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        M.link_case(conn, matter, case_type="Writ Petition", case_number="1", case_year="2026")
        reply = handle(conn, "123", f"/matter {reference}")
        assert "Karim v Bangladesh" in reply.text
        assert "Writ Petition 1/2026" in reply.text

    def test_unknown_reference_is_reported(self, conn, user):
        assert "No file with reference" in handle(conn, "123", "/matter MAT-1999-001").text

    def test_matter_needs_a_reference(self, conn, user):
        assert "Which file" in handle(conn, "123", "/matter").text

    def test_note_is_recorded(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        assert "Noted on" in handle(conn, "123", f"/note {reference} Conference with client").text
        assert M.list_notes(conn, matter)[0]["body"] == "Conference with client"

    def test_note_needs_text(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        assert "Use" in handle(conn, "123", f"/note {reference}").text

    def test_time_is_logged_and_totalled(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        handle(conn, "123", f"/time {reference} 90 Drafting the petition")
        reply = handle(conn, "123", f"/time {reference} 30 Conference")
        assert "2.0h total" in reply.text
        assert M.time_summary(conn, matter).minutes == 120

    def test_time_needs_minutes(self, conn, user, matter):
        reference = M.get_matter(conn, matter)["reference"]
        assert "Use" in handle(conn, "123", f"/time {reference} ages Drafting").text


class TestLimitation:
    def test_computes_and_shows_working(self, conn, user):
        reply = handle(conn, "123", "/limitation 2026-01-15 90 appeal")
        assert "Deadline: 2026-04-15" in reply.text
        assert "s. 12(2)" in reply.text
        assert "not advice" in reply.text

    def test_needs_two_arguments(self, conn, user):
        assert "Use" in handle(conn, "123", "/limitation 2026-01-15").text

    def test_rejects_a_non_numeric_period(self, conn, user):
        assert "number of days" in handle(conn, "123", "/limitation 2026-01-15 ninety").text


class TestStatutes:
    def test_empty_corpus_is_explained(self, conn, user):
        assert "not be synced" in handle(conn, "123", "/statute limitation").text

    def test_needs_a_query(self, conn, user):
        assert "looking for" in handle(conn, "123", "/statute").text


class TestFormatting:
    def test_markdown_metacharacters_in_data_are_escaped(self, conn, user, matter):
        M.add_note(conn, matter, "Party is *Acme* [Ltd]")
        reference = M.get_matter(conn, matter)["reference"]
        text = handle(conn, "123", f"/matter {reference}").text
        assert r"\*Acme\*" in text
        assert r"\[Ltd\]" in text

    def test_long_replies_are_truncated_to_telegram_limit(self):
        reply = Reply("x" * 9000)
        assert len(reply.truncated()) <= 4096
        assert reply.truncated().endswith("(truncated)")

    def test_short_replies_pass_through(self):
        assert Reply("short").truncated() == "short"


class TestTransport:
    def _transport(self, handler):
        settings = Settings(telegram_bot_token="test-token")
        return TelegramTransport(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_requires_a_token(self):
        from barrister.services.bot import BotError

        with pytest.raises(BotError, match="TELEGRAM_BOT_TOKEN"):
            TelegramTransport(Settings(telegram_bot_token=""))

    def test_sends_with_markdown(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        assert self._transport(handler).send("123", Reply("hello")) is True
        assert "sendMessage" in seen["url"]
        assert "parse_mode=Markdown" in seen["body"]

    def test_falls_back_to_plain_text_when_markdown_is_rejected(self):
        attempts = []

        def handler(request):
            attempts.append(request.content.decode())
            if len(attempts) == 1:
                return httpx.Response(400, text="can't parse entities")
            return httpx.Response(200, json={"ok": True})

        assert self._transport(handler).send("123", Reply("bad *markdown")) is True
        assert len(attempts) == 2
        assert "parse_mode" not in attempts[1]

    def test_run_loop_answers_a_message(self, tmp_path):
        sent = []

        class FakeTransport:
            def get_updates(self, offset=None, timeout=50):
                if offset is None:
                    return [{
                        "update_id": 1,
                        "message": {"chat": {"id": 123}, "text": "/help"},
                    }]
                return []

            def send(self, chat_id, reply):
                sent.append((chat_id, reply.text))
                return True

        handled = run(
            Settings(telegram_bot_token="t"),
            transport=FakeTransport(),
            db_path=str(tmp_path / "bot.db"),
            max_iterations=2,
        )

        assert handled == 1
        assert sent[0][0] == "123"
        assert "Barrister Tools" in sent[0][1]

    def test_run_loop_skips_updates_without_a_message(self, tmp_path):
        class FakeTransport:
            def get_updates(self, offset=None, timeout=50):
                return [{"update_id": 1, "poll": {"id": "x"}}] if offset is None else []

            def send(self, chat_id, reply):
                raise AssertionError("should not send")

        assert run(
            Settings(telegram_bot_token="t"), transport=FakeTransport(),
            db_path=str(tmp_path / "bot.db"), max_iterations=2,
        ) == 0
