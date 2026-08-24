"""CLI wiring — the surfaces a cron job and a barrister actually invoke."""

from __future__ import annotations

import pytest

from barrister.cli import build_parser, main


class TestParser:
    def test_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_parses_a_sweep(self):
        args = build_parser().parse_args(["sweep", "--dry-run", "--divisions", "2"])
        assert args.dry_run is True
        assert args.divisions == [2]

    def test_parses_dates_in_several_formats(self):
        for value in ("2026-01-15", "15/01/2026", "15.01.2026"):
            args = build_parser().parse_args(["limitation", "--from", value, "--days", "90"])
            assert args.from_date.isoformat() == "2026-01-15"

    def test_rejects_an_unparseable_date(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["limitation", "--from", "last Tuesday", "--days", "1"])


class TestCommands:
    def test_templates_lists_them(self, capsys):
        assert main(["templates"]) == 0
        assert "writ_petition" in capsys.readouterr().out

    def test_limitation_prints_its_working(self, capsys):
        code = main(["limitation", "--from", "2026-01-15", "--days", "90", "--proceeding", "appeal"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Filing deadline:" in out
        assert "s. 12(2)" in out

    def test_limitation_without_a_period_is_an_error(self, capsys):
        assert main(["limitation", "--from", "2026-01-15"]) == 2

    def test_review_queue_points_at_the_official_text(self, capsys):
        assert main(["review-queue", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert "awaiting verification" in out
        assert "bdlaws.minlaw.gov.bd" in out

    def test_user_and_watch_round_trip(self, tmp_path, capsys):
        db = str(tmp_path / "cli.db")
        assert main(["--db", db, "adduser", "Sabir", "--telegram", "1"]) == 0
        assert "created user 1" in capsys.readouterr().out

        assert main(["--db", db, "watch", "1", "advocate", "Abu Hanif"]) == 0
        capsys.readouterr()

        assert main(["--db", db, "watches", "1"]) == 0
        assert "Abu Hanif" in capsys.readouterr().out

    def test_statute_search_on_an_empty_corpus_reports_cleanly(self, tmp_path, capsys):
        code = main(["--db", str(tmp_path / "s.db"), "statutes", "search", "limitation"])
        assert code == 1
        assert "corpus been synced" in capsys.readouterr().out

    def test_draft_writes_to_a_file(self, tmp_path, capsys):
        out = tmp_path / "petition.txt"
        code = main([
            "draft", "writ_petition",
            "--petitioner", "Md. Karim", "--respondent", "Bangladesh",
            "--facts", "Some facts.", "--prayer", "Issue a Rule Nisi",
            "--year", "2026", "--out", str(out),
        ])
        assert code == 0
        text = out.read_text()
        assert "IN THE SUPREME COURT OF BANGLADESH" in text
        assert "(i) Issue a Rule Nisi" in text
