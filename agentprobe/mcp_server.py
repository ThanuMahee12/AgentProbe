"""An MCP server over the archive, so memory is not Claude Code's alone.

MCP is the reason this exists rather than another bespoke sync. Every agent that
speaks it - Claude Code, Gemini CLI, OpenCode - can reach the same memory through
one protocol, on any machine, which is what "centralized" has to mean to be worth
doing. A file sync would have to be written once per tool and would still leave
each tool's memory shaped differently.

It also replaces the prompt-injection approach to recall. A `UserPromptSubmit`
hook has to guess when history is wanted, pays a round trip on prompts that did
not want it, and pushes text into the context whether or not it helps. A tool is
called only when the model actually wants the answer.

**stdio is the protocol channel.** Messages are newline-delimited JSON-RPC 2.0 on
stdin/stdout - no Content-Length framing. Anything else printed to stdout
corrupts the stream, so every diagnostic goes to stderr. That is the single
easiest way to break an MCP server and the reason `log()` exists.

No SDK: the protocol is small enough to implement directly, and this has to run
from a launcher on machines where `pip install` is not a step anyone will take.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .memory import (
    SCOPE_GLOBAL,
    TYPES,
    Memory,
    owner_slug,
    scope_matches,
    slugify,
)

#: Versions we know how to speak. The client's is echoed back when recognised,
#: otherwise the newest here - an unknown version is better answered with
#: something concrete than with a negotiation failure.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2024-11-05"

SERVER_NAME = "agentprobe-memory"
SERVER_VERSION = "0.1.0"


def log(*parts: Any) -> None:
    """Diagnostics. stderr only - stdout belongs to the protocol."""
    print(*parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #


class MemoryStore:
    """Memory entries in Firestore, under the path its rules already reserve.

    `match /memory/{userId}/{document=**}` exists in firestore.rules and the
    collection is empty - the path was anticipated and never built. Entries live
    at `memory/{owner}/entries/{slug}`, keyed by name so writing the same fact
    twice updates rather than duplicates.
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config.load()
        self._store = None

    @property
    def store(self):
        # Lazy so the server starts, and reports tools, even when the credential
        # is missing or the network is down. A dead tool call is recoverable; a
        # server that refuses to start looks like a broken install.
        if self._store is None:
            from .store import Firestore

            self._store = Firestore()
            self._store.timeout = 20
        return self._store

    @property
    def owner(self) -> str:
        return owner_slug(self.config.user_email or "unknown")

    def path(self, doc_id: str) -> str:
        return "memory/%s/entries/%s" % (self.owner, doc_id)

    def collection(self) -> str:
        return "memory/%s/entries" % self.owner

    # -- writes ----------------------------------------------------------- #

    def write(self, entry: Memory) -> Dict[str, Any]:
        existing = self.get(entry.name)
        if existing and existing.get("created"):
            # Preserve first-seen. An update is not a new fact.
            entry.created = existing["created"]
        self.store.commit([self.store.write(self.path(entry.doc_id), entry.to_dict())])
        return {"name": entry.name, "doc_id": entry.doc_id, "updated": entry.updated,
                "created": entry.created, "replaced": bool(existing)}

    def delete(self, name: str) -> bool:
        doc_id = slugify(name)
        if not self.get(name):
            return False
        self.store.commit([{"delete": "%s/%s" % (self.store.name_base, self.path(doc_id))}])
        return True

    # -- reads ------------------------------------------------------------ #

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        from .store import decode_fields

        raw = self.store.get(self.path(slugify(name)))
        if not raw:
            return None
        doc = decode_fields(raw.get("fields"))
        doc["_path"] = self.path(slugify(name))
        return doc

    def list(self, scope: str = "", type_: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        docs = self.store.list_documents(self.collection(), page_size=100,
                                         max_documents=max(limit, 1) * 3)
        out = [d for d in docs
               if scope_matches(d.get("scope", SCOPE_GLOBAL), scope)
               and (not type_ or d.get("type") == type_)]
        out.sort(key=lambda d: d.get("updated") or "", reverse=True)
        return out[:limit]

    def search(self, query: str, scope: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """Rank by term overlap across name, description and body.

        The whole collection is listed rather than filtered server-side: memory
        is tens of entries, not thousands, and scanning it is cheaper than the
        composite index a filtered query would need.
        """
        from .search import Query, parse_query, score, window

        q: Query = parse_query(query)
        hits = []
        for doc in self.store.list_documents(self.collection(), page_size=100):
            if not scope_matches(doc.get("scope", SCOPE_GLOBAL), scope):
                continue
            value, matched = score(q, doc, (("name", 3.0), ("description", 2.5),
                                            ("keywords", 2.0), ("tags", 2.0), ("body", 1.0)))
            if not value:
                continue
            doc["_score"] = round(value, 2)
            doc["_matched"] = matched
            doc["_snippet"] = window(doc.get("body") or "", q, 240)
            hits.append(doc)
        hits.sort(key=lambda d: d["_score"], reverse=True)
        return hits[:limit]


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def _text(payload: Any) -> Dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": body}], "isError": False}


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class Tools:
    """The tool surface. One method per tool, registered in `SPEC`."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config.load()
        self.memory = MemoryStore(self.config)

    # -- memory ----------------------------------------------------------- #

    def memory_write(self, name: str, body: str, description: str = "",
                     type: str = "project", scope: str = SCOPE_GLOBAL,
                     project: str = "", tags: Optional[List[str]] = None) -> Dict[str, Any]:
        if not name or not body:
            return _error("both `name` and `body` are required")
        entry = Memory(
            name=name, body=body, description=description, type=type,
            scope=scope or SCOPE_GLOBAL, project=project, tags=list(tags or []),
            user_email=self.config.user_email, origin_agent="mcp",
        )
        return _text(self.memory.write(entry))

    def memory_search(self, query: str, scope: str = "", limit: int = 10) -> Dict[str, Any]:
        if not query:
            return _error("`query` is required")
        hits = self.memory.search(query, scope=scope, limit=int(limit or 10))
        if not hits:
            return _text("no memory matched %r" % query)
        return _text([{
            "name": h.get("name"), "description": h.get("description"),
            "type": h.get("type"), "scope": h.get("scope"),
            "score": h.get("_score"), "snippet": h.get("_snippet"),
            "updated": h.get("updated"),
        } for h in hits])

    def memory_get(self, name: str) -> Dict[str, Any]:
        doc = self.memory.get(name)
        if not doc:
            return _error("no memory named %r" % name)
        return _text({k: v for k, v in doc.items() if not k.startswith("_")})

    def memory_list(self, scope: str = "", type: str = "", limit: int = 50) -> Dict[str, Any]:
        docs = self.memory.list(scope=scope, type_=type, limit=int(limit or 50))
        if not docs:
            return _text("no memory stored yet")
        return _text([{"name": d.get("name"), "description": d.get("description"),
                       "type": d.get("type"), "scope": d.get("scope"),
                       "updated": d.get("updated")} for d in docs])

    def memory_delete(self, name: str) -> Dict[str, Any]:
        return _text({"deleted": self.memory.delete(name), "name": name})

    # -- archive ---------------------------------------------------------- #

    def history_search(self, query: str, kind: str = "", since: str = "",
                       project: str = "", limit: int = 15) -> Dict[str, Any]:
        """Search past sessions, commands and files - not memory."""
        if not query:
            return _error("`query` is required")
        from .search import KINDS, Options, Search, parse_query

        kinds = tuple(k.strip() for k in kind.split(",") if k.strip() in KINDS) or KINDS
        opts = Options(kinds=kinds, since=since, project=project, limit=int(limit or 15))
        results = Search(self.memory.store).run(parse_query(query), opts)
        if not results.hits:
            return _text("nothing in the archive matched %r" % query)
        return _text([{
            "kind": h.kind, "date": h.date, "project": h.project,
            "title": h.title, "snippet": h.snippet, "ref": h.ref,
        } for h in results.hits])


#: name -> (description, JSON Schema for arguments)
SPEC: Dict[str, Dict[str, Any]] = {
    "memory_search": {
        "description": "Search remembered facts by keyword. Use this before asking the "
                       "user something they may have already told you.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to search for"},
                "scope": {"type": "string", "description": "global, user, or project:<name>"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    "memory_write": {
        "description": "Remember a fact so any agent on any machine can find it later. "
                       "Keyed by name - writing the same name updates in place.",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short slug-like identifier"},
                "body": {"type": "string", "description": "the fact, as markdown"},
                "description": {"type": "string", "description": "one-line summary"},
                "type": {"type": "string", "enum": list(TYPES), "default": "project"},
                "scope": {"type": "string", "default": SCOPE_GLOBAL,
                          "description": "global, user, or project:<name>"},
                "project": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "body"],
        },
    },
    "memory_get": {
        "description": "Read one remembered fact in full, by name.",
        "schema": {"type": "object",
                   "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    "memory_list": {
        "description": "List remembered facts, newest first.",
        "schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "type": {"type": "string", "enum": list(TYPES)},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    "memory_delete": {
        "description": "Forget a fact by name. Use when a remembered fact turned out wrong.",
        "schema": {"type": "object",
                   "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    "history_search": {
        "description": "Search past agent sessions, shell commands and touched files. "
                       "Use for 'how did I do X before' questions.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string",
                         "description": "comma separated: sessions, commands, files, context, notes, docs"},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
    },
}


# --------------------------------------------------------------------------- #
# the JSON-RPC loop
# --------------------------------------------------------------------------- #


class Server:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.tools = Tools(config)
        self.protocol = DEFAULT_PROTOCOL

    # -- dispatch --------------------------------------------------------- #

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method") or ""
        msg_id = msg.get("id")

        # A notification has no id and must never be answered - replying to one
        # is a protocol violation some clients treat as fatal.
        if msg_id is None:
            return None

        try:
            if method == "initialize":
                return self._ok(msg_id, self._initialize(msg.get("params") or {}))
            if method == "tools/list":
                return self._ok(msg_id, {"tools": self._tool_list()})
            if method == "tools/call":
                return self._ok(msg_id, self._call(msg.get("params") or {}))
            if method == "ping":
                return self._ok(msg_id, {})
            if method in ("resources/list", "prompts/list"):
                # Declared unsupported, but answered rather than errored: clients
                # probe for these and a JSON-RPC error reads as a broken server.
                return self._ok(msg_id, {"resources": [], "prompts": []})
            return self._err(msg_id, -32601, "unknown method: %s" % method)
        except Exception as exc:
            log("agentprobe-mcp: %s\n%s" % (exc, traceback.format_exc()))
            return self._err(msg_id, -32603, str(exc))

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asked = params.get("protocolVersion") or ""
        self.protocol = asked if asked in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    @staticmethod
    def _tool_list() -> List[Dict[str, Any]]:
        return [{"name": name, "description": spec["description"],
                 "inputSchema": spec["schema"]} for name, spec in SPEC.items()]

    def _call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if name not in SPEC:
            return _error("unknown tool: %s" % name)
        fn: Callable[..., Dict[str, Any]] = getattr(self.tools, name)
        try:
            return fn(**args)
        except TypeError as exc:
            return _error("bad arguments for %s: %s" % (name, exc))
        except Exception as exc:
            # Tool failures are results, not transport errors - the model can
            # read the message and try something else.
            log("agentprobe-mcp: %s failed: %s" % (name, traceback.format_exc()))
            return _error("%s failed: %s" % (name, exc))

    # -- framing ---------------------------------------------------------- #

    @staticmethod
    def _ok(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        log("%s %s ready" % (SERVER_NAME, SERVER_VERSION))

        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log("agentprobe-mcp: dropped unparseable line")
                continue

            # A batch is a JSON array; each member is handled independently.
            batch = msg if isinstance(msg, list) else [msg]
            replies = [r for r in (self.handle(m) for m in batch if isinstance(m, dict)) if r]
            for reply in replies:
                stdout.write(json.dumps(reply) + "\n")
            if replies:
                stdout.flush()
        return 0


def main(config: Optional[Config] = None) -> int:
    return Server(config).serve()
