# Setting up on a new machine

The whole point of this document: the machine you are standing at should end up
identical to every other one, by running the same commands in the same order.

Five steps. Two of them need something only you can supply.

```bash
git clone https://github.com/<you>/AgentProbe && cd AgentProbe
sudo ./install.sh --email you@example.com --project your-firebase-project
agentprobe doctor
```

`doctor` reports what is set up, what is not, and the command that fixes each
thing. It exits with the **failure count**, so a script or an agent can branch on
it without parsing output. Work through what it says until it reports zero
failures; everything below is the detail behind those lines.

On a machine with several accounts, check them all at once:

```bash
sudo /usr/local/bin/agentprobe doctor --all-users
```

It runs the check as each account through a **login shell**, which matters:
`sudo -u someone agentprobe ...` keeps the caller's environment and so resolves a
different `PATH` and a different `python3` than that account really has. Checking
by hand that way has already produced two wrong diagnoses — a missing launcher
and a missing module, for an account that had neither problem.

Use the absolute path under `sudo`: `secure_path` in `/etc/sudoers` usually does
not include `/usr/local/bin`, so a bare `sudo agentprobe` reports command not
found.

An agent can drive this whole process — the repo ships a `machine-setup` skill
that knows the order to fix things in and which steps need a human.

## What has to be on the machine

| | Needed for | Notes |
|---|---|---|
| python 3.9+ | everything | the interpreter the launcher resolves, not necessarily yours |
| `requests` | all Firestore traffic | |
| `cryptography` | **service-account auth only** | easy to miss: without it the fallback still works, and installing a service account then breaks |
| `firebase` CLI | deploying rules and indexes | `npm i -g firebase-tools` |
| `gh` / `glab` | repository work | optional; `doctor` reports them |
| a Firebase project | the archive | |

ClickUp has no CLI. It is a remote MCP server added to an agent client, not
something installed on the machine.

Agent clients are optional and detected rather than required — Claude Code,
Gemini CLI, Antigravity and OpenCode each get the MCP server registered if their
config location exists.

---

## What `install.sh` does

Mirrors how a dotfiles repo is deployed: one root-owned tree, a launcher on
PATH, global config in `/etc`, every user wired up — rather than a per-user copy
that drifts.

| Path | What |
|---|---|
| `/opt/agentprobe` | the code, root-owned, mode 644/755 |
| `/usr/local/bin/agentprobe` | launcher |
| `/etc/agentcontext/config.json` | project id, email, enabled flag (mode 644) |
| `/etc/agentcontext/sa.json` | service-account key (mode **600**) |
| `~/.claude/settings.json` | `SessionEnd` hook, per user |
| `~/.claude.json` | MCP registration — Claude Code, per user |
| `~/.gemini/settings.json` | MCP registration — Gemini CLI, per user |
| `~/.gemini/config/mcp_config.json` | MCP registration — Antigravity, per user |
| `~/.config/opencode/opencode.json` | MCP registration — OpenCode, per user |
| `~/.local/state/agentprobe` | per-user record of what has been pushed |

It discovers every account with a real login shell and a home directory, and
wires each one. Service accounts without a shell never run an agent, so wiring
them would only create dead config.

**It is idempotent.** Re-run it after every `git pull`. Existing entries are
replaced rather than stacked, and an existing `config.json` is kept.

### Flags

```
--users a,b     wire only these accounts
--no-hooks      install the code, skip settings.json
--no-mcp        install the code, skip MCP registration (all clients)
--project ID    firebase project id      (default: the built-in one)
--email ADDR    the address stamped on captured sessions
--uninstall     remove hooks and MCP registration; leaves code and data
```

---

## The credential, and why it is the step people get wrong

Two mechanisms work. Only one of them works *for everybody*.

**Service account — use this.** A Firebase key at `/etc/agentcontext/sa.json`.
Root-owned, mode 600, readable by whichever accounts run probes. Not tied to any
person's login, which is what makes it correct for hooks and MCP servers running
as several different accounts.

Firebase console → Project settings → Service accounts → Generate new private key.

The key must be readable by **every account that runs an agent**, which mode 600
root-owned is not — those accounts will find the file and fail to open it. Give it
a group instead:

```bash
sudo groupadd -f agentprobe
sudo usermod -aG agentprobe someuser          # once per account
sudo install -m 640 -o root -g agentprobe ~/Downloads/key.json /etc/agentcontext/sa.json
```

`640 root:agentprobe` keeps the private key off world-readable disk while still
letting the accounts that need it read it. `agentprobe doctor` checks that it can
actually be opened, not merely that it exists.

**Service-account auth also needs `cryptography`**, which signs the RS256
assertion — and only that. A machine where one account has it and the others do
not keeps working on the refresh-token fallback and then breaks the moment the
service account is installed, which looks like a bad key rather than a missing
module:

```bash
sudo /usr/bin/python3 -m pip install cryptography    # the interpreter the launcher uses
```

**Refresh token — the fallback, and a trap on a shared machine.** If no service
account is present, the code falls back to the credential `firebase login` leaves
in `~/.config/configstore/firebase-tools.json`. It works instantly for the person
who ran that login, and **not at all for anyone else**: the file is mode 600
inside a mode 700 home directory, so other accounts cannot read it. On a machine
with four users, two of them will see

```
no usable credential for you@example.com: no credential found ...
```

and the fix is always the service account. The CLI also warns that the refresh
token mechanism is deprecated.

Same key is what CI needs as its `FIREBASE_SERVICE_ACCOUNT` secret.

---

## Verifying

```bash
agentprobe status
```

Reports the config file, project, credential *kind* and path, and how many
sessions are discovered versus already pushed. `credential  NONE FOUND` is the
one line that means nothing will work.

```bash
agentprobe push -v          # push what changed
agentprobe push --force -v  # push everything, ignoring state
```

Check MCP, per user:

```bash
claude mcp list | grep agentprobe-memory      # expect: ✔ Connected
```

Or drive the server directly, which is also how you check it as another account:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | sudo -u someuser agentprobe mcp
```

---

## Firestore setup

The database needs indexes the dashboard repo carries in
`firestore.indexes.json`:

```bash
firebase deploy --only firestore:indexes
firebase deploy --only firestore:rules
```

Collection-group ordering will not work until the indexes are deployed. Search
degrades to an unordered window and says so in its warnings rather than failing,
which is easy to miss — if results seem to be missing recent work, check this
first.

---

## Updating

```bash
cd AgentProbe && git pull && sudo ./install.sh
```

Re-running the installer is the update path. It replaces `/opt/agentprobe`
wholesale, refreshes the launcher, and updates every user's hook and MCP entry
in place.

## Removing

```bash
sudo ./install.sh --uninstall
```

Removes the `SessionEnd` hook and the MCP registration from every user. The code
in `/opt`, the config in `/etc` and everything already in Firestore are left
alone — uninstalling should never be how you lose your history.
