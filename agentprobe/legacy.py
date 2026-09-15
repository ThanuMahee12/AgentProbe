"""Importer for the markdown session notes AgentContext accumulated before this.

Those files are not one format. Across 121 of them there are three generations:

1. **Generated transcripts** - `### Session <id> - HH:MM` blocks with working
   directory, turn counts, tool lists and a `#### Conversation` section.
2. **Notes under a session heading** - `# Session: YYYY-MM-DD (Linux)` followed
   by freeform prose, headings and tables. No session id, no turns.
3. **Plain dated notes** - `# YYYY-MM-DD`, a project heading, a summary list.

Only the first is a session. Coercing the other 77 into the Session schema
would produce records that are mostly empty fields, so everything lands in
`notes` with the markdown preserved, and the structured fields are filled in
only where the source actually provides them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .context import tokenize

#: Firestore allows 1 MiB per document; the largest note here is far smaller,
#: but a cap keeps one pathological file from failing an entire batch.
MAX_BODY = 400_000

FILENAME_RE = re.compile(r"^(?P<platform>[lw])-(?P<date>\d{4}-\d{2}-\d{2})\.md$")
PLAIN_DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")

SESSION_RE = re.compile(r"^###\s+Session\s+(?P<id>[0-9a-f]{6,})\s*[-—–]\s*(?P<time>\d{1,2}:\d{2})", re.M)
CWD_RE = re.compile(r"^\*\*Working Directory:\*\*\s*`?([^`\n]+)`?", re.M)
TURNS_RE = re.compile(r"^\*\*Turns:\*\*\s*(\d+)\s*user\s*/\s*(\d+)\s*assistant", re.M)
TOOLS_RE = re.compile(r"^\*\*Tools:\*\*\s*(.+)$", re.M)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)

PLATFORMS = {"l": "linux", "w": "windows"}


@dataclass
class SessionRef:
    """A session block found inside a generated note."""

    session_id: str
    time: str = ""
    cwd: str = ""
    user_turns: int = 0
    assistant_turns: int = 0
    tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Note:
    date: str
    provider: str            # claude | gemini
    platform: str            # linux | windows | ""
    title: str
    body: str
    source_file: str
    generation: int          # 1 generated, 2 session-headed notes, 3 plain notes
    keywords: List[str] = field(default_factory=list)
    sessions: List[SessionRef] = field(default_factory=list)
    project: str = ""
    bytes: int = 0
    truncated: bool = False

    @property
    def doc_id(self) -> str:
        """Derived from the source path so re-running the import updates in place."""
        return hashlib.sha1(self.source_file.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["doc_id"] = self.doc_id
        d["sessions"] = [s.to_dict() for s in self.sessions]
        d["session_count"] = len(self.sessions)
        d["source"] = "legacy-md"
        return d


# --------------------------------------------------------------------------- #


def parse_file(path: str, provider: str) -> Optional[Note]:
    name = os.path.basename(path)
    if name in ("index.md", "README.md"):
        return None

    platform = ""
    date = ""
    m = FILENAME_RE.match(name)
    if m:
        platform = PLATFORMS.get(m.group("platform"), "")
        date = m.group("date")
    else:
        m = PLAIN_DATE_RE.match(name)
        if m:
            date = m.group("date")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        body = fh.read()

    if not body.strip():
        return None

    # A note whose filename carries no date still usually states one in its
    # heading - losing those would silently drop files from the timeline.
    if not date:
        found = re.search(r"(\d{4}-\d{2}-\d{2})", body[:400])
        if found:
            date = found.group(1)

    title_match = TITLE_RE.search(body)
    title = title_match.group(1).strip() if title_match else name

    refs: List[SessionRef] = []
    for sm in SESSION_RE.finditer(body):
        block = body[sm.start(): sm.start() + 1200]
        cwd_m = CWD_RE.search(block)
        turns_m = TURNS_RE.search(block)
        tools_m = TOOLS_RE.search(block)
        refs.append(
            SessionRef(
                session_id=sm.group("id"),
                time=sm.group("time"),
                cwd=cwd_m.group(1).strip() if cwd_m else "",
                user_turns=int(turns_m.group(1)) if turns_m else 0,
                assistant_turns=int(turns_m.group(2)) if turns_m else 0,
                tools=[t.strip() for t in tools_m.group(1).split(",")] if tools_m else [],
            )
        )

    if refs:
        generation = 1
    elif body.lstrip().startswith("# Session:"):
        generation = 2
    else:
        generation = 3

    # Project is only meaningful when a working directory was recorded.
    project = ""
    for ref in refs:
        if ref.cwd:
            project = os.path.basename(os.path.normpath(ref.cwd))
            break

    raw_len = len(body.encode("utf-8", "replace"))
    truncated = len(body) > MAX_BODY
    if truncated:
        body = body[:MAX_BODY]

    return Note(
        date=date,
        provider=provider,
        platform=platform,
        title=title,
        body=body,
        source_file=path,
        generation=generation,
        # Headings and the title carry the signal; the whole body would swamp
        # the index with prose. tokenize() caps and dedupes.
        keywords=tokenize(title, " ".join(re.findall(r"^#{1,4}\s+(.+)$", body, re.M)[:40])),
        sessions=refs,
        project=project,
        bytes=raw_len,
        truncated=truncated,
    )


def parse_directory(root: str) -> List[Note]:
    """Walk docs/sessions/{claude,gemini}/ and parse every note found."""
    notes: List[Note] = []
    for provider in ("claude", "gemini"):
        directory = os.path.join(root, provider)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".md"):
                continue
            note = parse_file(os.path.join(directory, name), provider)
            if note:
                notes.append(note)
    return notes


def summarize(notes: Iterable[Note]) -> Dict[str, Any]:
    notes = list(notes)
    gens: Dict[int, int] = {}
    for n in notes:
        gens[n.generation] = gens.get(n.generation, 0) + 1
    dated = [n.date for n in notes if n.date]
    return {
        "notes": len(notes),
        "generations": gens,
        "sessions_referenced": sum(len(n.sessions) for n in notes),
        "undated": sum(1 for n in notes if not n.date),
        "range": (min(dated), max(dated)) if dated else ("", ""),
        "bytes": sum(n.bytes for n in notes),
    }
