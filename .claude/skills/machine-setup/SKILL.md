---
name: machine-setup
description: Set up or repair AgentProbe on a machine - after cloning the repo, when moving to a new box, or when something that used to work stopped. Covers the installer, the Firebase credential, MCP registration across Claude/Gemini/Antigravity/OpenCode, and the external CLIs (firebase, gh, glab). Triggers on "set this up", "new machine", "why isn't the memory working", "mcp not connected", "no credential", or a fresh clone of this repo.
---

# Setting up a machine

Run the checker first. Do not read config files by hand to work out what is
wrong — `doctor` already knows, and it prints the command that fixes each thing.

```bash
agentprobe doctor
```

It exits with the **failure count**, so you can branch on it. Warnings are things
that work but should not be relied on; failures are things that do not work.

If `agentprobe` is not on PATH yet, the repo has not been installed:

```bash
sudo ./install.sh --email you@example.com --project your-firebase-project
```

That one command installs to `/opt`, puts a launcher on PATH, writes
`/etc/agentcontext/config.json`, wires the `SessionEnd` capture hook for every
account, and registers the MCP server in all four agent clients. It is
idempotent — re-running it is the update path, not a repair of last resort.

## Work failures in this order

Each step's failure causes the ones below it, so fixing them top-down avoids
chasing symptoms.

**1. `cryptography` missing.** It signs the RS256 assertion for service-account
auth and nothing else needs it. Install it for *the interpreter the launcher
uses* (`command -v python3` in a login shell), not the one you happen to be in.
Skipping this makes a correct service account look like a bad one.

**2. No credential, or an unreadable one.** A service account at
`/etc/agentcontext/sa.json` is the only thing that works for every account. The
refresh-token fallback belongs to whoever ran `firebase login` and is mode 600
inside their home, so other accounts cannot read it — and the error says nothing
about that. The key itself must be group-readable (`640 root:<group>`), because
`600 root:root` is found by every account and opened by none.

**3. Firestore read fails.** Means the credential authenticates but cannot reach
the data. Check the project id in `/etc/agentcontext/config.json`.

**4. MCP not registered.** `sudo ./install.sh` fixes all four clients at once.
Re-run it rather than hand-editing any client's config.

**5. External CLIs unauthenticated.** `firebase login`, `gh auth login`,
`glab auth login`. These are interactive and open a browser — **ask the user to
run them**, do not try to drive them yourself. ClickUp has no CLI; it is a remote
MCP server added to an agent client.

## Verifying it actually works

A green `doctor` says it is configured. These say it runs:

```bash
agentprobe push -v                       # capture a real session
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | agentprobe mcp
```

Then confirm from the client that will use it, **as the account that will use
it**:

```bash
claude mcp list
gemini mcp list
```

## Two traps that have already produced wrong diagnoses

**Test as the right user, in the right directory.** Both of these have caused a
real misdiagnosis here:

- `sudo -u someone agentprobe ...` without a login shell gets a different `PATH`
  and a different `python3` than that user really has. Use `sudo -u someone bash
  -lc '...'`.
- Gemini checks folder trust against the **current working directory**, so
  running it from another account's folder reports the server as `Disabled`.
  That is the feature working. Check where you are standing before changing a
  security setting.

**Do not weaken folder trust to make a warning go away.** It exists so entering a
hostile repository cannot silently launch MCP servers. If a specific folder needs
trusting, that is the user's decision to make in Gemini, or `gemini --skip-trust`
for a single session.

## What needs a human

Do not attempt these; report them and stop:

- generating a Firebase service-account key (console, browser)
- `firebase login`, `gh auth login`, `glab auth login` (interactive)
- `firebase deploy` (changes deployed infrastructure)
- deciding which folders an agent should trust
