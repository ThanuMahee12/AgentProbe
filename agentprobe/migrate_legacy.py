"""Convert the previous pipeline's Firestore tree into the current schema.

The old layout put the varying values on COLLECTION segments:

    claude/session/{project}/{YYYY}/{YYYYMMDD}/{session_id}

which is why it needs a bespoke walk - `{YYYYMMDD}` is a collection id, so there
is no single collection to group over, and the intermediate `{YYYY}` documents
were never created. Listing documents at any level returns nothing; only
listCollectionIds reveals what is underneath. Counting documents the obvious way
reports an empty tree while 225 sessions sit inside it.

Field mapping, with the gaps stated honestly. The old pipeline never extracted
commands or file touches, so those stay zero - a converted session is a real
session with less detail, not a broken one.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .store import Firestore

LEGACY_ROOT = "claude/session"
SOURCE = "legacy-pipeline"


def _s(fields: Dict[str, Any], key: str, default: str = "") -> str:
    return fields.get(key, {}).get("stringValue", default) or default


def _i(fields: Dict[str, Any], key: str, default: int = 0) -> int:
    raw = fields.get(key, {}).get("integerValue")
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _arr(fields: Dict[str, Any], key: str) -> List[Any]:
    return fields.get(key, {}).get("arrayValue", {}).get("values", []) or []


class LegacyMigrator:
    def __init__(self, store: Firestore) -> None:
        self.store = store
        self.h = store._headers()

    # -- traversal -------------------------------------------------------- #

    def _cols(self, doc_path: str) -> List[str]:
        r = requests.post(
            "%s/%s:listCollectionIds" % (self.store.base, doc_path),
            headers=self.h, data="{}", timeout=60,
        )
        return r.json().get("collectionIds", []) if r.status_code == 200 else []

    def _docs(self, collection: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor = None
        while True:
            url = "%s/%s?pageSize=300" % (self.store.base, collection)
            if cursor:
                url += "&pageToken=%s" % cursor
            r = requests.get(url, headers=self.h, timeout=60).json()
            out.extend(r.get("documents", []))
            cursor = r.get("nextPageToken")
            if not cursor:
                break
        return out

    def walk(self) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """Yield (legacy_path, document) for every legacy session."""
        for project in self._cols(LEGACY_ROOT):
            proj_path = "%s/%s" % (LEGACY_ROOT, project)
            # Year documents may be implicit, so take the union of any that do
            # exist with the collection ids reachable beneath them.
            years = {d["name"].split("/")[-1] for d in self._docs(proj_path)}
            years |= {"2025", "2026", "2027"}
            for year in sorted(years):
                year_path = "%s/%s" % (proj_path, year)
                for day in self._cols(year_path):
                    for doc in self._docs("%s/%s" % (year_path, day)):
                        yield doc["name"].split("/documents/")[-1], doc

    # -- conversion ------------------------------------------------------- #

    @staticmethod
    def convert(fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        session_id = _s(fields, "session_id")
        date = _s(fields, "date")
        if not session_id or not date:
            return None

        # The old record kept a start `time` and a full `ended_at`; rebuild an
        # ISO start from the two rather than leaving `started` empty, since the
        # timeline sorts on it.
        time_of_day = _s(fields, "time")
        started = "%sT%s:00Z" % (date, time_of_day) if time_of_day else _s(fields, "ended_at")

        preview = ""
        for entry in _arr(fields, "conversation"):
            f = entry.get("mapValue", {}).get("fields", {})
            if f.get("role", {}).get("stringValue") == "user":
                preview = (f.get("text", {}).get("stringValue") or "").strip()[:280]
                if preview:
                    break

        cwd = _s(fields, "cwd")
        return {
            "schema_version": 1,
            "provider": "claude",
            "source": SOURCE,
            "session_id": session_id,
            "parent_session_id": session_id,
            "is_sidechain": False,
            "date": date,
            "started": started,
            "ended": _s(fields, "ended_at"),
            "cwd": cwd,
            "project": _s(fields, "project") or (cwd.rsplit("/", 1)[-1] if cwd else "unknown"),
            "user_email": "",
            "os_user": "",
            "host": "",
            "git_branch": "",
            "agent_version": "",
            "message_count": _i(fields, "turns_user") + _i(fields, "turns_assistant"),
            # The old pipeline never extracted these. Zero is the truth, not a
            # placeholder - the detail was never captured and cannot be recovered.
            "command_count": 0,
            "failed_count": 0,
            "file_count": 0,
            "preview": preview,
            "tools": [v.get("stringValue", "") for v in _arr(fields, "tools")],
            "transcript_chunks": _i(fields, "transcript_parts"),
            "transcript_bytes": _i(fields, "transcript_bytes"),
            "transcript_sha256": "",
        }

    # -- run -------------------------------------------------------------- #

    def migrate(self, dry_run: bool = False, verbose: bool = False) -> Dict[str, int]:
        stats = {"found": 0, "converted": 0, "skipped": 0, "parts": 0, "writes": 0}
        writes: List[Dict[str, Any]] = []

        for legacy_path, doc in self.walk():
            stats["found"] += 1
            record = self.convert(doc.get("fields", {}))
            if not record:
                stats["skipped"] += 1
                continue

            day_key = record["date"].replace("-", "")
            new_path = "projects/%s/days/%s/sessions/%s" % (
                record["project"], day_key, record["session_id"])
            writes.append(self.store.write(new_path, record))

            for part in self._docs("%s/parts" % legacy_path):
                name = part["name"].split("/")[-1]
                # Copy the typed field map verbatim. Unwrapping and re-encoding
                # turns integerValue into stringValue, because the REST API
                # hands integers back as strings.
                writes.append(
                    self.store.write_raw("%s/parts/%s" % (new_path, name), part.get("fields", {}))
                )
                stats["parts"] += 1

            stats["converted"] += 1
            if verbose:
                print("  %s  %-22s %s" % (record["date"], record["project"][:22], record["session_id"][:8]))

        stats["writes"] = len(writes)
        if not dry_run and writes:
            self.store.commit(writes)
        return stats
