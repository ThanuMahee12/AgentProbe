"""Memory entries: facts an agent learned, kept somewhere every agent can reach.

Claude Code stores memory per *project directory* - `~/.claude/projects/<slug>/memory/`.
On this machine that means 64 files scattered across 13 directories and two
accounts, so a fact learned in `data-alchemy` is invisible from `injestion`, and
nothing survives a rebuilt box. Gemini, OpenCode and the rest each keep their own
in their own format, none of which the others can read.

This is the shared form. One record per fact, keyed by a slug, carrying enough
scope to be filtered rather than siloed: a fact can be global, tied to one user,
or tied to one project, but it lives in the same collection either way and a
query decides what is relevant. That is the difference between centralizing and
merely relocating the fragmentation.

Deliberately the same shape as the markdown files it replaces - frontmatter
`name`/`description`/`type` plus a body - so importing an existing tree is a
parse, not a translation, and exporting back to files stays lossless.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .context import tokenize

#: The vocabulary Claude Code's own memory files already use. Kept identical so
#: an imported file does not have to be reclassified.
TYPES = ("user", "feedback", "project", "reference")

#: global - true everywhere. user - this account. project:<name> - one project.
SCOPE_GLOBAL = "global"
SCOPE_USER = "user"

MAX_BODY = 100_000

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """A stable document id. Memory is keyed by name, so this decides identity."""
    return _SLUG_STRIP.sub("-", (text or "").strip().lower()).strip("-")[:120] or "unnamed"


def owner_slug(email: str) -> str:
    """Firestore path segment for an account.

    The existing rule is `match /memory/{userId}/{document=**}`, so the owner is
    a path segment and has to survive being one - no dots, no @.
    """
    return slugify(email) or "unknown"


@dataclass
class Memory:
    """One remembered fact."""

    name: str
    body: str
    description: str = ""
    type: str = "project"
    scope: str = SCOPE_GLOBAL

    project: str = ""
    tags: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)

    #: Where it came from, so a wrong fact can be traced back to the session
    #: that introduced it rather than just deleted and re-learned.
    origin_session: str = ""
    origin_agent: str = ""
    origin_host: str = ""
    user_email: str = ""
    #: The OS account that wrote it. Distinct from user_email: the machine-global
    #: config carries one email, but four accounts run agents under it, and
    #: "who taught it this" is the question you ask when a fact turns out wrong.
    os_user: str = ""

    created: str = ""
    updated: str = ""

    keywords: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            self.type = "project"
        if len(self.body) > MAX_BODY:
            self.body = self.body[:MAX_BODY]
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.created = self.created or now
        self.updated = now
        if not self.keywords:
            self.keywords = tokenize(self.name, self.description, self.body)
        if not self.links:
            self.links = wiki_links(self.body)

    @property
    def doc_id(self) -> str:
        return slugify(self.name)

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["doc_id"] = self.doc_id
        return d

    def to_markdown(self) -> str:
        """Back to the on-disk form, so export is lossless."""
        lines = ["---", "name: %s" % self.name]
        if self.description:
            lines.append("description: %s" % self.description)
        lines.append("metadata:")
        lines.append("  type: %s" % self.type)
        if self.scope and self.scope != SCOPE_GLOBAL:
            lines.append("  scope: %s" % self.scope)
        lines.append("---")
        lines.append("")
        lines.append(self.body)
        return "\n".join(lines)


def wiki_links(body: str) -> List[str]:
    """`[[other-memory]]` references. They are how entries relate."""
    return list(dict.fromkeys(re.findall(r"\[\[([^\]]+)\]\]", body or "")))


def parse_markdown(raw: str, fallback_name: str = "") -> Optional[Memory]:
    """Parse one of Claude Code's memory files.

    Frontmatter is YAML-ish rather than YAML: the files in the wild use both a
    flat `type:` and a nested `metadata:`/`  type:`, so both are accepted rather
    than insisting on one and dropping half the tree on import.
    """
    if not (raw or "").strip():
        return None

    meta: Dict[str, str] = {}
    body = raw.strip()
    m = _FRONTMATTER.match(raw)
    if m:
        body = raw[m.end():].strip()
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lstrip("-").strip()
            val = val.strip().strip("\"'")
            if key and val:
                meta.setdefault(key, val)

    name = meta.get("name") or fallback_name
    if not name:
        return None

    return Memory(
        name=name,
        description=meta.get("description", ""),
        body=body,
        type=meta.get("type", "project"),
        scope=meta.get("scope", SCOPE_GLOBAL),
        project=meta.get("project", ""),
        origin_session=meta.get("originSessionId", "") or meta.get("origin_session", ""),
        created=meta.get("created", "") or meta.get("modified", ""),
    )


def scope_matches(entry_scope: str, want: str) -> bool:
    """Whether an entry is in play for a requested scope.

    `global` is always in play - that is what makes it global. Asking for a
    project returns that project's entries plus everything global, which is the
    behaviour that stops centralizing from turning into one undifferentiated pile.
    """
    entry_scope = entry_scope or SCOPE_GLOBAL
    if entry_scope == SCOPE_GLOBAL or not want or want == "all":
        return True
    return entry_scope == want
