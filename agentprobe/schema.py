"""The normalized session record every probe produces.

One schema, regardless of which agent the session came from. Probes translate
their tool's native storage into these dataclasses; nothing downstream knows or
cares what wrote the original records.

Kept 3.9-safe (`from __future__ import annotations`, no PEP 604 unions at
runtime) because the probes run on whatever python a given box happens to have.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Firestore hard-caps a document at 1 MiB. Transcripts routinely exceed that, so
# they are split across a `parts/` subcollection. 700k leaves comfortable room
# for the rest of the doc plus UTF-8 expansion.
CHUNK_CHARS = 700_000

SCHEMA_VERSION = 1


@dataclass
class Command:
    """A single shell invocation the agent made."""

    ts: str
    command: str
    description: str = ""
    tool_id: str = ""
    exit_status: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


#: Per-artifact content cap. Firestore allows 1 MiB per document and the rest of
#: the record needs room; anything larger is stored truncated with a flag, and
#: the untruncated text is still in the transcript chunks.
MAX_CONTENT = 200_000


@dataclass
class FileTouch:
    """A file the agent read, wrote or edited.

    For writes and edits the produced content is captured, not just the path.
    The transcript already contains it, but only as raw JSONL - storing it here
    means a script the agent wrote is a document you can open, rather than
    something to dig out of a 2 MB chunk.

    Reads carry no content: the payload lands in a tool_result rather than the
    tool_use input, it is usually a file that already exists on disk, and
    including it would multiply the size of every session for no recall value.
    """

    ts: str
    path: str
    action: str  # read | write | edit
    content: str = ""
    bytes: int = 0
    truncated: bool = False
    #: for edits, what was replaced - enough to see the change, not the file
    replaced: str = ""

    def __post_init__(self) -> None:
        if self.content:
            self.bytes = len(self.content.encode("utf-8", "replace"))
            if len(self.content) > MAX_CONTENT:
                self.content = self.content[:MAX_CONTENT]
                self.truncated = True
        if len(self.replaced) > 4_000:
            self.replaced = self.replaced[:4_000]

    @property
    def name(self) -> str:
        import os

        return os.path.basename(self.path) or self.path

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def summary(self) -> Dict[str, Any]:
        """Content-free form, for the session document's inline list."""
        d = dataclasses.asdict(self)
        d.pop("content", None)
        d.pop("replaced", None)
        return d


@dataclass
class Session:
    """One agent session, normalized.

    `project` is the last path segment of `cwd` - the same convention the
    dashboard groups by. It is deliberately not the git repo name: plenty of
    work happens outside a repo, and cwd is always present.
    """

    provider: str          # claude | antigravity | gemini | copilot | cursor
    session_id: str
    date: str              # YYYY-MM-DD, derived from `started`
    started: str
    ended: str

    cwd: str
    project: str
    user_email: str
    os_user: str
    host: str

    git_branch: str = ""
    agent_version: str = ""
    message_count: int = 0
    preview: str = ""

    #: Every distinct YYYY-MM-DD on which this session had activity.
    #: Sessions get resumed - one observed here ran 22 Jun to 11 Aug - so
    #: indexing by start date alone would hide it on every day but the first.
    #: Queried with array-contains, which Firestore indexes natively.
    active_days: List[str] = field(default_factory=list)

    commands: List[Command] = field(default_factory=list)
    files: List[FileTouch] = field(default_factory=list)
    transcript: str = ""

    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------ #

    @property
    def day_key(self) -> str:
        """YYYYMMDD - the document id under `days/`."""
        return self.date.replace("-", "")

    @property
    def transcript_chunks(self) -> List[str]:
        t = self.transcript
        return [t[i:i + CHUNK_CHARS] for i in range(0, len(t), CHUNK_CHARS)] or []

    @property
    def transcript_sha256(self) -> str:
        return hashlib.sha256(self.transcript.encode("utf-8", "replace")).hexdigest()

    def summary_doc(self) -> Dict[str, Any]:
        """The light document the dashboard lists.

        Every field the sidebar filters on is duplicated here as a plain field,
        not left implicit in the document path: Firestore cannot filter a
        collection-group query on ancestor path segments.
        """
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "session_id": self.session_id,
            "date": self.date,
            "started": self.started,
            "ended": self.ended,
            "cwd": self.cwd,
            "project": self.project,
            "user_email": self.user_email,
            "os_user": self.os_user,
            "host": self.host,
            "git_branch": self.git_branch,
            "agent_version": self.agent_version,
            "message_count": self.message_count,
            "command_count": len(self.commands),
            # Precomputed because the dashboard's list query reads summary
            # documents only - commands live in a subcollection and a session
            # here carries 141 of them. Without this the card could not show a
            # failure count without fetching every command of every session.
            "failed_count": sum(1 for c in self.commands if c.exit_status == 1),
            "file_count": len(self.files),
            "preview": self.preview,
            "transcript_chunks": len(self.transcript_chunks),
            "transcript_bytes": len(self.transcript.encode("utf-8", "replace")),
            "transcript_sha256": self.transcript_sha256,
        }
