"""Seed a demo database with realistic fake data, fully offline.

This is a local testing aid. It writes to its own data directory (default
``.demodata``) so it never touches a real ``~/.barrister`` install, and every
row it inserts goes through the real service functions the application uses —
the same code the CLI, web UI, API and bot call — so what you see in the demo
is exactly what the product does.

What it creates:

* one barrister (``Sabir Rahman``) plus watches for the advocate's own name, a
  client's name and a case number;
* three matters, each with a client, a linked court case (which also starts the
  cause-list watch), notes, time entries, a document and deadlines (one
  overdue, one due soon, one later);
* today-dated ``cause_list_entries`` that match those watches. This is the part
  that makes the "Today" homepage and ``barrister sweep --dry-run`` actually
  show rows — the dashboard only renders listings for the current date.

Usage:

    python tools/seed_demo.py --reset            # rebuild from scratch
    python tools/seed_demo.py --data-dir demo2   # use a different dir

Then run the app against the same data dir:

    $env:BARRISTER_DATA_DIR = "d:\\path\\to\\.demodata"
    uvicorn barrister.api:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

# The settings object is built at import time, so the data dir must be decided
# before any barrister module is imported.
ROOT = Path(__file__).resolve().parent.parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a demo database for Barrister Tools (offline)."
    )
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / ".demodata"),
        help="where to put the demo SQLite database (default: <repo>/.demodata)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the demo database before seeding, so it can be re-run cleanly",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    data_dir = Path(args.data_dir).resolve()
    os.environ["BARRISTER_DATA_DIR"] = str(data_dir)

    if args.reset and (data_dir / "barrister.db").exists():
        (data_dir / "barrister.db").unlink()
    # WAL sidecars from a previous run.
    for suffix in ("-wal", "-shm"):
        sidecar = data_dir / ("barrister.db" + suffix)
        if sidecar.exists():
            sidecar.unlink()
    # The HTTP cache is disposable.
    cache = data_dir / "http-cache"
    if cache.exists():
        shutil.rmtree(cache)

    # Imported after the data dir is pinned.
    from barrister.db import session
    from barrister.scrapers.cause_list import CauseListEntry, today_ddmmyyyy
    from barrister.services import matters as M
    from barrister.services import watchlist as W

    today = today_ddmmyyyy()
    two_days_ago = date.today() - timedelta(days=2)

    with session() as conn:
        # --- user + watches -----------------------------------------------
        user_id = W.add_user(
            conn,
            "Sabir Rahman",
            chamber="Rahman & Co.",
            telegram_chat_id="123456789",
            email="sabir@rahman.test",
        )
        W.add_watch(conn, user_id, "advocate", "Sabir Rahman")
        W.add_watch(conn, user_id, "party", "Karim Uddin")

        # --- matter 1: First Appeal 226/2013 ------------------------------
        client_1 = M.add_client(
            conn, user_id, "Md. Ruhul Amin",
            phone="+880 1711 000 001", email="ruhul@example.com",
            address="Mirpur, Dhaka",
            notes="Tenant in the underlying suit; first-time appellant.",
        )
        matter_1 = M.open_matter(
            conn, user_id, "Ruhul Amin v Aiyub Ali",
            client_id=client_1, court="High Court Division",
            description="Appeal against the trial court decree in the ejectment suit.",
            fee_agreed=250000.0,
        )
        M.link_case(conn, matter_1, case_type="First Appeal", case_number="226", case_year="2013")
        M.add_note(conn, matter_1, "Conference; instructions to press the appeal.", kind="attendance", noted_on=two_days_ago)
        M.add_note(conn, matter_1, "Client may settle; holding settlement meeting.", kind="note")
        M.log_time(conn, matter_1, 150, "Settling grounds of appeal", rate=6000, worked_on=two_days_ago)
        M.log_time(conn, matter_1, 90, "Drafting the petition", rate=6000)
        M.add_document(conn, matter_1, "Grounds of appeal", kind="draft", path="grounds.docx", filed_on=two_days_ago)
        M.add_deadline(conn, matter_1, "File paper book", date.today() - timedelta(days=2),
                       basis="Court direction on 20/08/2026", verified=True)
        M.add_deadline(conn, matter_1, "Reply affidavit", date.today() + timedelta(days=3),
                       basis="Limitation Act 1908, s. 12(2)", verified=True)
        M.add_deadline(conn, matter_1, "Vakalatnama renewal", date.today() + timedelta(days=30),
                       basis="Roster", verified=False)

        # --- matter 2: Civil Revision 2347/2007 ---------------------------
        client_2 = M.add_client(
            conn, user_id, "Fatema Begum",
            phone="+880 1711 000 002", email="fatema@example.com",
            address="Dhanmondi, Dhaka",
        )
        matter_2 = M.open_matter(
            conn, user_id, "Fatema Begum v Abdul Karim",
            client_id=client_2, court="High Court Division",
            description="Civil revision against the order in Title Suit 44/2005.",
            fee_agreed=180000.0,
        )
        M.link_case(conn, matter_2, case_type="Civil Revision", case_number="2347", case_year="2007")
        M.add_note(conn, matter_2, "Advised revision is arguable.", kind="advice")
        M.log_time(conn, matter_2, 60, "Reviewing trial record", rate=6000)
        M.add_document(conn, matter_2, "Memorandum of revision", kind="draft")
        M.add_deadline(conn, matter_2, "File rejoinder", date.today() + timedelta(days=6),
                       basis="Rule returnable date", verified=True)

        # --- matter 3: Writ Petition 1234/2025 (party watch) --------------
        client_3 = M.add_client(
            conn, user_id, "Karim Uddin",
            phone="+880 1711 000 003", email="karim@example.com",
            address="Gulshan, Dhaka",
        )
        matter_3 = M.open_matter(
            conn, user_id, "Karim Uddin v Bangladesh",
            client_id=client_3, court="High Court Division",
            description="Writ challenging an acquisition notice.",
            fee_agreed=320000.0,
        )
        M.link_case(conn, matter_3, case_type="Writ Petition", case_number="1234", case_year="2025")
        M.add_note(conn, matter_3, "Hearing completed; reserved for judgment.", kind="hearing")
        M.log_time(conn, matter_3, 45, "Perusing acquisition file", rate=7000)

        # --- today's cause-list entries ------------------------------------
        # These match the watches above so the "Today" page and the sweep show
        # rows. Two courts to exercise the by-court grouping on the homepage.
        entries = [
            CauseListEntry(
                list_date=today, division="High Court Division",
                court_id="42", bench_id="10294",
                court_name="Annex Building Court No. 18",
                judges="Justice Sheikh Abdul Awal and Justice A. K. M. Rabiul Hassan",
                section="Fixing a date of hearing", serial=10,
                case_type="First Appeal", case_number="226", case_year="2013",
                district="Dhaka",
                parties="Md. Ruhul Amin and ors. vs Alhaj Md. Aiyub Ali Sikder and ors.",
                petitioner="Md. Ruhul Amin and ors.",
                respondent="Alhaj Md. Aiyub Ali Sikder and ors.",
                advocates=["Mr. Sabir Rahman", "with Mr. A. K. M. Rafique"],
                raw="First Appeal 226/2013 Dhaka | Md. Ruhul Amin and ors. vs Alhaj Md. Aiyub Ali Sikder and ors.",
            ),
            CauseListEntry(
                list_date=today, division="High Court Division",
                court_id="42", bench_id="10294",
                court_name="Annex Building Court No. 18",
                judges="Justice Sheikh Abdul Awal and Justice A. K. M. Rabiul Hassan",
                section="For Judgment", serial=12,
                case_type="Civil Revision", case_number="2347", case_year="2007",
                district="Dhaka",
                parties="Fatema Begum vs Abdul Karim",
                petitioner="Fatema Begum", respondent="Abdul Karim",
                advocates=["Sabir Rahman", "with J. Uddin"],
                raw="Civil Revision 2347/2007 Dhaka | Fatema Begum vs Abdul Karim",
            ),
            CauseListEntry(
                list_date=today, division="High Court Division",
                court_id="43", bench_id="10301",
                court_name="Court No. 5",
                judges="Justice Md. Naima Haider",
                section="For Hearing", serial=3,
                case_type="Writ Petition", case_number="1234", case_year="2025",
                district="Dhaka",
                parties="Karim Uddin and ors. vs Bangladesh, represented by the Secretary, Ministry of Land",
                petitioner="Karim Uddin and ors.", respondent="Bangladesh",
                advocates=["Mr. T. Ahmed"],
                raw="Writ Petition 1234/2025 Dhaka | Karim Uddin and ors. vs Bangladesh",
            ),
        ]
        W.store_entries(conn, entries)

    print(f"Seeded demo data in {data_dir}")
    print(f"  user: Sabir Rahman (id {user_id})")
    print(f"  matters: MAT-{date.today().year}-001 / -002 / -003")
    print(f"  watches: advocate 'Sabir Rahman', party 'Karim Uddin', cases linked")
    print(f"  cause-list entries for {today}: {len(entries)}")
    print()
    print("Run the app against this data dir:")
    print(f'  $env:BARRISTER_DATA_DIR = "{data_dir}"')
    print("  uvicorn barrister.api:app --host 127.0.0.1 --port 8000")
    print("Then open http://127.0.0.1:8000/ (Today), /matters, /diary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


