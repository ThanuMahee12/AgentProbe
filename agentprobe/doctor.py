"""What is set up on this machine, what is not, and the command that fixes it.

Written for the moment after `git clone` on a machine nobody has configured yet,
and for the agent doing it. Setting this up touches a Firebase project, four
agent CLIs with three different MCP config schemas, and a handful of external
tools that each fail in their own way - and almost every failure is silent. An
expired GitLab token still reports a configured host. A Firebase credential that
belongs to another user is simply unreadable rather than absent. MCP registration
succeeds against a client that is not installed.

So every check answers two questions: what is true, and what to run about it.
Anything that cannot say both is not worth printing.

Exit status is the number of failures, matching the convention the ssh config
verifier already uses on these machines, so CI and an agent can branch on it
without parsing the output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import CONFIG_PATH, Config

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

MARK = {OK: "+", WARN: "!", FAIL: "x", SKIP: "-"}

#: External commands get a short leash. `doctor` is run interactively and by
#: agents; one unreachable network service must not hang the whole report.
TIMEOUT = 20


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Group:
    title: str
    checks: List[Check] = field(default_factory=list)


def run(cmd: List[str], timeout: int = TIMEOUT) -> tuple:
    """-> (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:  # pragma: no cover
        return 1, str(exc)


def which(name: str) -> str:
    return shutil.which(name) or ""


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


def check_runtime() -> Group:
    g = Group("Runtime")
    v = sys.version_info
    g.checks.append(Check(
        "python", OK if v >= (3, 9) else FAIL,
        "%d.%d.%d" % (v.major, v.minor, v.micro),
        "" if v >= (3, 9) else "python 3.9+ is required",
    ))
    try:
        __import__("requests")
        g.checks.append(Check("requests", OK, "importable"))
    except ImportError:
        g.checks.append(Check("requests", FAIL, "missing", "pip install requests"))

    # cryptography signs the RS256 assertion for service-account auth, and
    # nothing else here needs it. On a machine where only root has it, every
    # other account keeps working on the refresh-token fallback and then breaks
    # the moment a service account is installed - which looks like the service
    # account being wrong rather than a missing module.
    try:
        __import__("cryptography")
        g.checks.append(Check("cryptography", OK, "importable"))
    except ImportError:
        g.checks.append(Check(
            "cryptography", FAIL,
            "missing for %s" % (shutil.which("python3") or "python3"),
            "pip install cryptography - required to use a service account",
        ))

    launcher = which("agentprobe")
    g.checks.append(Check(
        "launcher", OK if launcher else FAIL, launcher or "not on PATH",
        "" if launcher else "sudo ./install.sh",
    ))
    installed = os.path.isdir("/opt/agentprobe/agentprobe")
    g.checks.append(Check(
        "/opt/agentprobe", OK if installed else FAIL,
        "present" if installed else "missing",
        "" if installed else "sudo ./install.sh",
    ))
    return g


def check_config(config: Config) -> Group:
    g = Group("Configuration")
    info = config.describe()

    g.checks.append(Check(
        "config file", OK if info["config_file"] else WARN,
        info["config_file"] or "absent (using defaults)",
        "" if info["config_file"] else "sudo ./install.sh --project ID --email ADDR",
    ))
    g.checks.append(Check("project", OK, info["project_id"]))
    g.checks.append(Check(
        "user email", OK if config.user_email else WARN,
        config.user_email or "unset",
        "" if config.user_email else "set user_email in %s" % CONFIG_PATH,
    ))

    kind, path = info["credential"], info["credential_path"]
    if not kind:
        g.checks.append(Check(
            "credential", FAIL, "none found",
            "drop a service-account key at /etc/agentcontext/sa.json (mode 600)",
        ))
    elif kind == "service-account":
        # isfile() is not enough: a key at mode 600 root:root is *found* by every
        # account and readable by none of them, so the check has to open it.
        if os.access(path, os.R_OK):
            g.checks.append(Check("credential", OK, "service account - %s" % path))
        else:
            g.checks.append(Check(
                "credential", FAIL, "service account at %s is NOT READABLE by this user" % path,
                "make it group-readable: sudo chgrp <group> %s && sudo chmod 640 %s" % (path, path),
            ))
    else:
        # The failure mode that costs the most time on a shared machine: it works
        # for whoever ran `firebase login` and is unreadable to everyone else,
        # and nothing about the error says so.
        readable = os.access(path, os.R_OK)
        g.checks.append(Check(
            "credential", WARN if readable else FAIL,
            "refresh token - %s%s" % (path, "" if readable else " (NOT READABLE by this user)"),
            "a service account at /etc/agentcontext/sa.json works for every "
            "account; the refresh token belongs to one user",
        ))
    return g


def check_firestore(config: Config) -> Group:
    g = Group("Firestore")
    try:
        from .store import Firestore

        store = Firestore(config)
        store.timeout = TIMEOUT
        store.creds.token()
        g.checks.append(Check("authentication", OK, "token minted via %s" % store.creds.kind))
        try:
            store.list_documents("docs", page_size=1)
            g.checks.append(Check("read", OK, "query succeeded"))
        except Exception as exc:
            g.checks.append(Check("read", FAIL, str(exc)[:90],
                                  "check the project id and the key's permissions"))
    except Exception as exc:
        g.checks.append(Check("authentication", FAIL, str(exc)[:110],
                              "see the credential line above"))
    return g


#: label -> (config file relative to $HOME, key holding the servers)
MCP_CLIENTS = (
    ("claude", ".claude.json", "mcpServers"),
    ("gemini", ".gemini/settings.json", "mcpServers"),
    ("antigravity", ".gemini/config/mcp_config.json", "mcpServers"),
    ("opencode", ".config/opencode/opencode.json", "mcp"),
)

SERVER_NAME = "agentprobe-memory"


def check_mcp() -> Group:
    g = Group("MCP registration (this user)")
    home = os.path.expanduser("~")
    for label, rel, key in MCP_CLIENTS:
        path = os.path.join(home, rel)
        if not os.path.exists(path):
            g.checks.append(Check(label, FAIL, "no config at ~/%s" % rel,
                                  "sudo ./install.sh"))
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError) as exc:
            g.checks.append(Check(label, FAIL, "unreadable: %s" % str(exc)[:50]))
            continue
        servers = data.get(key) or {}
        if SERVER_NAME in servers:
            g.checks.append(Check(label, OK, "registered"))
        else:
            g.checks.append(Check(label, FAIL, "not registered", "sudo ./install.sh"))
    return g


