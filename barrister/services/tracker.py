"""Case status tracking: snapshot, diff, and alert on real changes.

The court's history page changes for boring reasons (whitespace, a re-render),
so the differ compares *parsed* hearings rather than page bytes, and only
reports the three things a barrister would want a phone buzz for:

* a new hearing appeared on the case's history;
* a result was filled in for a hearing that previously had none;
* a recorded result was amended.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from ..db import record_snapshot
from ..scrapers.case_status import CaseStatus, Hearing
from .notify import Notifier
from .watchlist import queue_alert

CHANGE_LABELS = {
    "new_hearing": "New listing",
    "result_added": "Result recorded",
    "result_changed": "Result amended",
    "first_seen": "Now tracking",
}


@dataclass(frozen=True)
class StatusChange:
    kind: str
    hearing: Hearing
    previous_result: str | None = None

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        parts = [f"{self.label}:"]
        if self.hearing.date:
            parts.append(self.hearing.date)
        if self.hearing.court:
            parts.append(f"— {self.hearing.court}")
        if self.kind == "result_changed":
            parts.append(f"— result: {self.previous_result!r} → {self.hearing.result!r}")
        elif self.hearing.result:
            parts.append(f"— result: {self.hearing.result}")
        return " ".join(parts)


def _hearing_key(hearing: Hearing | dict[str, Any]) -> tuple:
    if isinstance(hearing, dict):
        return (hearing.get("number"), hearing.get("date"), hearing.get("court"))
    return (hearing.number, hearing.date, hearing.court)


def diff_status(previous: dict[str, Any] | None, current: CaseStatus) -> list[StatusChange]:
    """Compare a stored snapshot payload against a freshly parsed status."""
    if previous is None:
        # First sighting: report only the most recent listing, not all 22.
        latest = current.latest
        return [StatusChange("first_seen", latest)] if latest else []

    old_by_key = {_hearing_key(h): h for h in previous.get("hearings", [])}
    changes: list[StatusChange] = []

    for hearing in current.hearings:
        key = _hearing_key(hearing)
        old = old_by_key.get(key)
        if old is None:
            changes.append(StatusChange("new_hearing", hearing))
            continue

        old_result = (old.get("result") or "").strip() or None
        new_result = (hearing.result or "").strip() or None
        if old_result == new_result:
            continue
        if old_result is None:
            changes.append(StatusChange("result_added", hearing))
        elif new_result is not None:
            changes.append(StatusChange("result_changed", hearing, previous_result=old_result))

    # Newest first, matching how the court presents the history.
    return sorted(changes, key=lambda c: (c.hearing.number is None, -(c.hearing.number or 0)))


def format_status_alert(status: CaseStatus, changes: Sequence[StatusChange]) -> tuple[str, str]:
    subject = f"{status.case_ref}: {len(changes)} update(s)"
    lines = [f"{status.case_ref}", ""]
    for change in changes:
        lines.append(f"• {change.describe()}")
        if change.hearing.judges:
            lines.append(f"   Bench: {change.hearing.judges}")
    if status.petitioner or status.respondent:
        lines += ["", f"{status.petitioner or '?'} vs {status.respondent or '?'}"]
    if status.source_url:
        lines += ["", f"Source: {status.source_url}"]
    lines.append("Verify against the official record before relying on it.")
    return subject, "\n".join(lines)


def track_case(
    conn: sqlite3.Connection,
    user_id: int,
    status: CaseStatus,
    notifier: Notifier | None = None,
    *,
    alert_on_first_seen: bool = False,
) -> list[StatusChange]:
    """Snapshot a case, queue an alert for whatever changed, return the changes."""
    key = f"{status.division_id}:{status.case_ref}"
    payload = status.to_dict()
    changed, previous = record_snapshot(conn, "case_status", key, payload)
    if not changed:
        return []

    changes = diff_status(previous, status)
    if not changes:
        return []
    if not alert_on_first_seen and all(c.kind == "first_seen" for c in changes):
        return changes

    subject, body = format_status_alert(status, changes)
    dedupe = "casestatus:" + key + ":" + "|".join(
        f"{c.kind}@{c.hearing.number}:{c.hearing.result}" for c in changes
    )
    queue_alert(conn, user_id, dedupe, subject, body)

    if notifier is not None:
        from .watchlist import deliver_pending

        deliver_pending(conn, notifier)
    return changes
