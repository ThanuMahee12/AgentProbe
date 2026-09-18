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

`install.sh` registers the server for every user automatically. To do it by hand:

```bash
claude mcp add agentprobe-memory --scope user -- /usr/local/bin/agentprobe mcp
```

For any other MCP client, the config is the same three fields:

```json
{
  "mcpServers": {
    "agentprobe-memory": {
      "type": "stdio",
      "command": "/usr/local/bin/agentprobe",
      "args": ["mcp"]
    }
  }
}
```

Client config locations differ — Claude Code uses `~/.claude.json`, others use
their own file — but the server entry does not.

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
