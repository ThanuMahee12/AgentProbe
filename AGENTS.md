# AGENTS.md

Instructions for any coding agent working on this repository. Claude Code,
Gemini CLI, OpenCode and Cursor all read this file or a symlink to it.

## What this is

AgentProbe collects AI coding-session history and makes it searchable. One probe
per agent normalizes that tool's storage into a shared schema, pushes it to
Firestore, and serves it back over MCP so any agent on any machine can reach it.

Two halves, two repos: **AgentProbe** collects, **AgentContext** displays.

## Principles that are not up for negotiation

**No language model in the capture path.** Capture is deterministic, costs
nothing per session, adds no latency and never invents detail that was not in
the original record. If a change needs a model to decide something during
capture, it belongs somewhere else.

**Dependency footprint is a feature.** This runs inside a `SessionEnd` hook on
every machine and as an MCP server launched by clients that will never run
`pip install`. `requests` and `cryptography` are the only imports allowed, which
is why Firestore is spoken over REST rather than through `google-cloud-firestore`
and MCP is implemented directly rather than through an SDK.

**Never fail the hook.** A capture problem must not surface to the user as a
session that ended badly. Errors go to stderr, `push` still exits 0.

**Writes are idempotent.** Every write targets a deterministic path or key, so
re-running after a resumed session updates in place instead of duplicating.

## Layout

```
agentprobe/
  schema.py       the normalized Session record every probe produces
  context.py      URL extraction, classification, keyword tokenizer
  store.py        Firestore client - writes, and the read path search uses
  search.py       keyword search over the archive
  memory.py       the shared memory record
  mcp_server.py   MCP server over stdio - memory + archive tools
  probes/
    base.py       probe interface + shared identity helpers
    claude.py     Claude Code JSONL parser
install.sh        machine-global installer: /opt tree, launcher, hooks, MCP
docs/             install and MCP reference
```

## Gotchas that have already cost someone a day

**stdout belongs to the MCP protocol.** `mcp_server.py` speaks newline-delimited
JSON-RPC on stdin/stdout. Anything else printed to stdout corrupts the stream.
Every diagnostic goes through `log()`, which writes to stderr. This is the single
easiest way to break the server.

**Never reply to a JSON-RPC notification.** A message with no `id` gets no
response. Some clients treat a reply to one as fatal.

**Firestore caps a document at 1 MiB and a commit payload at 11 MiB**, and the
two limits are independent. Batching respects both — counting writes alone is not
enough for anything carrying transcript text.

**Firestore does not index for collection-group queries by default.** A
single-field index exists at collection scope automatically; ordering a
collection-group query by that field still needs an explicit `fieldOverride` with
`COLLECTION_GROUP` scope in `firestore.indexes.json`. Without it the query fails
and any fallback silently returns an arbitrary window rather than the newest one.

**`bool` is a subclass of `int` in Python.** `encode()` checks bool first
deliberately; reversing it writes `True` as `integerValue 1` and silently changes
the type in the database.

**A subagent transcript reports its parent's `sessionId`.** Sessions are keyed by
the transcript's own filename stem, not the id inside it — keying on the reported
id collapsed twelve documents onto one.

**Integers come back from the REST API as strings.** `decode()` exists for this;
without it a `message_count` sorts as text and "9" ranks above "1780".

## Conventions

Commit messages are imperative and explain *why*, not what — the diff already
says what. No `Co-Authored-By`, no "Generated with" trailers.

Comments explain reasoning that is not recoverable from the code. Do not add
comments that restate the line below them.

Python targets 3.9+: `from __future__ import annotations`, no PEP 604 unions at
runtime, because probes run on whatever interpreter a given box happens to have.

## Verifying a change

```bash
python3 -m py_compile agentprobe/*.py agentprobe/probes/*.py
agentprobe status                      # config, credential, what is pending
agentprobe push --force -v             # one real round trip
```

For the MCP server, drive it directly — it is not meant to be run interactively:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | agentprobe mcp
```

See `docs/mcp.md` for the tool surface and `docs/install.md` for setting this up
on a new machine.
