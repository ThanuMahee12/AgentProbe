"""Context items: links, tickets, sheets and notes, keyed to the same day timeline
as sessions.

Two ways an item gets here:

* **Automatically** - every URL pasted into any agent chat is pulled out of the
  transcript by `extract_from_session`. Regex only, no model, so it costs nothing
  and requires no discipline from the user.
* **Manually** - `ContextItem` constructed directly for things that never
  appeared in a chat.

Identity is the *normalized URL*, not the mention. A link pasted in five
sessions is one item that records five references, not five rows.

Firestore has no full-text search, so `keywords` is built at write time and
queried with `array-contains-any`. That is the whole search story - deliberately
no Algolia/Typesense until keyword matching demonstrably stops being enough.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .schema import Session

# --------------------------------------------------------------------------- #
# URL detection
# --------------------------------------------------------------------------- #

# Trailing punctuation is almost always prose, not part of the URL: a link at the
# end of a sentence, wrapped in parens, or inside a markdown [label](url).
_TRAILING = ')>,.;:!?"\'`]}'

URL_RE = re.compile(r"https?://[^\s<>\"'`\\]+", re.IGNORECASE)

#: domain fragment -> (source, item type, regex capturing the external id)
SOURCE_PATTERNS: List[Tuple[str, str, str, Optional[str]]] = [
    ("app.clickup.com",   "clickup",       "ticket",  r"/t/([A-Za-z0-9_-]+)"),
    ("docs.google.com/spreadsheets", "google-sheets", "sheet",
     r"/spreadsheets/d/([A-Za-z0-9_-]+)"),
    ("docs.google.com/document",     "google-docs",   "doc",
     r"/document/d/([A-Za-z0-9_-]+)"),
    ("drive.google.com",  "google-drive",  "file",    r"/d/([A-Za-z0-9_-]+)"),
    ("slack.com/archives", "slack",        "thread",  r"/archives/([A-Z0-9]+)"),
    # gist before github - gist.github.com contains "github.com"
    ("gist.github.com",   "github",        "gist",    r"gist\.github\.com/([^/]+/[0-9a-f]+)"),
    ("github.com",        "github",        "repo",    r"github\.com/([^/]+/[^/#?]+)"),
    # self-hosted GitLab is matched before gitlab.com so the work instance keeps
    # its own source label rather than being lumped in with the public one
    ("git.codewilling.com", "gitlab",      "repo",    r"git\.codewilling\.com/(.+?)/-/"),
    ("gitlab.com",        "gitlab",        "repo",    r"gitlab\.com/([^/]+/[^/#?]+)"),
    ("atlassian.net",     "jira",          "ticket",  r"/browse/([A-Z][A-Z0-9]*-\d+)"),
    ("notion.so",         "notion",        "page",    None),
    ("figma.com",         "figma",         "design",  None),
]

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "com", "for", "from",
    "has", "have", "http", "https", "i", "if", "in", "is", "it", "its", "of", "on",
    "or", "que", "so", "that", "the", "then", "there", "this", "to", "was", "we",
    "were", "what", "when", "which", "will", "with", "www", "you", "your",
}

MAX_KEYWORDS = 60
MAX_BODY = 500


def normalize_url(url: str) -> str:
    """Strip trailing prose punctuation and common tracking noise.

    Normalization decides identity, so it must be stable: the same link pasted
    with and without a trailing period has to collapse to one item.
    """
    url = url.strip()
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    # A clone URL and its web URL are the same thing to a human, so collapse the
    # `.git` suffix rather than carrying two items for one repo.
    if url.endswith(".git"):
        url = url[:-4]
    # drop utm_* and fbclid, keep everything else - query params are often
    # load-bearing (sheet gid, clickup view)
    if "?" in url:
        base, _, query = url.partition("?")
        kept = [
            p for p in query.split("&")
            if p and not p.lower().startswith(("utm_", "fbclid=", "gclid="))
        ]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url.rstrip("/") if url.count("/") > 3 else url


#: Markers that mean a "URL" is documentation, not a destination:
#: `.../projects/{proj}/...`, `<id>`, or an ellipsis where a console truncated it.
_TEMPLATE_MARKERS = ("{", "}", "<", ">", "...", "…", "%s", "$")


def is_template_url(url: str) -> bool:
    return any(marker in url for marker in _TEMPLATE_MARKERS)


def collapse_prefixes(items: Dict[str, "ContextItem"]) -> Dict[str, "ContextItem"]:
    """Merge URLs that are strict prefixes of a longer one.

    Terminal output and log lines truncate URLs for display, so the same link
    shows up as `.../d/1AbC_`, `.../d/1AbC_dE` and the real `.../d/1AbC_dEf`.
    Without this they become three separate context items for one sheet.

    Longest wins and absorbs the others' mentions. Only applied to same-source
    URLs so unrelated short links are never folded into a longer neighbour.
    """
    ordered = sorted(items.values(), key=lambda i: len(i.url), reverse=True)
    kept: List["ContextItem"] = []
    for item in ordered:
        for winner in kept:
            if winner.source == item.source and winner.url.startswith(item.url):
                winner.merge(item)
                break
        else:
            kept.append(item)
    return {i.doc_id: i for i in kept}


def classify(url: str) -> Tuple[str, str, str]:
    """-> (source, type, external_id). Unknown domains are still worth keeping."""
    low = url.lower()
    for fragment, source, itype, id_re in SOURCE_PATTERNS:
        if fragment in low:
            external_id = ""
            if id_re:
                m = re.search(id_re, url)
                if m:
                    external_id = m.group(1)
            return source, itype, external_id
    return "web", "link", ""


def tokenize(*parts: str) -> List[str]:
    """Lowercase alphanumeric tokens, stopwords dropped, order preserved.

    Order is kept (rather than sorting) so the most distinctive words - which
    tend to appear in the title - survive the MAX_KEYWORDS cut.
    """
    seen: List[str] = []
    for part in parts:
        if not part:
            continue
        for tok in re.split(r"[^A-Za-z0-9]+", part.lower()):
            if len(tok) < 2 or tok in STOPWORDS or tok.isdigit() and len(tok) > 6:
                continue
            if tok not in seen:
                seen.append(tok)
            if len(seen) >= MAX_KEYWORDS:
                return seen
    return seen


# --------------------------------------------------------------------------- #


@dataclass
class ContextItem:
    url: str
    source: str = "web"
    type: str = "link"
    external_id: str = ""
    title: str = ""
    body: str = ""

    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)

    date: str = ""
    project: str = ""
    user_email: str = ""
    provider: str = ""

    #: sessions that mentioned this link, and when it was first/last seen
    sessions: List[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    mention_count: int = 0

    pinned: bool = False

    @property
    def doc_id(self) -> str:
        """Stable id derived from the URL, so re-runs upsert instead of duplicate."""
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["doc_id"] = self.doc_id
        return d

    def merge(self, other: "ContextItem") -> None:
        """Fold a later sighting of the same URL into this item."""
        for sid in other.sessions:
            if sid not in self.sessions:
                self.sessions.append(sid)
        self.mention_count += other.mention_count
        if other.first_seen and (not self.first_seen or other.first_seen < self.first_seen):
            self.first_seen = other.first_seen
            self.date = other.date or self.date
        if other.last_seen and other.last_seen > self.last_seen:
            self.last_seen = other.last_seen
        if not self.body and other.body:
            self.body = other.body
        for kw in other.keywords:
            if kw not in self.keywords and len(self.keywords) < MAX_KEYWORDS:
                self.keywords.append(kw)


# --------------------------------------------------------------------------- #


def extract_from_session(session: Session) -> List[ContextItem]:
    """Pull every URL out of a session's transcript as a deduped ContextItem list.

    Reads the raw transcript rather than the parsed message list so links in
    tool output, file contents and attachments are caught too - those are often
    where the useful ticket link actually is.
    """
    items: Dict[str, ContextItem] = {}

    for line in session.transcript.splitlines():
        line = line.strip()
        if not line or "http" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue

        ts = rec.get("timestamp") or session.started
        prose, raw = _record_text(rec)
        if "http" not in prose and "http" not in raw:
            continue

        # Scan both, but remember which side a URL came from: a link the user
        # typed deserves its surrounding sentence as context; one scraped out of
        # command output does not.
        for text, is_prose in ((prose, True), (raw, False)):
            if not text or "http" not in text:
                continue
            for raw_url in URL_RE.findall(text):
                url = normalize_url(raw_url)
                if not url or len(url) < 12 or is_template_url(url):
                    continue
                source, itype, external_id = classify(url)
                body = _snippet(text, raw_url) if is_prose else ""
                item = ContextItem(
                    url=url,
                    source=source,
                    type=itype,
                    external_id=external_id,
                    title="",  # enriched later via the relevant connector
                    body=body,
                    # URL always indexes; prose only when a human wrote it
                    keywords=tokenize(url, body),
                    date=(ts or "")[:10],
                    project=session.project,
                    user_email=session.user_email,
                    provider=session.provider,
                    sessions=[session.session_id],
                    first_seen=ts,
                    last_seen=ts,
                    mention_count=1,
                )
                existing = items.get(item.doc_id)
                if existing:
                    existing.merge(item)
                else:
                    items[item.doc_id] = item

    return list(collapse_prefixes(items).values())


def _record_text(rec: Dict[str, Any]) -> Tuple[str, str]:
    """-> (prose, raw).

    `prose` is text a human or the model actually wrote. `raw` is tool output:
    command stdout, file contents, JSON blobs.

    Both are scanned for URLs - the useful ticket link is often buried in tool
    output - but only `prose` is tokenized into keywords. Indexing raw output
    fills the search index with shell noise (`gawk`, `csh`, `issidechain`) that
    matches everything and means nothing.
    """
    prose: List[str] = []
    raw: List[str] = []

    message = rec.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            prose.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    prose.append(block.get("text") or "")
                elif btype == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str):
                        raw.append(c)
                    elif isinstance(c, list):
                        for sub in c:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                raw.append(sub.get("text") or "")
                elif btype == "tool_use":
                    inp = block.get("input")
                    if isinstance(inp, dict):
                        raw.append(json.dumps(inp))

    val = rec.get("lastPrompt")
    if isinstance(val, str):
        prose.append(val)
    val = rec.get("content")
    if isinstance(val, str):
        raw.append(val)

    return ("\n".join(p for p in prose if p), "\n".join(p for p in raw if p))


def _snippet(text: str, url: str) -> str:
    """The sentence-ish window around the URL - cheap context for a bare link."""
    idx = text.find(url)
    if idx < 0:
        return text[:MAX_BODY].strip()
    start = max(0, idx - MAX_BODY // 2)
    end = min(len(text), idx + len(url) + MAX_BODY // 2)
    return text[start:end].strip()


def extract_many(sessions: Iterable[Session]) -> List[ContextItem]:
    """Extract across sessions, merging repeat sightings of the same URL."""
    merged: Dict[str, ContextItem] = {}
    for session in sessions:
        for item in extract_from_session(session):
            existing = merged.get(item.doc_id)
            if existing:
                existing.merge(item)
            else:
                merged[item.doc_id] = item
    merged = collapse_prefixes(merged)
    return sorted(merged.values(), key=lambda i: (i.last_seen or ""), reverse=True)
