"""Run the web UI + API against the demo data dir.

A thin launcher so you don't have to remember to set ``BARRISTER_DATA_DIR``
(and so automated runs have a reliable single entry point). It pins the
environment *before* the app is imported, then starts uvicorn on localhost.

Usage:

    python tools/run_demo.py                 # uses <repo>/.demodata
    python tools/run_demo.py --data-dir d:\\demo
    python tools/run_demo.py --port 8001
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / ".demodata"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    os.environ.setdefault("BARRISTER_DATA_DIR", str(Path(args.data_dir).resolve()))
    os.environ.setdefault("BARRISTER_CONTACT_EMAIL", "sabir@rahman.test")

    import uvicorn

    uvicorn.run("barrister.api:app", host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
