#!/usr/bin/env bash
# Install AgentProbe machine-globally.
#
# Mirrors how shellrc is deployed on this box: one root-owned tree in /opt, a
# launcher on PATH, global config in /etc, and every user wired up - rather
# than a per-user copy that drifts.
#
#   sudo ./install.sh                 install / update, wire every human user
#   sudo ./install.sh --users a,b     wire only these users
#   sudo ./install.sh --no-hooks      install the code, skip settings.json
#   sudo ./install.sh --no-mcp        skip registering the MCP server
#   sudo ./install.sh --uninstall     remove hooks and MCP entry (leaves code and data)
#
# Idempotent: safe to re-run after a git pull.

set -euo pipefail

PREFIX=/opt/agentprobe
BIN=/usr/local/bin/agentprobe
CONFIG_DIR=/etc/agentcontext
CONFIG=$CONFIG_DIR/config.json
SRC="$(cd "$(dirname "$0")" && pwd)"

PROJECT_ID="${AGENTPROBE_PROJECT:-agentcontext-sessions}"
USER_EMAIL="${AGENTPROBE_EMAIL:-}"
DO_HOOKS=1
DO_MCP=1
UNINSTALL=0
USERS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --users)     USERS="$2"; shift 2 ;;
        --no-hooks)  DO_HOOKS=0; shift ;;
        --no-mcp)    DO_MCP=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)
            # The flags are documented in the header comment; print it rather
            # than keeping a second copy that drifts out of step with it.
            sed -n '2,/^$/p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
            exit 0 ;;
        --project)   PROJECT_ID="$2"; shift 2 ;;
        --email)     USER_EMAIL="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "install.sh must run as root" >&2; exit 1; }

# Users with a real login shell and a home directory. Service accounts without
# one never run an agent, so wiring them only creates dead config.
discover_users() {
    getent passwd | awk -F: '$7 !~ /(nologin|false|sync)$/ && $6 ~ /^(\/root|\/home\/)/ {print $1}'
}

[ -n "$USERS" ] && USER_LIST=$(echo "$USERS" | tr ',' ' ') || USER_LIST=$(discover_users)

# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

apply_hook() {
    local user="$1" action="$2" home
    home=$(getent passwd "$user" | cut -d: -f6)
    [ -d "$home" ] || return 0

    mkdir -p "$home/.claude"
    HOOK_USER="$user" HOOK_HOME="$home" HOOK_ACTION="$action" HOOK_BIN="$BIN" python3 - <<'PY'
import json, os

home   = os.environ["HOOK_HOME"]
action = os.environ["HOOK_ACTION"]
binary = os.environ["HOOK_BIN"]
path   = os.path.join(home, ".claude", "settings.json")

try:
    with open(path) as fh:
        settings = json.load(fh)
except (FileNotFoundError, ValueError):
    settings = {}

hooks = settings.setdefault("hooks", {})
entries = hooks.get("SessionEnd") or []

command = "%s push --quiet" % binary

# Drop any agentprobe entry we previously wrote, so re-running updates in place
# instead of stacking duplicates. Other tools' hooks are left alone.
def ours(group):
    return any("agentprobe" in (h.get("command") or "") for h in group.get("hooks", []))

entries = [g for g in entries if not ours(g)]

if action == "install":
    entries.append({"hooks": [{"type": "command", "command": command}]})

if entries:
    hooks["SessionEnd"] = entries
else:
    hooks.pop("SessionEnd", None)
if not hooks:
    settings.pop("hooks", None)

tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("  %-12s %s" % (os.environ["HOOK_USER"], action + "ed"))
PY

    chown -R "$user":"$(id -gn "$user")" "$home/.claude" 2>/dev/null || true
}

