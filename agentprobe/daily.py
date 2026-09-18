"""Per-day activity totals, safe to show without signing in.

The session archive is private and stays private: `firestore.rules` denies every
session to an anonymous reader. But "which days had work on them" is not the same
information as "what the work was", and a calendar is useless without it.

So this writes a separate, deliberately thin document per day: **counts only**.
No project names, no previews, no commands, no paths, no user. A visitor learns
that a day was busy; they learn nothing about what happened on it.

That split is the point. Deriving the public view from the private collection at
read time would mean one missing filter exposes everything; a separate document
cannot leak what it never contained.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import Config
from .store import Firestore, build_query

COLLECTION = "daily"

#: Fields copied into the public document. Anything not named here never leaves
#: the private collection - the allowlist is the security boundary, so it is
#: spelled out rather than derived by removing keys from the session document.
PUBLIC_FIELDS = ("date", "sessions", "messages", "commands", "files", "failed")


def collect(store: Firestore, since: str = "") -> Dict[str, Dict[str, Any]]:
    """Aggregate every session into per-day totals."""
    where = [("date", ">=", since)] if since else None
    rows = store.run_query(build_query(
        "sessions", where=where, order_by="date", desc=True,
        limit=3000, all_descendants=True))

    days: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        date = str(r.get("date") or "")[:10]
        if not date:
            continue
        # Subagent transcripts are part of a session, not sessions of their own.
        # Counting them would inflate a quiet day into a busy-looking one.
        if r.get("is_sidechain"):
            continue
        d = days.setdefault(date, {
            "date": date, "sessions": 0, "messages": 0,
            "commands": 0, "files": 0, "failed": 0,
        })
        d["sessions"] += 1
        d["messages"] += int(r.get("message_count") or 0)
        d["commands"] += int(r.get("command_count") or 0)
        d["files"] += int(r.get("file_count") or 0)
        d["failed"] += int(r.get("failed_count") or 0)
    return days


def publish(store: Optional[Firestore] = None, since: str = "",
            dry_run: bool = False) -> Dict[str, Any]:
    store = store or Firestore(Config.load())
    days = collect(store, since=since)

    stats: Dict[str, Any] = {"days": len(days), "sessions": sum(d["sessions"] for d in days.values())}
    if dry_run or not days:
        return stats

    writes: List[Dict[str, Any]] = []
    for date, d in sorted(days.items()):
        # Rebuilt through the allowlist rather than written straight through, so
        # a field added to the aggregate later cannot reach the public document
        # by accident.
        writes.append(store.write("%s/%s" % (COLLECTION, date),
                                  {k: d[k] for k in PUBLIC_FIELDS}))
    stats["written"] = store.commit(writes)
    return stats
