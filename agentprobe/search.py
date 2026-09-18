"""Keyword search over everything AgentProbe has already pushed.

`push` sends a session up; this brings it back down. That is the whole point -
capture is only worth the writes if the archive is reachable from inside the
next session, and an agent that can answer "how did I mount that bucket last
month" from its own history does not have to ask again.

**Firestore has no full-text search.** Nothing here pretends otherwise. Two
mechanisms carry the whole feature:

* `context` and `notes` were written with a `keywords` array precisely so they
  could be queried later with `array-contains-any`. Exact token match, indexed,
  cheap - but it only ever matches whole tokens.
* everything else is a **bounded recent-window scan**: pull the newest N
  documents of a kind, rank them in memory. Substring matching, partial words
  and phrases all work, at the cost of reading N documents.

The window is the honest trade. A search is not guaranteed to reach the oldest
record of a kind, and `--scan` is the dial. Raising it costs Firestore reads,
which on the free tier are a real daily budget - so the defaults are modest and
the result always reports how many documents it actually looked at.

Queries combining two fields would need composite indexes nobody has created,
so exactly one filter is pushed server-side (the date range, when given) and
every other constraint is applied in memory.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .context import tokenize
from .store import MAX_DISJUNCTS, Firestore, IndexRequired, build_query

#: Everything searchable. Order is the order results are rendered in.
KINDS = ("context", "notes", "docs", "sessions", "commands", "files")

#: How many documents each kind may pull in one search. Deliberately modest:
#: Firestore's free tier allows 50k reads a day and a search that scans 10k
#: documents is a search you can only run five times.
SCAN: Dict[str, int] = {
    "context": 500,
    "notes": 400,
    "docs": 300,
    "sessions": 600,
    "commands": 1200,
    "files": 800,
}

#: Which field a kind is ordered by when taking its recent window, and which
#: field supplies the displayed date.
ORDER: Dict[str, str] = {
    "context": "last_seen",
    "notes": "date",
    "docs": "date",
    "sessions": "date",
    "commands": "ts",
    "files": "ts",
}

#: (field, weight) per kind. A term in a title says far more about relevance
#: than the same term buried in a 40 KB body, and without weighting every long
#: document outranks the short one that is actually about the subject.
WEIGHTS: Dict[str, Tuple[Tuple[str, float], ...]] = {
    "context": (("url", 3.0), ("title", 3.0), ("external_id", 3.0),
                ("keywords", 2.0), ("source", 1.0), ("body", 1.0)),
    "notes": (("title", 3.0), ("keywords", 2.0), ("project", 1.5), ("body", 1.0)),
    "docs": (("title", 3.0), ("tags", 2.5), ("description", 2.0), ("path", 2.0),
             ("section", 1.0), ("body", 1.0)),
    "sessions": (("session_id", 3.0), ("project", 2.5), ("git_branch", 2.0),
                 ("preview", 2.0), ("cwd", 1.5)),
    "commands": (("command", 3.0), ("description", 1.5)),
    "files": (("path", 3.0), ("action", 0.5)),
}

#: Where a one-line snippet is drawn from, in order of preference.
SNIPPET: Dict[str, Tuple[str, ...]] = {
    "context": ("body", "title", "url"),
    "notes": ("body", "title"),
    "docs": ("description", "body"),
    "sessions": ("preview", "cwd"),
    "commands": ("command", "description"),
    "files": ("path",),
}

SNIPPET_CHARS = 220
#: `--full` widens it rather than removing the cap - an untruncated transcript
#: window is a 700k-character chunk.
SNIPPET_CHARS_FULL = 900

#: Deep search reads transcript chunks, which are 700k characters each. Both
#: caps are needed: a session count alone says nothing about the bytes behind it.
DEEP_SESSIONS = 10
DEEP_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------- #
# the query
# --------------------------------------------------------------------------- #


_QUOTED = re.compile(r'^\s*(?:"(?P<d>.+)"|\'(?P<s>.+)\')\s*$', re.S)


@dataclass
class Query:
    """A parsed search query.

    `terms` drives the indexed `array-contains-any` lookups and the in-memory
    substring scoring. `phrase` is a bonus - or, when the whole query was
    quoted, a hard requirement.
    """

    raw: str
    terms: List[str] = field(default_factory=list)
    phrase: str = ""
    exact: bool = False

    def __bool__(self) -> bool:
        return bool(self.terms or self.phrase)


def parse_query(raw: str) -> Query:
    """Text -> Query.

    A fully quoted query means exact phrase: `"daily write quota"` must appear
    as written. Otherwise the words are independent terms and their adjacency
    is only a ranking bonus.
    """
    raw = (raw or "").strip()
    m = _QUOTED.match(raw)
    if m:
        inner = (m.group("d") or m.group("s")).strip()
        return Query(raw=inner, terms=_terms(inner), phrase=inner.lower(), exact=True)

    terms = _terms(raw)
    # A multi-word query still rewards the words appearing together, it just
    # does not insist on it.
    phrase = raw.lower() if len(raw.split()) > 1 else ""
    return Query(raw=raw, terms=terms, phrase=phrase)


def _terms(raw: str) -> List[str]:
    """The same tokenizer that built the stored keywords, so a term can match.

    Falling back to raw words matters: `tokenize` drops stopwords and short
    numbers, and a query of nothing but those would otherwise search for
    nothing at all and silently return everything.
    """
    terms = tokenize(raw)
    if terms:
        return terms
    return [w for w in re.split(r"[^A-Za-z0-9]+", raw.lower()) if len(w) >= 2]


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass
class Hit:
    kind: str
    score: float
    title: str
    snippet: str
    ref: str
    date: str = ""
    project: str = ""
    session_id: str = ""
    path: str = ""
    matched: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["score"] = round(self.score, 2)
        return d


@dataclass
class Results:
    query: Query
    hits: List[Hit] = field(default_factory=list)
    scanned: int = 0
    #: kind -> why it returned nothing useful, when the reason is not "no match"
    warnings: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query.raw,
            "terms": self.query.terms,
            "exact": self.query.exact,
            "scanned": self.scanned,
            "elapsed": round(self.elapsed, 3),
            "warnings": self.warnings,
            "hits": [h.to_dict() for h in self.hits],
        }


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    """Any stored field as one searchable string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    return str(value)


