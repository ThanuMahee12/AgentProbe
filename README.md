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

## Memory over MCP

Capture is only half of it. `agentprobe mcp` serves the archive back as an MCP
server, so remembered facts and past sessions are reachable from any agent that
speaks the protocol — Claude Code, Gemini CLI, OpenCode — on any wired machine.

```
memory_search / memory_write / memory_get / memory_list / memory_delete
history_search      past sessions, shell commands, touched files
```

Memory is otherwise per-tool and per-project-directory: a fact learned in one
project is invisible from the next, none of it survives a rebuilt machine, and no
other tool can read it. One server and one store fixes all three at once.

See [docs/mcp.md](docs/mcp.md).

## Firestore

```
projects/{project}/days/{YYYYMMDD}/sessions/{session_id}
  └─ parts/{n}       transcript chunks
context/{id}         links, tickets, sheets, notes — flat, queried by field
memory/{owner}/entries/{slug}    remembered facts
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

## Checking a machine

```bash
agentprobe doctor
```

Reports what is set up and what is not — runtime, credential, Firestore
reachability, MCP registration across all four clients, and whether `firebase`,
`gh` and `glab` are actually authenticated rather than merely installed. Every
failing line carries the command that fixes it, and the exit status is the
failure count.

## Documentation

| Document | For |
|---|---|
| [docs/install.md](docs/install.md) | setting this up on a new machine, and the credential step people get wrong |
| [docs/mcp.md](docs/mcp.md) | the MCP server: tools, storage, protocol, troubleshooting |
| [.claude/skills/](.claude/skills) | `machine-setup` and `agent-memory` — skills an agent loads to set this up or use the memory |
| [AGENTS.md](AGENTS.md) | working *on* this repo — principles, layout, and the gotchas that have cost someone a day |
