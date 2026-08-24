"""End-to-end sweep: parse a real list, match watches, queue and deliver alerts."""

from __future__ import annotations

from barrister.scrapers.cause_list import parse_cause_list
from barrister.services.notify import NullNotifier
from barrister.services.watchlist import (
    add_user, add_watch, format_alert, list_watches, remove_watch, run_sweep,
)
from barrister.services.matching import match_all


class TestWatches:
    def test_add_and_list(self, conn):
        user = add_user(conn, "Sabir", chamber="Chambers")
        add_watch(conn, user, "advocate", "Abu Hanif")
        add_watch(conn, user, "case", "First Appeal 226/2013")
        assert len(list_watches(conn, user)) == 2

    def test_adding_the_same_watch_twice_is_idempotent(self, conn):
        user = add_user(conn, "Sabir")
        first = add_watch(conn, user, "advocate", "Abu Hanif")
        second = add_watch(conn, user, "advocate", "Abu Hanif")
        assert first == second
        assert len(list_watches(conn, user)) == 1

    def test_removed_watch_stops_matching(self, conn):
        user = add_user(conn, "Sabir")
        watch = add_watch(conn, user, "advocate", "Abu Hanif")
        remove_watch(conn, watch)
        assert list_watches(conn, user) == []

    def test_rejects_unknown_kind(self, conn):
        user = add_user(conn, "Sabir")
        try:
            add_watch(conn, user, "judge", "Someone")
        except ValueError:
            return
        raise AssertionError("expected ValueError for an unknown watch kind")


class TestSweep:
    def _seed(self, conn):
        user = add_user(conn, "Sabir", telegram_chat_id="12345")
        add_watch(conn, user, "advocate", "Abu Hanif")
        add_watch(conn, user, "case", "First Appeal 226/2013")
        return user

    def test_sweep_delivers_one_grouped_alert(self, conn, cause_list_html):
        self._seed(conn)
        entries = parse_cause_list(cause_list_html)
        notifier = NullNotifier()

        result = run_sweep(conn, entries, notifier)

        assert result.entries_seen == 26
        assert result.users_notified == 1
        # Two matched matters, but a barrister gets one message, not two.
        assert result.alerts_created == 1
        assert len(notifier.sent) == 1

    def test_rerunning_the_sweep_does_not_resend(self, conn, cause_list_html):
        self._seed(conn)
        entries = parse_cause_list(cause_list_html)
        notifier = NullNotifier()

        run_sweep(conn, entries, notifier)
        second = run_sweep(conn, entries, notifier)

        assert second.alerts_created == 0
        assert len(notifier.sent) == 1

    def test_user_with_no_matches_is_not_alerted(self, conn, cause_list_html):
        user = add_user(conn, "Someone Else", telegram_chat_id="999")
        add_watch(conn, user, "advocate", "Nobody At All")
        notifier = NullNotifier()

        result = run_sweep(conn, parse_cause_list(cause_list_html), notifier)

        assert result.users_notified == 0
        assert notifier.sent == []

    def test_alert_body_carries_what_a_barrister_needs(self, conn, cause_list_html):
        entries = parse_cause_list(cause_list_html)
        matches = match_all(entries, [("case", "First Appeal 226/2013")])
        subject, body = format_alert(matches)

        assert "24/08/2026" in subject
        assert "First Appeal 226/2013" in body
        assert "Annex Building Court No. 18" in body     # which court
        assert "Serial 10" in body                        # where in the list
        assert "Fixing a date of hearing" in body         # what happens
        assert "supremecourt.gov.bd" in body              # verifiable

    def test_undelivered_alerts_are_retried(self, conn, cause_list_html):
        self._seed(conn)

        class FailingNotifier:
            def __init__(self):
                self.attempts = 0

            def send(self, recipient, subject, body):
                self.attempts += 1
                return False

        failing = FailingNotifier()
        run_sweep(conn, parse_cause_list(cause_list_html), failing)
        assert failing.attempts == 1

        # The alert stays queued, so a later run can still deliver it.
        working = NullNotifier()
        run_sweep(conn, parse_cause_list(cause_list_html), working)
        assert len(working.sent) == 1
