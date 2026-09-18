# Setting up on a new machine

The whole point of this document: the machine you are standing at should end up
identical to every other one, by running the same commands in the same order.

Five steps. Two of them need something only you can supply.

```bash
git clone https://github.com/<you>/AgentProbe && cd AgentProbe
sudo ./install.sh --email you@example.com --project your-firebase-project
sudo install -m 600 /path/to/service-account.json /etc/agentcontext/sa.json
agentprobe status
claude mcp list | grep agentprobe-memory
```

If `status` reports a credential and `mcp list` says connected, the machine is
done. Everything below explains what those commands did and what to do when one
of them does not.

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

```bash
sudo install -m 600 -o root -g root ~/Downloads/key.json /etc/agentcontext/sa.json
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
