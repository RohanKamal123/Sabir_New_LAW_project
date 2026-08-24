"""Command line interface.

The cron-facing commands are ``sweep`` (the nightly cause-list run) and
``track`` (periodic case-status re-checks). Everything else is interactive.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .config import settings
from .db import session
from .http import PoliteClient
from .scrapers import bdlaws, case_status, cause_list
from .services import bot, drafting, limitation, matters, statutes, tracker, watchlist
from .services.notify import ConsoleNotifier, default_notifier

log = logging.getLogger(__name__)


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"unrecognised date {value!r}; use YYYY-MM-DD, DD/MM/YYYY or DD.MM.YYYY"
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_adduser(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        user_id = watchlist.add_user(
            conn, args.name, chamber=args.chamber, telegram_chat_id=args.telegram, email=args.email
        )
    print(f"created user {user_id}: {args.name}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        watch_id = watchlist.add_watch(conn, args.user, args.kind, args.value)
    print(f"watch {watch_id}: user {args.user} watching {args.kind} {args.value!r}")
    return 0


def cmd_watches(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        rows = watchlist.list_watches(conn, args.user)
    if not rows:
        print(f"user {args.user} has no active watches")
        return 0
    for row in rows:
        print(f"  [{row['id']}] {row['kind']:9} {row['value']}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Fetch today's cause lists, match every watch, deliver alerts."""
    divisions = tuple(args.divisions) if args.divisions else (1, 2)
    entries = []
    with PoliteClient() as client:
        for entry in cause_list.fetch_all(
            client, divisions=divisions, force_refresh=args.refresh
        ):
            entries.append(entry)
    print(f"parsed {len(entries)} listing(s) across divisions {divisions}")

    notifier = ConsoleNotifier() if args.dry_run else default_notifier()
    with session(args.db) as conn:
        result = watchlist.run_sweep(conn, entries, notifier, deliver=not args.no_deliver)
    print(
        f"users notified: {result.users_notified}; alerts created: {result.alerts_created}; "
        f"delivered: {result.alerts_delivered}"
    )
    return 0


