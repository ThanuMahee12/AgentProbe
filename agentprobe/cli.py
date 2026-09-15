"""Command line entry point.

    agentprobe status          what is configured, which credential, what is pending
    agentprobe push            push sessions that changed since the last run
    agentprobe push --force    push everything regardless of state
    agentprobe export FILE     dump parsed sessions to JSON (dashboard fixtures)

`push` is what the SessionEnd hook calls. It is deliberately quiet on success
and never exits non-zero for a capture failure: a hook that fails loudly when
the network is down would make every session end in an error the user cannot
act on. Problems go to stderr and the session still closes cleanly.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .config import Config
from .context import extract_many
from .probes.claude import ClaudeProbe
from .schema import Session


def _probes(config: Config) -> List[ClaudeProbe]:
    # Only Claude for now; the poll-based probes register here as they land.
    return [ClaudeProbe(user_email=config.user_email)]


def _collect(config: Config) -> List[Session]:
    sessions: List[Session] = []
    for probe in _probes(config):
        sessions.extend(probe.sessions())
    return sessions


# --------------------------------------------------------------------------- #


def cmd_status(args: argparse.Namespace) -> int:
    config = Config.load()
    info = config.describe()

    print("AgentProbe")
    for key in ("config_file", "project_id", "user_email", "enabled", "state_dir"):
        print("  %-14s %s" % (key, info[key]))
    print("  %-14s %s" % ("credential", info["credential"] or "NONE FOUND"))
    if info["credential_path"]:
        print("  %-14s %s" % ("", info["credential_path"]))

    sessions = _collect(config)
    print("\nDiscovered %d session(s)" % len(sessions))

    if info["credential"]:
        from .store import State

        state = State(config)
        pending = [s for s in sessions if not state.is_current(s)]
        print("  %d pending push, %d already current" % (len(pending), len(sessions) - len(pending)))
        for s in pending[:10]:
            print("    %s  %-16s %4d msg  %3d cmd  %s" % (
                s.date, s.project, s.message_count, len(s.commands), s.session_id[:8]))
    else:
        print("  (no credential - nothing can be pushed)")

    if args.verbose:
        items = extract_many(sessions)
        print("\nContext items: %d" % len(items))

    return 0


def cmd_push(args: argparse.Namespace) -> int:
    config = Config.load()

    if not config.enabled:
        return 0

    try:
        from .store import Firestore, State
    except ImportError as exc:
        print("agentprobe: %s" % exc, file=sys.stderr)
        return 0

    sessions = _collect(config)
    if not sessions:
        return 0

    try:
        store = Firestore(config)
        state = State(config)

        pending = sessions if args.force else [s for s in sessions if not state.is_current(s)]
        if not pending:
            if args.verbose:
                print("nothing to push")
            return 0

        totals = {"sessions": 0, "writes": 0}
        for session in pending:
            result = store.push_session(session)
            state.mark(session)
            totals["sessions"] += 1
            totals["writes"] += result["writes"]
            if args.verbose:
                print("pushed %s  %s  %d writes" % (
                    session.session_id[:8], session.project, result["writes"]))

        items = extract_many(pending)
        context_writes = store.push_context(items)
        state.save()

        if args.verbose or not args.quiet:
            print("agentprobe: %d session(s), %d writes, %d context item(s) via %s" % (
                totals["sessions"], totals["writes"], context_writes, store.creds.kind))
        return 0

    except Exception as exc:
        # Never fail the hook. A capture problem must not surface as a session
        # that ended badly.
        print("agentprobe: push failed: %s" % exc, file=sys.stderr)
        return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = Config.load()
    sessions = _collect(config)
    items = extract_many(sessions)

    def as_json(s: Session) -> dict:
        d = s.summary_doc()
        d["commands"] = [c.to_dict() for c in s.commands]
        d["files"] = [f.to_dict() for f in s.files]
        return d

    payload = {"sessions": [as_json(s) for s in sessions], "context": [i.to_dict() for i in items]}
    with open(args.path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("wrote %s (%d sessions, %d context items)" % (args.path, len(sessions), len(items)))
    return 0


# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    # Shared flags are declared on a parent so they work on either side of the
    # subcommand - `agentprobe -v push` and `agentprobe push -v` both being
    # natural things to type, and the hook command reads better with the flag last.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("-q", "--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="agentprobe", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show configuration and pending work", parents=[common])

    push = sub.add_parser("push", help="push changed sessions to Firestore", parents=[common])
    push.add_argument("--force", action="store_true", help="ignore state, push everything")

    export = sub.add_parser("export", help="dump parsed sessions to JSON", parents=[common])
    export.add_argument("path")

    args = parser.parse_args(argv)

    if args.command == "push":
        return cmd_push(args)
    if args.command == "export":
        return cmd_export(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
