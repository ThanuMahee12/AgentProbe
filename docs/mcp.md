# The memory MCP server

`agentprobe mcp` serves remembered facts and the session archive over MCP, so
memory belongs to *you* rather than to whichever agent happened to learn it.

## Why MCP rather than a sync

Claude Code keeps memory per project directory. A fact learned in one project is
invisible from the next, none of it survives a rebuilt machine, and no other tool
can read any of it. Gemini, OpenCode and Cursor each keep their own, in their own
format.

A file sync would have to be written once per tool and would still leave every
tool's memory shaped differently. MCP is the protocol they already share: one
server, one store, and every client that speaks it gets the same memory on every
machine.

It also beats injecting recall into prompts. A `UserPromptSubmit` hook has to
guess when history is wanted, pays a round trip on prompts that did not want it,
and pushes text into the context whether it helps or not. A tool is called only
when the model actually wants the answer.

## Tools

| Tool | Use it for |
|---|---|
| `memory_search` | find remembered facts — before asking something you may already have been told |
| `memory_write` | remember a fact; keyed by `name`, so re-writing updates in place |
| `memory_get` | read one fact in full |
| `memory_list` | list facts, newest first |
| `memory_delete` | forget a fact that turned out wrong |
| `history_search` | search past sessions, shell commands and touched files |

### Scope

Every fact carries a scope, which is what stops centralizing from turning into
one undifferentiated pile:

- `global` — true everywhere, always in play
- `user` — this account
- `project:<name>` — one project

Asking for a project returns that project's facts *plus* everything global.

### Types

`user`, `feedback`, `project`, `reference` — the same vocabulary Claude Code's
own memory files use, kept identical so an imported file does not have to be
reclassified.

## Storage

```
memory/{owner}/entries/{slug}
```

`owner` is derived from the configured email, `slug` from the fact's name — so
writing the same name twice updates rather than duplicates. Each entry records
the OS account and host that wrote it, which is the question you ask when several
accounts share a pool and a remembered fact turns out wrong.

The record is deliberately the same shape as the markdown files it replaces —
frontmatter `name` / `description` / `type` plus a body — so importing an
existing tree is a parse, not a translation, and exporting back is lossless.

## Wiring it up

`install.sh` registers the server for **every user in every client** it knows
about, automatically. Re-run it after an update and it reconciles in place.

One server, four clients, four config files and three different schemas. The
shapes below were taken from what each tool's own `mcp add` writes, not from
documentation:

| Client | File | Key |
|---|---|---|
| Claude Code | `~/.claude.json` | `mcpServers` |
| Gemini CLI | `~/.gemini/settings.json` | `mcpServers` |
| Antigravity | `~/.gemini/config/mcp_config.json` | `mcpServers` |
| OpenCode | `~/.config/opencode/opencode.json` | `mcp` |

```json
// claude
{ "mcpServers": { "agentprobe-memory": {
    "type": "stdio", "command": "/usr/local/bin/agentprobe", "args": ["mcp"] } } }

// gemini
{ "mcpServers": { "agentprobe-memory": {
    "command": "/usr/local/bin/agentprobe", "args": ["mcp"] } } }

// antigravity
{ "mcpServers": { "agentprobe-memory": {
    "command": "/usr/local/bin/agentprobe", "args": ["mcp"], "disabled": false } } }

// opencode - nests argv as one list, and calls a local server "local" not "stdio"
{ "mcp": { "agentprobe-memory": {
    "type": "local", "command": ["/usr/local/bin/agentprobe", "mcp"], "enabled": true } } }
```

The installer writes these files directly rather than shelling out to each
client's `mcp add`. Three of the four are installed inside a single user's home
and cannot be run as anybody else, so their own CLIs are not available to an
installer wiring up four accounts. Existing config is merged, never replaced —
these are the clients' own state files, one of which is 120 KB of session data —
and an unparseable one skips that client rather than risking overwriting it.

By hand, per client:

```bash
claude mcp add agentprobe-memory --scope user -- /usr/local/bin/agentprobe mcp
gemini mcp add -s user -t stdio agentprobe-memory /usr/local/bin/agentprobe mcp
agy    mcp add -t stdio agentprobe-memory /usr/local/bin/agentprobe mcp
# opencode's `mcp add` is interactive; write the file above instead
```

### Gemini shows the server as Disabled

Expected, and not a misconfiguration:

```
Warning: MCP servers are configured but disabled because this folder is untrusted.
```

Gemini suppresses MCP servers — including user-level ones — in folders it does
not trust. Registration is correct; trust the folder in Gemini to enable it.
Deciding which folders are trusted is a security choice, so the installer does
not make it for you.

## Protocol notes

Newline-delimited JSON-RPC 2.0 on stdin/stdout. **Not** LSP-style
`Content-Length` framing.

Implemented directly rather than through an SDK: the protocol is small, and this
has to run on machines where `pip install` is not a step anyone will take.

Three rules the implementation depends on:

- **stdout is the protocol.** Diagnostics go to stderr, via `log()`. Anything
  else written to stdout corrupts the stream.
- **Notifications get no reply.** A message without an `id` is answered with
  silence; replying is a protocol violation some clients treat as fatal.
- **Tool failures are results, not transport errors.** A failing tool returns
  `isError: true` with a message the model can read and act on. Only a broken
  request gets a JSON-RPC error.

Protocol versions `2024-11-05`, `2025-03-26` and `2025-06-18` are recognised; the
client's version is echoed back when known, otherwise the default is used.

## Troubleshooting

**`✘ Failed to connect`** — run the launcher by hand and read stderr:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | agentprobe mcp
```

**`no usable credential`** — the expected failure on a shared machine. The
fallback credential belongs to one user and other accounts cannot read it. Fix it
with a service account at `/etc/agentcontext/sa.json`; see `install.md`.

**Tools listed but every call fails** — the server starts before it authenticates,
deliberately. A dead tool call is recoverable; a server that refuses to start
looks like a broken install. The error text names the cause.

**`history_search` returns nothing for something you know happened** — the
collection-group indexes are probably not deployed. See `install.md`.