def check_agents() -> Group:
    """The agent CLIs themselves. Absent is fine - not every machine runs all four."""
    g = Group("Agent CLIs")
    for name, hint in (
        ("claude", "npm i -g @anthropic-ai/claude-code"),
        ("gemini", "npm i -g @google/gemini-cli"),
        ("opencode", "https://opencode.ai"),
        ("agy", "Antigravity CLI"),
    ):
        path = which(name)
        g.checks.append(Check(name, OK if path else SKIP,
                              path or "not installed", "" if path else hint))
    return g


def check_tools(auth: bool = True) -> Group:
    """External tools. Presence is cheap; being *authenticated* is the real check."""
    g = Group("External tools")

    for name in ("git", "node"):
        p = which(name)
        g.checks.append(Check(name, OK if p else WARN, p or "not installed"))

    # firebase
    if not which("firebase"):
        g.checks.append(Check("firebase", SKIP, "not installed",
                              "npm i -g firebase-tools"))
    elif not auth:
        g.checks.append(Check("firebase", OK, "installed (auth not checked)"))
    else:
        code, out = run(["firebase", "login:list"])
        if code == 0 and "Logged in as" in out:
            who = out.split("Logged in as", 1)[1].strip().splitlines()[0]
            g.checks.append(Check("firebase", OK, "logged in as %s" % who))
        else:
            g.checks.append(Check("firebase", WARN, "not logged in", "firebase login"))

    # gh
    if not which("gh"):
        g.checks.append(Check("gh", SKIP, "not installed", "https://cli.github.com"))
    elif not auth:
        g.checks.append(Check("gh", OK, "installed (auth not checked)"))
    else:
        code, out = run(["gh", "auth", "status"])
        g.checks.append(Check("gh", OK, "authenticated") if code == 0
                        else Check("gh", WARN, "not logged in", "gh auth login"))

    # glab - a configured host is not an authenticated one
    if not which("glab"):
        g.checks.append(Check("glab", SKIP, "not installed",
                              "https://gitlab.com/gitlab-org/cli"))
    elif not auth:
        g.checks.append(Check("glab", OK, "installed (auth not checked)"))
    else:
        code, out = run(["glab", "auth", "status"])
        if "401" in out or "Unauthorized" in out:
            g.checks.append(Check("glab", WARN, "token rejected (401)", "glab auth login"))
        elif code == 0:
            g.checks.append(Check("glab", OK, "authenticated"))
        else:
            g.checks.append(Check("glab", WARN, "not logged in", "glab auth login"))

    # ClickUp has no CLI - it is a remote MCP server, added to a client rather
    # than installed. Stated so nobody goes looking for a binary.
    g.checks.append(Check("clickup", SKIP, "remote MCP server, no CLI",
                          "add it as an HTTP MCP server in your agent client"))
    return g


