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
                "url": meta.get("url", ""),
                "gist": meta.get("gist", ""),
                "notion": meta.get("notion", ""),
                "project": meta.get("project") or (segments[0] if len(segments) > 1 else ""),
                "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
                "body": body,
                "headings": [{"depth": len(h[0]), "text": h[1].strip()}
                             for h in HEADING.findall(body)],
                "source": "content/%s/%s" % (folder, rel.replace(os.sep, "/")),
                "bytes": len(body.encode("utf-8")),
            })
    return docs


def publish(store: Firestore, directory: str, dry_run: bool = False) -> Dict[str, Any]:
    docs = load(directory)
    stats: Dict[str, Any] = {"found": len(docs), "sections": {}, "removed": 0}
    for d in docs:
        stats["sections"][d["section"]] = stats["sections"].get(d["section"], 0) + 1

    if dry_run:
        return stats

    writes = [store.write("%s/%s" % (COLLECTION, d.pop("_id")), d) for d in docs]
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