apply_mcp() {
    local user="$1" action="$2" home
    home=$(getent passwd "$user" | cut -d: -f6)
    [ -d "$home" ] || return 0

    # One server, four clients, four different config files and three different
    # schemas. Each client's own `mcp add` would be the obvious way, but three of
    # them are installed in a single user's home and cannot be run as anyone
    # else - so the files are written directly. The shapes below were taken from
    # what each tool's own CLI writes, not from documentation.
    HOOK_USER="$user" HOOK_HOME="$home" HOOK_ACTION="$action" HOOK_BIN="$BIN" python3 - <<'PYMCP'
import json, os, sys

home   = os.environ["HOOK_HOME"]
action = os.environ["HOOK_ACTION"]
binary = os.environ["HOOK_BIN"]
user   = os.environ["HOOK_USER"]
NAME   = "agentprobe-memory"

# (label, path, key, entry)
CLIENTS = [
    ("claude",      ".claude.json", "mcpServers",
     {"type": "stdio", "command": binary, "args": ["mcp"], "env": {}}),
    ("gemini",      ".gemini/settings.json", "mcpServers",
     {"command": binary, "args": ["mcp"]}),
    ("antigravity", ".gemini/config/mcp_config.json", "mcpServers",
     {"command": binary, "args": ["mcp"], "disabled": False}),
    # opencode nests the command as a single argv list and calls a local server
    # "local" rather than "stdio".
    ("opencode",    ".config/opencode/opencode.json", "mcp",
     {"type": "local", "command": [binary, "mcp"], "enabled": True}),
]

done = []
for label, rel, key, entry in CLIENTS:
    path = os.path.join(home, rel)
    if os.path.exists(path):
        try:
            with open(path) as fh:
                config = json.load(fh)
        except ValueError:
            # Corrupt or half-written. These are the clients' own state files -
            # one of them is 120 KB of session data - so leave it entirely alone
            # rather than risk replacing it with our two keys.
            done.append("%s:SKIPPED" % label)
            continue
        if not isinstance(config, dict):
            done.append("%s:SKIPPED" % label)
            continue
    elif action != "install":
        continue
    else:
        config = {}

    servers = config.get(key)
    if not isinstance(servers, dict):
        servers = {}
    before = NAME in servers

    if action == "install":
        servers[NAME] = entry
        config[key] = servers
        done.append("%s:%s" % (label, "updated" if before else "added"))
    else:
        servers.pop(NAME, None)
        if servers:
            config[key] = servers
        else:
            config.pop(key, None)
        if not before:
            continue
        done.append("%s:removed" % label)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)

print("  %-12s %s" % (user, ", ".join(done) if done else "nothing to do"))
PYMCP

    # Every path we may have created, so a root-run installer does not leave
    # root-owned config in someone else's home.
    for d in .claude.json .gemini .config/opencode; do
        [ -e "$home/$d" ] && chown -R "$user":"$(id -gn "$user")" "$home/$d" 2>/dev/null
    done
    return 0
}

if [ "$UNINSTALL" = "1" ]; then
    echo "Removing AgentProbe hooks:"
    for u in $USER_LIST; do apply_hook "$u" uninstall; done
    echo "Removing MCP server registration:"
    for u in $USER_LIST; do apply_mcp "$u" uninstall; done
    echo "Done. Code at $PREFIX and data in Firestore are untouched."
    exit 0
fi

# --------------------------------------------------------------------------
# code
# --------------------------------------------------------------------------

echo "Installing AgentProbe -> $PREFIX"
mkdir -p "$PREFIX"
rm -rf "$PREFIX/agentprobe"
cp -r "$SRC/agentprobe" "$PREFIX/agentprobe"
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chown -R root:root "$PREFIX"
find "$PREFIX" -type d -exec chmod 755 {} +
find "$PREFIX" -type f -exec chmod 644 {} +

cat > "$BIN" <<EOF
#!/usr/bin/env bash
# AgentProbe launcher. Real code lives in $PREFIX (root-owned, updated by
# re-running install.sh after a git pull).
exec python3 -c 'import sys; sys.path.insert(0, "$PREFIX"); from agentprobe.cli import main; sys.exit(main())' "\$@"
EOF
chmod 755 "$BIN"

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

mkdir -p "$CONFIG_DIR"
chmod 755 "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
    PROJECT_ID="$PROJECT_ID" USER_EMAIL="$USER_EMAIL" python3 - <<'PY'
import json, os
cfg = {"project_id": os.environ["PROJECT_ID"], "enabled": True}
if os.environ.get("USER_EMAIL"):
    cfg["user_email"] = os.environ["USER_EMAIL"]
with open("/etc/agentcontext/config.json", "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
    chmod 644 "$CONFIG"
    echo "Wrote $CONFIG"
else
    echo "Kept existing $CONFIG"
fi

# --------------------------------------------------------------------------

if [ "$DO_HOOKS" = "1" ]; then
    echo "Wiring SessionEnd hooks:"
    for u in $USER_LIST; do apply_hook "$u" install; done
fi

if [ "$DO_MCP" = "1" ]; then
    echo "Registering the memory MCP server (claude, gemini, antigravity, opencode):"
    for u in $USER_LIST; do apply_mcp "$u" install; done
fi

echo
"$BIN" status || true

if [ ! -f "$CONFIG_DIR/sa.json" ]; then
    echo
    echo "NOTE: no service account at $CONFIG_DIR/sa.json - falling back to a"
    echo "      firebase-tools refresh token, which belongs to one user and is"
    echo "      deprecated. Generate a key from the Firebase console"
    echo "      (Project settings > Service accounts) and drop it there."
fi
