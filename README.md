# AgentProbe

Session-history collectors for AI coding agents.

Development is spread across a growing set of agents — Claude Code, Antigravity,
Gemini, Copilot, Cursor — and each stores sessions differently. Claude Code writes
JSONL transcripts and fires lifecycle hooks. Cursor keeps conversations in SQLite
inside workspace storage. Copilot and Gemini bury logs in extension directories.
It is all local, none of it is searchable, and most of it is rotated away or lost
when a machine is rebuilt.

AgentProbe attaches one probe per agent. Each probe understands a single tool's
storage format, extracts completed sessions, and normalizes them into a shared
schema: the full transcript, every command run, every file read or edited, plus
timing, project and user metadata. The result is pushed to Firestore, where it
becomes permanent and queryable.

The pipeline is **deterministic by design** — no language model anywhere in the
collection path. Capture costs nothing per session, adds no latency, and never
invents detail that was not in the original record.

AgentProbe is the collection half of a pair. **AgentContext** is the dashboard
that reads what it writes, rendering your work day by day with search, filters,
and selective public sharing.

---

## Probes

| Provider    | Source                            | Method          | Status |
|-------------|-----------------------------------|-----------------|--------|
| Claude Code | `~/.claude/projects/*/*.jsonl`     | SessionEnd hook | ✅ working |
| Antigravity | workspace storage                  | poll + diff     | planned |
| Gemini CLI  | `~/.gemini/tmp/`                   | poll + diff     | planned |
| Copilot     | VS Code extension storage          | poll + diff     | planned |
| Cursor      | `workspaceStorage/*.vscdb`         | SQLite read     | planned |

Only Claude Code exposes real lifecycle hooks. The rest are poll-and-diff, which
is why the collector needs a watcher daemon rather than just a hook script.

> Paths for the planned probes are best-effort and **not yet verified against real
> installs**. Do not trust that column until the probe exists.

## What gets extracted

Purely structural — read straight out of each tool's own records:

- **Sessions** — id, provider, project, cwd, git branch, agent version, start/end,
  message count, and the full transcript (chunked, since Firestore caps a document
  at 1 MiB)
- **Commands** — every shell invocation with its description, correlated to whether
  the result came back an error
- **Files** — every path read, written or edited
- **Context items** — every URL pasted into any chat, classified by source
  (ClickUp ticket, Google Sheet, Slack thread, GitHub/GitLab repo), deduplicated
  across sessions, and tokenized into keywords for search

## Layout

```
agentprobe/
├── schema.py        the normalized Session record every probe produces
├── context.py       URL extraction, classification, keyword tokenizer
└── probes/
    ├── base.py      probe interface + shared identity helpers
    └── claude.py    Claude Code JSONL parser
```

## Firestore

```
projects/{project}/days/{YYYYMMDD}/sessions/{session_id}
  └─ parts/{n}       transcript chunks
context/{id}         links, tickets, sheets, notes — flat, queried by field
```

Hierarchy is project → day → session, with every filterable value **also** stored
as a plain field. Firestore cannot filter a collection-group query on ancestor
path segments, so the duplication is what makes the dashboard's filters work.

Search is `array-contains-any` over `keywords`, built at write time. Firestore has
no full-text search and this repo does not pretend otherwise — no Algolia or
Typesense until keyword matching demonstrably stops being enough.

## Design rules

1. **No LLM in the collection path.** Everything captured is parsed, not inferred.
2. **Credentials are inventory, never values.** Presence, fingerprint, location and
   what a key unlocks are safe to store and display. The secret itself never enters
   Firestore or a browser.
3. **Config is global, credentials are per-user.** SSH refuses a key readable by
   group or others, so keys cannot be shared from a common path — by design.
