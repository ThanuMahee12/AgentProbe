"""Publish AgentContext's markdown content to Firestore.

The site bundles the same content as a fallback, so this is not what makes the
site work - it is what lets content change without a deploy. Edit a markdown
file, publish, and the page updates; the bundled copy stays as the offline and
outage path.

Collection name is `docs`, deliberately not `content`: `context` already exists
and holds extracted links, and two collections a letter apart is a mistake
waiting to happen in a rules file.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List

from .store import Firestore

COLLECTION = "docs"

#: Visibility is deliberately NOT `status`. `status` is a workflow state the
#: author chooses - open, implementing, done - and overloading it to also mean
#: "the world can read this" makes every workflow rename a security change.
DEFAULT_VISIBILITY = "draft"

#: The only value that reaches the public page.
PUBLIC_VISIBILITY = "published"

#: content/<folder> -> the section key the site groups by
FOLDERS = {
    "brainstorm": "brainstorms",
    "ideas": "discussions",
    "kt": "kt",
    "tech-commands": "notes",
}

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?", re.S)
HEADING = re.compile(r"^(#{2,3})\s+(.+)$", re.M)


def parse(raw: str) -> Dict[str, Any]:
    """Frontmatter values are JSON, so punctuation in a title cannot break the
    parse. An unparseable value is kept as text rather than dropped."""
    meta: Dict[str, Any] = {}
    body = raw.strip()
    m = FRONTMATTER.match(raw)
    if m:
        body = raw[m.end():].strip()
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            rest = rest.strip()
            try:
                meta[key.strip()] = json.loads(rest)
            except ValueError:
                meta[key.strip()] = rest.strip("\"'")
    return {"meta": meta, "body": body}


def _walk(root: str) -> List[str]:
    """Every .md under root, as paths relative to it."""
    out: List[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def load(directory: str) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for folder, section in FOLDERS.items():
        path = os.path.join(directory, folder)
        if not os.path.isdir(path):
            continue
        for rel in _walk(path):
            name = os.path.basename(rel)
            with open(os.path.join(path, rel), "r", encoding="utf-8") as fh:
                parsed = parse(fh.read())
            meta, body = parsed["meta"], parsed["body"]
            doc_path = rel[:-3].replace(os.sep, "/")
            segments = doc_path.split("/")
            doc_id = segments[-1]
            docs.append({
                # Section is part of the id so two sections can hold a document
                # of the same name without colliding.
                # Keyed by section + full path, so two projects can hold a
                # document of the same name without colliding.
                "_id": "%s__%s" % (section, doc_path.replace("/", "__")),
                "id": doc_id,
                "path": doc_path,
                "segments": segments,
                "parent": "/".join(segments[:-1]),
                "depth": len(segments) - 1,
                "section": section,
                "title": meta.get("title") or doc_id,
                "description": meta.get("description", ""),
                "date": meta.get("date", ""),
                "status": meta.get("status", ""),
                # Absent means draft, never public. Defaulting the other way
                # would make a file appearing in the tree enough to publish it,
                # and promotion is meant to be a decision somebody makes rather
                # than a side effect of writing a document.
                "visibility": meta.get("visibility") or DEFAULT_VISIBILITY,
                # Whether the author actually declared one. An admin promotes a
                # document in the panel, not in the markdown, so a later publish
                # must not silently unpublish it - but an explicit value in the
                # file still wins, because the file is the source of truth.
                "_declared": bool(meta.get("visibility")),
                "url": meta.get("url", ""),
                "gist": meta.get("gist", ""),
                "notion": meta.get("notion", ""),
                "project": meta.get("project") or (segments[0] if len(segments) > 1 else ""),
                "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
                # Provenance. When something published here turns out wrong, the
                # question is always which agent wrote it and how, and that has
                # to be answerable without guessing.
                "agent": meta.get("agent", ""),
                "tools": meta.get("tools") if isinstance(meta.get("tools"), list) else [],
                "model": meta.get("model", ""),
                "session": meta.get("session", ""),
                "os_user": meta.get("os_user", ""),
                "host": meta.get("host", ""),
                "body": body,
                "headings": [{"depth": len(h[0]), "text": h[1].strip()}
                             for h in HEADING.findall(body)],
                "source": "content/%s/%s" % (folder, rel.replace(os.sep, "/")),
                "bytes": len(body.encode("utf-8")),
            })
    return docs


def publish(store: Firestore, directory: str, dry_run: bool = False) -> Dict[str, Any]:
    docs = load(directory)
    stats: Dict[str, Any] = {"found": len(docs), "sections": {}, "removed": 0, "status": {}}
    for d in docs:
        stats["sections"][d["section"]] = stats["sections"].get(d["section"], 0) + 1

    if dry_run:
        return stats

    # Preserve an admin's promotion. Reading current visibility first costs one
    # query and is the difference between "publish updates the text" and
    # "publish reverts whatever anyone decided in the panel".
    current = {}
    try:
        for row in store.list_documents(COLLECTION, page_size=300):
            current[row["_id"]] = row.get("visibility")
    except Exception:
        pass  # first run, or unreadable - fall through to the declared value

    writes = []
    for d in docs:
        doc_id = d.pop("_id")
        declared = d.pop("_declared", False)
        if not declared and current.get(doc_id):
            d["visibility"] = current[doc_id]
        # Counted here, after preservation, so the report states what will
        # actually be stored. Counting the declared value told you a document
        # was a draft while leaving it public, which is the one direction this
        # report must never be wrong in.
        stats["status"][d["visibility"]] = stats["status"].get(d["visibility"], 0) + 1
        writes.append(store.write("%s/%s" % (COLLECTION, doc_id), d))
    store.commit(writes)

    # A file deleted locally must disappear from the site too, or the page keeps
    # serving something that no longer exists in the repository.
    import requests

    wanted = {"%s__%s" % (d["section"], d["path"].replace("/", "__")) for d in load(directory)}
    r = requests.get("%s/%s?pageSize=300" % (store.base, COLLECTION),
                     headers=store._headers(), timeout=60).json()
    stale = [
        doc["name"].split("/")[-1]
        for doc in r.get("documents", [])
        if doc["name"].split("/")[-1] not in wanted
    ]
    if stale:
        store.commit([
            {"delete": "%s/%s/%s" % (store.name_base, COLLECTION, s)} for s in stale
        ])
        stats["removed"] = len(stale)

    return stats