def score(query: Query, doc: Dict[str, Any], weights: Sequence[Tuple[str, float]]) -> Tuple[float, List[str]]:
    """-> (score, matched terms). Zero means this document is not a result."""
    total = 0.0
    matched: List[str] = []
    phrase_found = False

    for name, weight in weights:
        low = _text(doc.get(name)).lower()
        if not low:
            continue
        for term in query.terms:
            n = low.count(term)
            if not n:
                continue
            if term not in matched:
                matched.append(term)
            # Diminishing returns. The tenth occurrence of a word says almost
            # nothing the second did not, and without this a long log file
            # outranks the document actually about the subject.
            total += weight * (1.0 + min(n - 1, 4) * 0.25)
        if query.phrase and query.phrase in low:
            phrase_found = True
            total += weight * 2.0

    if query.exact and not phrase_found:
        return 0.0, []
    if not total:
        return 0.0, []

    # Matching three of three terms is a much better answer than one of three,
    # and raw occurrence counts do not express that on their own.
    if query.terms:
        total *= 0.4 + 0.6 * (len(matched) / len(query.terms))
    return total, matched


def recency_bonus(date: str, today: Optional[dt.date] = None) -> float:
    """A small, bounded nudge towards recent work.

    Bounded on purpose: this ranks two comparable matches, it must never float
    an irrelevant document from yesterday above the right one from June.
    """
    if not date or len(date) < 10:
        return 0.0
    try:
        when = dt.date.fromisoformat(date[:10])
    except ValueError:
        return 0.0
    age = ((today or dt.date.today()) - when).days
    if age < 0:
        return 2.0
    return max(0.0, 1.0 - age / 365.0) * 2.0


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


