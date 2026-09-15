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
#   sudo ./install.sh --uninstall     remove hooks (leaves code and data)
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
UNINSTALL=0
USERS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --users)     USERS="$2"; shift 2 ;;
        --no-hooks)  DO_HOOKS=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
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

if [ "$UNINSTALL" = "1" ]; then
    echo "Removing AgentProbe hooks:"
    for u in $USER_LIST; do apply_hook "$u" uninstall; done
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

echo
"$BIN" status || true

if [ ! -f "$CONFIG_DIR/sa.json" ]; then
    echo
    echo "NOTE: no service account at $CONFIG_DIR/sa.json - falling back to a"
    echo "      firebase-tools refresh token, which belongs to one user and is"
    echo "      deprecated. Generate a key from the Firebase console"
    echo "      (Project settings > Service accounts) and drop it there."
fi
