---
name: agent-memory
description: Use the shared AgentProbe memory when you need to remember something across sessions or machines, or recall what was decided or done before. Triggers on "remember this", "what did we decide", "have I done this before", "last time", "why did we", setting up a machine that should match another, or hitting a problem that feels previously solved. Also covers searching past sessions, shell commands and touched files.
---

# Shared agent memory

Memory here is not this session's, not this machine's, and not Claude's. It is a
single store served over MCP that every agent and every account on every wired
machine reads and writes. Facts written from one box are readable from the next.

## Recall before you ask

Search memory **before** asking the user something they may already have told
you, and before concluding that a problem is new.

```
memory_search(query="s3 mount", scope="global")
```

The strongest signals that you should search first:

- the user says "again", "like last time", "as usual", "the normal way"
- you are about to ask for a path, an id, a convention or a preference
- you are setting up a machine that is supposed to match another one
- an error looks like something that would have been hit before

`history_search` is the other half: it searches past sessions, shell commands and
files touched, rather than curated facts. Use it for "how did I do X" — the exact
command someone ran three weeks ago is in there, and reconstructing it from
memory is slower and less accurate than looking.

```
history_search(query="mount-s3 allow-delete", kind="commands")
```

## Write facts that will still be true

Write when you learn something that outlives the session. Skip anything
recoverable from the code, the git history or a config file — a memory that
restates the repository is noise that dilutes search.

```
memory_write(
  name="firestore-collection-group-indexes",
  description="Collection-group ordering needs an explicit fieldOverride",
  body="...",
  type="project",
  scope="project:AgentProbe",
)
```

**Worth remembering:** decisions and the reasoning behind them, conventions the
user corrected you on, non-obvious constraints, things that cost real time to
work out, where something lives when it is not where you would guess.

**Not worth remembering:** anything in the repo already, this session's state,
facts that will be stale next week, anything you have not verified.

### Getting the fields right

`name` is the identity — writing the same name again **updates in place**. Use a
stable slug, and reuse it deliberately when a fact changes rather than inventing
a near-duplicate.

`scope` decides who sees it. `global` is always in play, so use it only for
things that are true everywhere. Machine- or repo-specific facts belong in
`project:<name>` — over-scoping to global is how a shared store becomes a pile
nobody trusts.

`description` is what search ranks on most heavily. One line, specific.

`body` is markdown. For `feedback` and `project` facts, follow with **Why:** and
**How to apply:** lines — a fact without its reasoning gets misapplied later.
Link related facts with `[[their-name]]`.

Convert relative dates to absolute ones. "Last Tuesday" is meaningless to the
session that reads it in March.

## Correcting and forgetting

A fact that turns out wrong is worse than no fact, because it is trusted. When
you find one, fix it: `memory_write` with the same `name` to correct it, or
`memory_delete` to drop it.

Facts record the account and host that wrote them, so a surprising one can be
traced rather than merely doubted.

## What memory is not

It is not a log — sessions are captured automatically and searchable through
`history_search`, so do not write session summaries into memory.

It is not a place for secrets. Keys, tokens and passwords never go in, whatever
the scope.

It is not authoritative about the present. A fact records what was true when it
was written; if one names a file, a flag or a command, verify it still exists
before acting on it.