_SESSION_PATH = re.compile(
    r"^projects/(?P<project>[^/]+)/days/(?P<day>\d{8})/sessions/(?P<session>[^/]+)"
)


def path_facts(path: str) -> Dict[str, str]:
    """project / day / session id, read back out of the document path.

    A command document holds only a timestamp and the text; nothing in its
    fields says which session or project it belongs to. The path is the only
    place that survives, which is why every kind carries `_path`.
    """
    m = _SESSION_PATH.match(path or "")
    if not m:
        return {}
    day = m.group("day")
    return {
        "project": m.group("project"),
        "date": "%s-%s-%s" % (day[:4], day[4:6], day[6:]),
        "session_id": m.group("session"),
    }


def _chunks(items: Sequence[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _snippet(doc: Dict[str, Any], kind: str, query: Query, width: int) -> str:
    """The window around the first matching term, or the head of the field."""
    for name in SNIPPET.get(kind, ()):
        text = _text(doc.get(name))
        if text:
            return window(text, query, width)
    return ""


def window(text: str, query: Query, width: int = SNIPPET_CHARS) -> str:
    """Collapse whitespace and centre on the first hit.

    Transcript chunks are raw JSONL, so the window is frequently mid-escape.
    Turning the escapes back into spaces is not prettification - left alone a
    snippet is an unreadable wall of `\\n`.
    """
    flat = text.replace("\\n", " ").replace("\\t", " ")
    flat = re.sub(r"\s+", " ", flat).strip()
    if not flat:
        return ""

    low = flat.lower()
    idx = -1
    if query.phrase:
        idx = low.find(query.phrase)
    if idx < 0:
        for term in query.terms:
            idx = low.find(term)
            if idx >= 0:
                break
    if idx < 0:
        return flat[:width] + ("..." if len(flat) > width else "")

    start = max(0, idx - width // 3)
    end = min(len(flat), start + width)
    return ("..." if start else "") + flat[start:end] + ("..." if end < len(flat) else "")


# --------------------------------------------------------------------------- #
# the search
# --------------------------------------------------------------------------- #


class Search:
    """One search across the archive.

    Construct with a `Firestore` and call `run`. Nothing is cached between
    calls - a search is a handful of queries, and a stale cache in a recall
    hook would be worse than the round trip it saves.
    """

    def __init__(self, store: Optional[Firestore] = None) -> None:
        self.store = store or Firestore()
        self.scanned = 0
        self.warnings: List[str] = []

    # -- query plumbing --------------------------------------------------- #

    def _query(self, collection: str, **kwargs: Any) -> List[Dict[str, Any]]:
        """Run a query, degrading rather than failing when an index is missing.

        A missing composite index is a console click away, which is no help at
        all inside a hook. Dropping the ordering and scanning what comes back
        returns a worse answer instead of an error.
        """
        try:
            docs = self.store.run_query(build_query(collection, **kwargs))
        except IndexRequired as exc:
            # Dropping the ordering is what keeps a search answering at all, but
            # it silently changes what the window *means*: no longer "the newest
            # N documents" but an arbitrary N, which can miss recent work
            # entirely. Say so - a quietly bad answer is worse than a slow one.
            note = ("%s: no collection-group index on the ordering field, so results "
                    "are an ARBITRARY window rather than the most recent - recent "
                    "matches may be missing. Fix: deploy the %s fieldOverride in "
                    "firestore.indexes.json" % (collection, collection))
            if exc.url:
                note += " or visit %s" % exc.url
            self.warnings.append(note)
            fallback = dict(kwargs)
            fallback.pop("order_by", None)
            fallback.pop("where", None)
            try:
                docs = self.store.run_query(build_query(collection, **fallback))
            except Exception as inner:  # pragma: no cover - last resort
                self.warnings.append("%s: %s" % (collection, inner))
                return []
        self.scanned += len(docs)
        return docs

    def _window(self, collection: str, query: Query, opts: "Options",
                all_descendants: bool = False) -> List[Dict[str, Any]]:
        """The bounded recent window for a kind, date-filtered server-side."""
        order = ORDER[collection]
        where: List[Tuple[str, str, Any]] = []
        # Exactly one filter, and it has to be on the ordering field: a range on
        # one field plus an order by another is the composite-index case.
        if opts.since:
            where.append((order, ">=", _bound(order, opts.since, end=False)))
        elif opts.until:
            where.append((order, "<=", _bound(order, opts.until, end=True)))

        return self._query(
            collection,
            where=where or None,
            order_by=order,
            desc=True,
            limit=opts.scan_for(collection),
            all_descendants=all_descendants,
        )

    def _by_keyword(self, collection: str, query: Query, opts: "Options") -> List[Dict[str, Any]]:
        """The indexed half: exact token match against the stored `keywords`.

        This is what reaches records older than the scan window. It only ever
        matches whole tokens, so it complements the window rather than
        replacing it.
        """
        if not query.terms:
            return []
        found: List[Dict[str, Any]] = []
        for group in _chunks(query.terms, MAX_DISJUNCTS):
            found.extend(self._query(
                collection,
                where=[("keywords", "array-contains-any", group)],
                limit=opts.scan_for(collection),
            ))
        return found

    # -- kinds ------------------------------------------------------------ #

    def _collect(self, kind: str, query: Query, opts: "Options") -> List[Dict[str, Any]]:
        """Candidate documents for one kind, deduplicated by path."""
        docs: Dict[str, Dict[str, Any]] = {}

        if kind in ("context", "notes"):
            for d in self._by_keyword(kind, query, opts):
                docs[d["_path"]] = d
        for d in self._window(kind, query, opts,
                              all_descendants=kind in ("sessions", "commands", "files")):
            docs.setdefault(d["_path"], d)
        return list(docs.values())

    def _rank(self, kind: str, docs: Iterable[Dict[str, Any]], query: Query,
              opts: "Options") -> List[Hit]:
        today = dt.date.today()
        weights = WEIGHTS[kind]
        hits: List[Hit] = []

        for doc in docs:
            facts = path_facts(doc.get("_path", ""))
            project = doc.get("project") or facts.get("project", "")
            date = _doc_date(kind, doc) or facts.get("date", "")

            if opts.project and opts.project.lower() not in project.lower():
                continue
            if opts.since and date and date < opts.since:
                continue
            if opts.until and date and date > opts.until:
                continue

            value, matched = score(query, doc, weights)
            if not value:
                continue

            hits.append(Hit(
                kind=kind,
                score=value + recency_bonus(date, today),
                title=_title(kind, doc, facts),
                snippet=_snippet(doc, kind, query, opts.snippet_width),
                ref=_ref(kind, doc),
                date=date,
                project=project,
                session_id=doc.get("session_id") or facts.get("session_id", ""),
                path=doc.get("_path", ""),
                matched=matched,
                extra=_extra(kind, doc),
            ))
        return hits

    # -- deep ------------------------------------------------------------- #

    def deep(self, query: Query, sessions: List[Dict[str, Any]], opts: "Options") -> List[Hit]:
        """Grep the transcript chunks of the most recent candidate sessions.

        The only true full-text path, and the expensive one: a chunk is 700k
        characters and a long session has several. Newest first, stopping at
        both a session count and a byte budget, because either limit alone
        lets one enormous session consume the whole search.
        """
        ordered = sorted(sessions, key=lambda d: _doc_date("sessions", d) or "", reverse=True)
        hits: List[Hit] = []
        budget = opts.deep_bytes
        looked = 0

        for doc in ordered:
            if looked >= opts.deep_sessions or budget <= 0:
                break
            size = int(doc.get("transcript_bytes") or 0)
            if size and size > budget:
                self.warnings.append(
                    "deep: skipped %s (%.1f MB left in budget, needs %.1f MB)"
                    % (doc.get("session_id", "?")[:8], budget / 1e6, size / 1e6))
                continue

            facts = path_facts(doc.get("_path", ""))
            try:
                parts = self.store.list_documents("%s/parts" % doc["_path"], page_size=10)
            except Exception as exc:
                self.warnings.append("deep: %s" % exc)
                continue

            self.scanned += len(parts)
            looked += 1
            text = "".join(p.get("text") or "" for p in sorted(
                parts, key=lambda p: int(p.get("index") or 0)))
            budget -= len(text.encode("utf-8", "replace"))
            if not text:
                continue

            for snippet, value in _grep(text, query, opts):
                hits.append(Hit(
                    kind="transcript",
                    score=value + recency_bonus(_doc_date("sessions", doc)),
                    title="%s  %s" % (doc.get("project") or facts.get("project", ""),
                                      (doc.get("session_id") or facts.get("session_id", ""))[:8]),
                    snippet=snippet,
                    ref=doc.get("_path", ""),
                    date=_doc_date("sessions", doc) or facts.get("date", ""),
                    project=doc.get("project") or facts.get("project", ""),
                    session_id=doc.get("session_id") or facts.get("session_id", ""),
                    path=doc.get("_path", ""),
                    matched=[t for t in query.terms if t in snippet.lower()],
                ))
        return hits

    # -- entry point ------------------------------------------------------ #

    def run(self, query: Query, opts: Optional["Options"] = None) -> Results:
        import time

        opts = opts or Options()
        started = time.time()
        self.scanned = 0
        self.warnings = []

        hits: List[Hit] = []
        sessions: List[Dict[str, Any]] = []

        for kind in opts.kinds:
            docs = self._collect(kind, query, opts)
            if kind == "sessions":
                sessions = docs
            hits.extend(self._rank(kind, docs, query, opts))

        if opts.deep:
            if not sessions:
                sessions = self._collect("sessions", query, opts)
            hits.extend(self.deep(query, sessions, opts))

        hits.sort(key=lambda h: h.score, reverse=True)
        return Results(
            query=query,
            hits=hits[:opts.limit] if opts.limit else hits,
            scanned=self.scanned,
            warnings=self.warnings,
            elapsed=time.time() - started,
        )


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    kinds: Tuple[str, ...] = KINDS
    project: str = ""
    since: str = ""
    until: str = ""
    limit: int = 20
    scan: int = 0            # 0 = the per-kind default
    deep: bool = False
    deep_sessions: int = DEEP_SESSIONS
    deep_bytes: int = DEEP_BYTES
    snippet_width: int = SNIPPET_CHARS

    def scan_for(self, kind: str) -> int:
        return self.scan or SCAN.get(kind, 300)


def _bound(field_name: str, date: str, end: bool) -> str:
    """A date as the ordering field actually stores it.

    `date` fields hold YYYY-MM-DD; `ts` and `last_seen` hold a full ISO
    timestamp. Comparing a timestamp against a bare date still works because
    ISO-8601 sorts lexicographically, but only if the bound is widened to cover
    the whole day - otherwise `--until 2026-09-18` drops that day's records.
    """
    if field_name in ("ts", "last_seen", "first_seen"):
        return date + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
    return date


def _doc_date(kind: str, doc: Dict[str, Any]) -> str:
    for name in ("date", ORDER.get(kind, ""), "ts", "last_seen", "started"):
        if name and doc.get(name):
            return str(doc[name])[:10]
    return ""


def _title(kind: str, doc: Dict[str, Any], facts: Dict[str, str]) -> str:
    if kind == "context":
        return doc.get("title") or doc.get("url") or ""
    if kind in ("notes", "docs"):
        return doc.get("title") or doc.get("id") or doc.get("_id", "")
    if kind == "sessions":
        return "%s  %s" % (doc.get("project") or facts.get("project", ""),
                           (doc.get("session_id") or facts.get("session_id", ""))[:8])
    if kind == "commands":
        return (doc.get("command") or "").strip().splitlines()[0][:120] if doc.get("command") else ""
    if kind == "files":
        return doc.get("path") or ""
    return doc.get("_id", "")


def _ref(kind: str, doc: Dict[str, Any]) -> str:
    if kind == "context":
        return doc.get("url") or doc.get("_path", "")
    if kind == "notes":
        return doc.get("source_file") or doc.get("_path", "")
    if kind == "docs":
        return "%s/%s" % (doc.get("section", ""), doc.get("path", ""))
    if kind == "files":
        return doc.get("path") or doc.get("_path", "")
    return doc.get("_path", "")


def _extra(kind: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    if kind == "sessions":
        return {k: doc.get(k) for k in
                ("message_count", "command_count", "file_count", "failed_count",
                 "git_branch", "provider", "is_sidechain") if doc.get(k) is not None}
    if kind == "commands":
        return {k: doc.get(k) for k in ("description", "exit_status") if doc.get(k)}
    if kind == "context":
        return {k: doc.get(k) for k in ("source", "type", "mention_count") if doc.get(k)}
    if kind == "files":
        return {k: doc.get(k) for k in ("action", "bytes") if doc.get(k)}
    return {}


def _grep(text: str, query: Query, opts: Options, max_windows: int = 3) -> List[Tuple[str, float]]:
    """Matching windows inside one transcript, best first.

    Windows are spaced out rather than taken greedily: three snippets from the
    same paragraph tell you less than three from different points in a session.
    """
    low = text.lower()
    needles = [query.phrase] if query.exact else ([query.phrase] if query.phrase else []) + query.terms
    found: List[Tuple[int, float]] = []
    for needle in needles:
        if not needle:
            continue
        weight = 6.0 if needle == query.phrase else 3.0
        start = 0
        while len(found) < max_windows * 4:
            idx = low.find(needle, start)
            if idx < 0:
                break
            found.append((idx, weight))
            start = idx + max(len(needle), 1)

    if query.exact and not any(w >= 6.0 for _, w in found):
        return []

    out: List[Tuple[str, float]] = []
    used: List[int] = []
    for idx, weight in sorted(found, key=lambda p: -p[1]):
        if any(abs(idx - u) < opts.snippet_width for u in used):
            continue
        used.append(idx)
        chunk = text[max(0, idx - opts.snippet_width // 3):
                     max(0, idx - opts.snippet_width // 3) + opts.snippet_width]
        out.append((window(chunk, query, opts.snippet_width), weight))
        if len(out) >= max_windows:
            break
    return out


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render(results: Results, show_scan: bool = True) -> str:
    """Grouped by kind, newest-looking first within each group."""
    lines: List[str] = []
    q = results.query
    header = 'agentprobe search: %s' % (('"%s"' % q.raw) if q.exact else q.raw)
    if q.terms:
        header += "   [%s]" % ", ".join(q.terms)
    lines.append(header)
    if show_scan:
        lines.append("%d hit(s) from %d document(s) scanned in %.1fs"
                     % (len(results.hits), results.scanned, results.elapsed))

    for note in results.warnings:
        lines.append("  ! %s" % note)

    if not results.hits:
        lines.append("\nnothing matched.")
        return "\n".join(lines)

    order = list(KINDS) + ["transcript"]
    for kind in order:
        group = [h for h in results.hits if h.kind == kind]
        if not group:
            continue
        lines.append("")
        lines.append("%s (%d)" % (kind, len(group)))
        for hit in group:
            lines.append("  %-10s %-16s %s" % (hit.date or "-", (hit.project or "-")[:16],
                                               hit.title or hit.ref))
            if hit.snippet and hit.snippet != hit.title:
                lines.append("             %s" % hit.snippet)
            if hit.ref and hit.ref != hit.title:
                lines.append("             %s" % hit.ref)
    return "\n".join(lines)


def search(text: str, opts: Optional[Options] = None,
           store: Optional[Firestore] = None) -> Results:
    """Convenience wrapper: parse, run, return."""
    return Search(store).run(parse_query(text), opts)