def cmd_causelist(args: argparse.Namespace) -> int:
    """Print one bench's list, or every bench for a division."""
    with PoliteClient() as client:
        benches = cause_list.fetch_benches(client, args.division, force_refresh=args.refresh)
        if args.court:
            benches = [b for b in benches if args.court.lower() in b.court_name.lower()]
        if not benches:
            print("no matching bench found", file=sys.stderr)
            return 1
        for bench in benches[: args.limit]:
            print(f"\n=== {bench.court_name} ({bench.list_date}) ===")
            print(f"    {bench.judges}")
            for entry in cause_list.fetch_cause_list(client, bench, force_refresh=args.refresh):
                marker = f"#{entry.serial}" if entry.serial is not None else "  -"
                print(f"  {marker:>5} [{entry.section or '-'}] {entry.case_ref}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with PoliteClient() as client:
        status = case_status.fetch_case_status(
            client,
            case_type=args.case_type,
            case_number=args.number,
            year=args.year,
            division_id=args.division,
            force_refresh=args.refresh,
        )
    print(f"{status.case_ref}")
    print(f"  {status.petitioner or '?'} vs {status.respondent or '?'}")
    if status.petitioner_lawyer:
        print(f"  petitioner's advocate: {status.petitioner_lawyer}")
    print(f"  hearings recorded: {len(status.hearings)}")
    for hearing in status.hearings[: args.limit]:
        print(f"    {hearing.date or '?'}  {hearing.court or '?'} — {hearing.result or 'no result recorded'}")

    if args.track_for is not None:
        with session(args.db) as conn:
            changes = tracker.track_case(conn, args.track_for, status, default_notifier())
        print(f"  changes since last check: {len(changes)}")
    return 0


def cmd_statutes_sync(args: argparse.Namespace) -> int:
    """Build or refresh the local Bangladesh Code corpus."""
    with PoliteClient() as client, session(args.db) as conn:
        if args.act:
            acts = [bdlaws.fetch_act(client, act, force_refresh=args.refresh) for act in args.act]
        else:
            acts = list(bdlaws.iter_acts(client, limit=args.limit, force_refresh=args.refresh))
        for act in acts:
            count = statutes.store_act(conn, act)
            print(f"  stored {act.ref.title} ({count} sections)")
        print("corpus:", statutes.corpus_stats(conn))
    return 0


def cmd_statutes_search(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        hits = statutes.search(conn, args.query, limit=args.limit)
    if not hits:
        print("no match. Has the corpus been synced? (barrister statutes sync)")
        return 1
    for hit in hits:
        print(f"\n{hit.citation}")
        if hit.part:
            print(f"  {hit.part}")
        print(f"  {hit.heading}")
        print(f"  {hit.snippet[:400]}")
        print(f"  {hit.url}")
    return 0


def cmd_limitation(args: argparse.Namespace) -> int:
    calendar = limitation.CourtCalendar.with_holidays(
        [_parse_date(h) for h in args.holiday or []]
    )
    if args.article:
        result = limitation.deadline_for_article(
            args.article,
            start_date=args.from_date,
            proceeding=args.proceeding,
            calendar=calendar,
            allow_unverified=args.allow_unverified,
            copy_applied_on=args.copy_applied,
            copy_ready_on=args.copy_ready,
        )
    else:
        if args.days is None:
            print("give either --article or --days", file=sys.stderr)
            return 2
        result = limitation.compute(
            start_date=args.from_date,
            period=limitation.Period(args.days, args.unit),
            proceeding=args.proceeding,
            calendar=calendar,
            copy_applied_on=args.copy_applied,
            copy_ready_on=args.copy_ready,
        )
    print(result.explain())
    print(f"\nDays remaining as of today: {result.days_remaining()}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    facts = Path(args.facts).read_text(encoding="utf-8") if args.facts_file else args.facts
    request = drafting.DraftRequest(
        template=args.template,
        year=args.year or str(date.today().year),
        petitioners=args.petitioner,
        respondents=args.respondent,
        subject_matter=args.subject or "",
        facts=facts or "",
        grounds=args.ground or [],
        prayers=args.prayer or [],
        supplied_authorities=args.authority or [],
        extra=json.loads(args.extra) if args.extra else {},
    )
    result = drafting.draft(request)
    if args.out:
        Path(args.out).write_text(result.text, encoding="utf-8")
        print(f"written to {args.out} (provider: {result.provider}, model: {result.model or '-'})")
    else:
        print(result.text)
    for warning in result.warnings:
        print(f"\n! {warning}", file=sys.stderr)
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    for name in drafting.available_templates():
        print(f"  {name}")
    return 0


def cmd_matter_open(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        client_id = None
        if args.client:
            client_id = matters.add_client(conn, args.user, args.client)
        matter_id = matters.open_matter(
            conn, args.user, args.title, reference=args.reference,
            client_id=client_id, court=args.court, fee_agreed=args.fee,
        )
        reference = matters.get_matter(conn, matter_id)["reference"]
        for case in args.case or []:
            parts = case.rsplit(maxsplit=2)
            if len(parts) != 3:
                print(f"skipping {case!r}: expected '<type> <number> <year>'", file=sys.stderr)
                continue
            matters.link_case(
                conn, matter_id, case_type=parts[0], case_number=parts[1], case_year=parts[2]
            )
            print(f"  linked and watching {case}")
    print(f"opened {reference} ({matter_id}): {args.title}")
    return 0


def cmd_matter_list(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        rows = matters.list_matters(conn, args.user, status=args.status)
        summary = matters.practice_summary(conn, args.user)
    if not rows:
        print("no matters yet")
        return 0
    for row in rows:
        client = f" — {row['client_name']}" if row["client_name"] else ""
        print(f"  {row['reference']:<14} [{row['status']:<8}] {row['title']}{client}")
    print(f"\n{summary['total_matters']} matter(s); {summary['unbilled_hours']}h unbilled; "
          f"{summary['deadlines_30d']} deadline(s) in 30 days")
    return 0


def cmd_matter_show(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        matter = matters.find_matter_by_reference(conn, args.user, args.reference)
        if matter is None:
            print(f"no matter {args.reference!r}", file=sys.stderr)
            return 1
        overview = matters.matter_overview(conn, int(matter["id"]))

    info = overview["matter"]
    print(f"{info['reference']}  {info['title']}")
    print(f"  status: {info['status']}   client: {info['client_name'] or '-'}")
    print(f"  time:   {overview['time']['hours']}h "
          f"({overview['time']['unbilled_minutes']}m unbilled)")
    if overview["cases"]:
        print("  cases:")
        for case in overview["cases"]:
            print(f"    {case['case_type']} {case['case_number']}/{case['case_year']}")
    if overview["deadlines"]:
        print("  deadlines:")
        for deadline in overview["deadlines"]:
            flag = "" if deadline["verified"] else "  (basis unverified)"
            done = " [done]" if deadline["completed"] else ""
            print(f"    {deadline['due_on']}  {deadline['label']}{flag}{done}")
    if overview["notes"]:
        print("  notes:")
        for note in overview["notes"][:10]:
            print(f"    {note['noted_on']}  [{note['kind']}] {note['body'][:70]}")
    return 0


def cmd_matter_note(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        matter = matters.find_matter_by_reference(conn, args.user, args.reference)
        if matter is None:
            print(f"no matter {args.reference!r}", file=sys.stderr)
            return 1
        matters.add_note(conn, int(matter["id"]), args.body, kind=args.kind)
    print(f"noted on {args.reference}")
    return 0


def cmd_matter_time(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        matter = matters.find_matter_by_reference(conn, args.user, args.reference)
        if matter is None:
            print(f"no matter {args.reference!r}", file=sys.stderr)
            return 1
        matters.log_time(
            conn, int(matter["id"]), args.minutes, args.description, rate=args.rate
        )
        summary = matters.time_summary(conn, int(matter["id"]))
    print(f"logged {args.minutes}m on {args.reference}; {summary.hours}h total, "
          f"{summary.billable} billable")
    return 0


def cmd_diary(args: argparse.Namespace) -> int:
    with session(args.db) as conn:
        rows = matters.upcoming_deadlines(conn, args.user, within_days=args.days)
    if not rows:
        print(f"no deadlines in the next {args.days} days")
        return 0
    today = date.today()
    for row in rows:
        due = date.fromisoformat(row["due_on"])
        remaining = (due - today).days
        marker = f"OVERDUE {abs(remaining)}d" if remaining < 0 else f"{remaining}d"
        flag = "" if row["verified"] else "  (basis unverified)"
        print(f"  {row['due_on']}  {marker:>10}  {row['reference']}  {row['label']}{flag}")
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    """Run the Telegram bot until interrupted."""
    try:
        handled = bot.run(db_path=args.db, max_iterations=args.iterations)
    except bot.BotError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"handled {handled} message(s)")
    return 0


def cmd_review_queue(args: argparse.Namespace) -> int:
    """List Schedule articles still awaiting a lawyer's verification."""
    pending = limitation.unverified_articles()
    print(f"{len(pending)} article(s) awaiting verification:\n")
    for rule in pending[: args.limit]:
        print(f"  Art. {rule.article:<6} {rule.period_text or '(no period parsed)':<20} {rule.description[:70]}")
    print(f"\nEdit barrister/data/limitation_schedule.json and set \"verified\": true once checked.")
    print(f"Official text: {limitation.load_source_url()}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="barrister", description="Tools for Supreme Court of Bangladesh practice"
    )
    parser.add_argument("--db", default=None, help="path to the SQLite database")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("adduser", help="register a barrister")
    p.add_argument("name")
    p.add_argument("--chamber")
    p.add_argument("--telegram", help="Telegram chat id for alerts")
    p.add_argument("--email")
    p.set_defaults(func=cmd_adduser)

    p = subparsers.add_parser("watch", help="watch a name or case number")
    p.add_argument("user", type=int)
    p.add_argument("kind", choices=["advocate", "party", "case"])
    p.add_argument("value")
    p.set_defaults(func=cmd_watch)

    p = subparsers.add_parser("watches", help="list a user's watches")
    p.add_argument("user", type=int)
    p.set_defaults(func=cmd_watches)

    p = subparsers.add_parser("sweep", help="fetch today's cause lists and alert (cron)")
    p.add_argument("--divisions", type=int, nargs="*", choices=[1, 2])
    p.add_argument("--refresh", action="store_true", help="bypass the HTTP cache")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    p.add_argument("--no-deliver", action="store_true", help="queue alerts without delivering")
    p.set_defaults(func=cmd_sweep)

    p = subparsers.add_parser("causelist", help="print cause lists")
    p.add_argument("--division", type=int, default=2, choices=[1, 2])
    p.add_argument("--court", help="filter benches by name")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_causelist)

    p = subparsers.add_parser("status", help="look up a case's status and history")
    p.add_argument("case_type", help='e.g. "Writ Petition", "First Appeal"')
    p.add_argument("number")
    p.add_argument("year")
    p.add_argument("--division", type=int, choices=[1, 2])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--track-for", type=int, metavar="USER_ID", help="snapshot and alert on changes")
    p.set_defaults(func=cmd_status)

    statute_parser = subparsers.add_parser("statutes", help="Bangladesh Code lookup")
    statute_sub = statute_parser.add_subparsers(dest="statutes_command", required=True)

    p = statute_sub.add_parser("sync", help="download acts into the local corpus")
    p.add_argument("--act", action="append", help="act id, e.g. 88 for the Limitation Act")
    p.add_argument("--limit", type=int, help="stop after N acts when syncing everything")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_statutes_sync)

    p = statute_sub.add_parser("search", help="search the local corpus")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_statutes_search)

    p = subparsers.add_parser("limitation", help="compute a filing deadline")
    p.add_argument("--from", dest="from_date", type=_parse_date, required=True,
                   help="date of the decree, order or judgment")
    p.add_argument("--article", help="First Schedule article number")
    p.add_argument("--days", type=int, help="period length, when not using an article")
    p.add_argument("--unit", default="days", choices=["days", "months", "years"])
    p.add_argument("--proceeding", default="suit",
                   choices=["suit", "appeal", "leave_to_appeal", "review", "application"])
    p.add_argument("--copy-applied", type=_parse_date, help="date the certified copy was applied for")
    p.add_argument("--copy-ready", type=_parse_date, help="date the certified copy was ready")
    p.add_argument("--holiday", action="append", help="a date the Court is closed (repeatable)")
    p.add_argument("--allow-unverified", action="store_true",
                   help="compute from an article no lawyer has verified")
    p.set_defaults(func=cmd_limitation)

    p = subparsers.add_parser("draft", help="draft a document from a template")
    p.add_argument("template")
    p.add_argument("--petitioner", action="append", required=True)
    p.add_argument("--respondent", action="append", required=True)
    p.add_argument("--facts", help="the facts, or a path with --facts-file")
    p.add_argument("--facts-file", action="store_true", help="treat --facts as a file path")
    p.add_argument("--subject")
    p.add_argument("--year")
    p.add_argument("--ground", action="append")
    p.add_argument("--prayer", action="append")
    p.add_argument("--authority", action="append",
                   help="a citation you have checked; the model may cite only these")
    p.add_argument("--extra", help="JSON of extra template variables")
    p.add_argument("--out", help="write to this file instead of stdout")
    p.set_defaults(func=cmd_draft)

    p = subparsers.add_parser("templates", help="list drafting templates")
    p.set_defaults(func=cmd_templates)

    matter_parser = subparsers.add_parser("matter", help="case-file management")
    matter_sub = matter_parser.add_subparsers(dest="matter_command", required=True)

    p = matter_sub.add_parser("open", help="open a new file")
    p.add_argument("user", type=int)
    p.add_argument("title")
    p.add_argument("--reference", help="defaults to MAT-<year>-<seq>")
    p.add_argument("--client")
    p.add_argument("--court")
    p.add_argument("--fee", type=float)
    p.add_argument("--case", action="append",
                   help='"<type> <number> <year>"; also starts watching it')
    p.set_defaults(func=cmd_matter_open)

    p = matter_sub.add_parser("list", help="list files")
    p.add_argument("user", type=int)
    p.add_argument("--status", choices=list(matters.MATTER_STATUSES))
    p.set_defaults(func=cmd_matter_list)

    p = matter_sub.add_parser("show", help="show one file")
    p.add_argument("user", type=int)
    p.add_argument("reference")
    p.set_defaults(func=cmd_matter_show)

    p = matter_sub.add_parser("note", help="add a note to a file")
    p.add_argument("user", type=int)
    p.add_argument("reference")
    p.add_argument("body")
    p.add_argument("--kind", default="note", choices=list(matters.NOTE_KINDS))
    p.set_defaults(func=cmd_matter_note)

    p = matter_sub.add_parser("time", help="log time against a file")
    p.add_argument("user", type=int)
    p.add_argument("reference")
    p.add_argument("minutes", type=int)
    p.add_argument("description")
    p.add_argument("--rate", type=float)
    p.set_defaults(func=cmd_matter_time)

    p = subparsers.add_parser("diary", help="deadlines falling due")
    p.add_argument("user", type=int)
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_diary)

    p = subparsers.add_parser("bot", help="run the Telegram bot")
    p.add_argument("--iterations", type=int, help="stop after N polls (for testing)")
    p.set_defaults(func=cmd_bot)

    p = subparsers.add_parser("review-queue", help="limitation articles needing verification")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_review_queue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.db is None:
        args.db = settings.db_path
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