# --------------------------------------------------------------------------- #


def collect(config: Optional[Config] = None, auth: bool = True,
            network: bool = True) -> List[Group]:
    config = config or Config.load()
    groups = [check_runtime(), check_config(config)]
    if network:
        groups.append(check_firestore(config))
    groups.extend([check_mcp(), check_agents(), check_tools(auth=auth)])
    return groups


def render(groups: List[Group]) -> str:
    lines: List[str] = []
    fixes: List[str] = []

    for group in groups:
        lines.append("")
        lines.append(group.title)
        for c in group.checks:
            lines.append("  %s %-14s %s" % (MARK[c.status], c.name, c.detail))
            if c.fix and c.status in (WARN, FAIL):
                fixes.append("  %-14s %s" % (c.name, c.fix))

    failures = sum(1 for g in groups for c in g.checks if c.status == FAIL)
    warnings = sum(1 for g in groups for c in g.checks if c.status == WARN)

    lines.append("")
    lines.append("%d failure(s), %d warning(s)" % (failures, warnings))
    if fixes:
        lines.append("")
        lines.append("To fix:")
        lines.extend(fixes)
    return "\n".join(lines)


def main(config: Optional[Config] = None, auth: bool = True, network: bool = True,
         as_json: bool = False) -> int:
    groups = collect(config, auth=auth, network=network)
    if as_json:
        print(json.dumps([{
            "group": g.title,
            "checks": [{"name": c.name, "status": c.status,
                        "detail": c.detail, "fix": c.fix} for c in g.checks],
        } for g in groups], indent=2))
    else:
        print(render(groups))
    return sum(1 for g in groups for c in g.checks if c.status == FAIL)

# --------------------------------------------------------------------------- #
# every account, from one invocation
# --------------------------------------------------------------------------- #


def discover_users() -> List[str]:
    """Accounts with a real login shell and a home directory.

    The same rule install.sh wires hooks by, so `doctor --all-users` reports on
    exactly the set that was configured rather than a different one.
    """
    code, out = run(["getent", "passwd"], timeout=10)
    if code != 0:
        return []
    users = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 7:
            continue
        name, home, shell = parts[0], parts[5], parts[6]
        if shell.endswith(("nologin", "false", "sync")):
            continue
        if not (home == "/root" or home.startswith("/home/")):
            continue
        users.append(name)
    return users


def for_user(user: str, extra: Optional[List[str]] = None) -> Optional[List[dict]]:
    """Run doctor as `user` and return its parsed groups.

    Through `bash -lc` deliberately. A bare `sudo -u` keeps the *caller's*
    environment, so it resolves a different PATH and a different python3 than
    that account actually has - which has already produced two wrong diagnoses
    here, reporting a missing launcher and a missing module for an account that
    had neither problem. A login shell is the only way to see what the user sees.
    """
    inner = "agentprobe doctor --json " + " ".join(extra or [])
    code, out = run(["sudo", "-n", "-u", user, "bash", "-lc", inner], timeout=180)
    start = out.find("[")
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except ValueError:
        return None


def all_users(extra: Optional[List[str]] = None) -> int:
    """Report every account. Returns the total failure count."""
    if os.geteuid() != 0:
        print("--all-users needs root (it runs the check as each account)")
        return 1

    users = discover_users()
    if not users:
        print("no accounts with a login shell found")
        return 1

    total = 0
    detail = []
    print("Checking %d account(s): %s\n" % (len(users), ", ".join(users)))

    for user in users:
        groups = for_user(user, extra)
        if groups is None:
            print("  %-12s could not run doctor as this account" % user)
            total += 1
            continue
        checks = [c for g in groups for c in g["checks"]]
        fails = [c for c in checks if c["status"] == FAIL]
        warns = [c for c in checks if c["status"] == WARN]
        total += len(fails)
        print("  %-12s %d failure(s), %d warning(s)" % (user, len(fails), len(warns)))
        for c in fails:
            detail.append("  %-12s %-14s %s" % (user, c["name"], c["detail"]))

    if detail:
        print("\nFailures:")
        for line in detail:
            print(line)

    print("\n%d failure(s) across %d account(s)" % (total, len(users)))
    return total

