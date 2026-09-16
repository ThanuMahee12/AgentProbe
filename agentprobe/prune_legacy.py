"""Delete the legacy Firestore trees once their contents have been converted.

Two properties of Firestore make this less trivial than "drop the collection":

* There is no collection delete. Every document goes individually.
* Deleting a document does NOT delete its subcollections - they survive as
  orphans, still readable by path. So children are deleted before parents;
  otherwise the prune reports success while the data is still there.

A third property is why the walk looks the way it does: the legacy layout put
varying values on COLLECTION segments, so its intermediate documents were never
created. Listing documents in such a collection returns nothing even though
documents exist further down, and only listCollectionIds reveals them.

Nothing here decides what is safe to remove - the caller establishes that the
data exists elsewhere. This refuses outright to touch a tree it was not given.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

import requests

from .store import Firestore

#: Trees this module is willing to touch, so a typo cannot become a request to
#: delete `projects`.
ALLOWED = {
    "claude": "claude/session",
    "agentcontext": "agentcontext/sessions",
}

#: Segments that exist only as path parents in the legacy layout and therefore
#: cannot be discovered by listing documents.
IMPLICIT = ("2024", "2025", "2026", "2027")

#: Deletes are cheaper per operation than writes but commit as a transaction,
#: and 450 of them returns "Transaction too big" where 450 writes are fine.
DELETE_BATCH = 100


class LegacyPruner:
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

    def _walk(self, collection: str, out: List[str]) -> None:
        """Append every document path under `collection`, deepest first."""
        found = self._docs(collection)
        for doc in found:
            path = doc["name"].split("/documents/")[-1]
            for sub in self._cols(path):
                self._walk("%s/%s" % (path, sub), out)
            out.append(path)          # parent after its children
        if not found:
            # The documents here are implicit; descend through them by name.
            for doc_id in IMPLICIT:
                for sub in self._cols("%s/%s" % (collection, doc_id)):
                    self._walk("%s/%s/%s" % (collection, doc_id, sub), out)

    def collect(self, root: str) -> List[str]:
        if root not in ALLOWED:
            raise ValueError(
                "refusing to prune %r; allowed roots: %s" % (root, sorted(ALLOWED))
            )
        paths: List[str] = []
        seed = ALLOWED[root]
        for sub in self._cols(seed):
            self._walk("%s/%s" % (seed, sub), paths)
        return paths

    # -- run -------------------------------------------------------------- #

    def prune(self, roots: Iterable[str], dry_run: bool = False) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        for root in roots:
            paths = self.collect(root)
            stats[root] = len(paths)
            if dry_run or not paths:
                continue
            # Deletes hit "Transaction too big" well below the 450-write cap
            # that suits document writes, so they go in smaller commits.
            deletes = [{"delete": "%s/%s" % (self.store.name_base, p)} for p in paths]
            for i in range(0, len(deletes), DELETE_BATCH):
                self.store.commit(deletes[i:i + DELETE_BATCH])
        return stats
