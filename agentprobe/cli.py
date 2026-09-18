"""Command line entry point.

    agentprobe status          what is configured, which credential, what is pending
    agentprobe push            push sessions that changed since the last run
    agentprobe push --force    push everything regardless of state
    agentprobe export FILE     dump parsed sessions to JSON (dashboard fixtures)
    agentprobe mcp             serve memory + archive over MCP (stdio)
    agentprobe doctor          what is set up, what is not, how to fix it

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
        pushed: List[Session] = []
        interrupted = ""
        for session in pending:
            try:
                result = store.push_session(session)
            except Exception as exc:
                # Persist what already landed before surfacing the failure. A
                # bulk import that dies two thirds through must resume from
                # there, not start over - re-pushing 8k writes to reach the same
                # wall is how a quota problem becomes a permanent one.
                interrupted = str(exc)
                break
            state.mark(session)
            pushed.append(session)
            totals["sessions"] += 1
            totals["writes"] += result["writes"]
            if args.verbose:
                print("pushed %s  %s  %d writes" % (
                    session.session_id[:8], session.project, result["writes"]))

        context_writes = 0
        if pushed and not interrupted:
            context_writes = store.push_context(extract_many(pushed))
        state.save()

        if interrupted:
            print("agentprobe: stopped after %d session(s), %d remaining: %s" % (
                totals["sessions"], len(pending) - totals["sessions"], interrupted),
                file=sys.stderr)
            return 0

        if args.verbose or not args.quiet:
            print("agentprobe: %d session(s), %d writes, %d context item(s) via %s" % (
                totals["sessions"], totals["writes"], context_writes, store.creds.kind))
        return 0

    except Exception as exc:
        # Never fail the hook. A capture problem must not surface as a session
        # that ended badly.
        print("agentprobe: push failed: %s" % exc, file=sys.stderr)
        return 0


def cmd_import_notes(args: argparse.Namespace) -> int:
    """Import AgentContext's legacy markdown session notes into Firestore."""
    from .legacy import parse_directory, summarize

    notes = parse_directory(args.path)
    if not notes:
        print("no notes found under %s" % args.path, file=sys.stderr)
        return 1

    stats = summarize(notes)
    print("Parsed %d note(s) from %s" % (stats["notes"], args.path))
    print("  generations       %s  (1 generated, 2 session-headed, 3 plain)" % stats["generations"])
    print("  sessions referenced %d" % stats["sessions_referenced"])
    print("  date range        %s .. %s" % stats["range"])
    print("  undated           %d" % stats["undated"])
    print("  total             %.0f KB" % (stats["bytes"] / 1024))

    if args.dry_run:
        print("\n(dry run - nothing written)")
        return 0

    config = Config.load()
    from .store import Firestore

    store = Firestore(config)
    written = store.push_notes(notes)
    print("\nwrote %d note document(s) to notes/ via %s" % (written, store.creds.kind))
    return 0


def cmd_migrate_legacy(args: argparse.Namespace) -> int:
    """Convert the previous pipeline's Firestore tree into the current schema."""
    from .migrate_legacy import LegacyMigrator
    from .store import Firestore

    store = Firestore(Config.load())
    stats = LegacyMigrator(store).migrate(dry_run=args.dry_run, verbose=args.verbose)

    print("legacy sessions found : %d" % stats["found"])
    print("  converted           : %d" % stats["converted"])
    print("  skipped (no id/date): %d" % stats["skipped"])
    print("  transcript parts    : %d" % stats["parts"])
    print("  writes              : %d%s" % (
        stats["writes"], "  (dry run - nothing written)" if args.dry_run else ""))
    return 0


def cmd_prune_legacy(args: argparse.Namespace) -> int:
    """Delete the legacy Firestore trees. Converted data must already exist."""
    from .prune_legacy import ALLOWED, LegacyPruner
    from .store import Firestore

    roots = args.roots or sorted(ALLOWED)
    pruner = LegacyPruner(Firestore(Config.load()))
    stats = pruner.prune(roots, dry_run=args.dry_run)

    total = sum(stats.values())
    for root, n in sorted(stats.items()):
        print("  %-16s %d document(s)" % (root, n))
    print("  %-16s %d%s" % ("total", total,
                            "  (dry run - nothing deleted)" if args.dry_run else "  DELETED"))
    return 0


def cmd_publish_docs(args: argparse.Namespace) -> int:
    """Publish AgentContext's markdown content to Firestore."""
    from .publish_docs import COLLECTION, publish
    from .store import Firestore

    store = Firestore(Config.load())
    stats = publish(store, args.path, dry_run=args.dry_run)

    print("markdown found : %d" % stats["found"])
    for section, n in sorted(stats["sections"].items()):
        print("  %-14s %d" % (section, n))
    if args.dry_run:
        print("  (dry run - nothing written)")
    else:
        print("  published to %s/ %s" % (
            COLLECTION,
            ("(%d stale removed)" % stats["removed"]) if stats["removed"] else ""))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the memory and archive tools over MCP on stdio.

    Never prints to stdout itself - that stream is the protocol. The client
    launches this; it is not meant to be run interactively.
    """
    from .mcp_server import main as serve

    return serve(Config.load())


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report setup state. Exits with the failure count, so a caller can branch."""
    from .doctor import main as run_doctor

    if args.all_users:
        from .doctor import all_users

        extra = []
        if args.no_auth:
            extra.append("--no-auth")
        if args.offline:
            extra.append("--offline")
        return all_users(extra)

    return run_doctor(Config.load(), auth=not args.no_auth,
                      network=not args.offline, as_json=args.json)


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

    sub.add_parser("mcp", parents=[common],
                   help="serve memory and archive tools over MCP (stdio)")

    doc = sub.add_parser("doctor", parents=[common],
                         help="check what is set up on this machine")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--no-auth", action="store_true",
                     help="skip the `gh`/`glab`/`firebase` auth probes")
    doc.add_argument("--offline", action="store_true", help="skip the Firestore round trip")
    doc.add_argument("--all-users", action="store_true",
                     help="check every account with a login shell (needs root)")

    mig = sub.add_parser("migrate-legacy", parents=[common],
                         help="convert the old claude/session tree into the current schema")
    mig.add_argument("--dry-run", action="store_true", help="report what would be written")

    prune = sub.add_parser("prune-legacy", parents=[common],
                           help="delete the legacy Firestore trees after conversion")
    prune.add_argument("roots", nargs="*", help="claude and/or agentcontext (default: both)")
    prune.add_argument("--dry-run", action="store_true", help="count what would be deleted")

    pub = sub.add_parser("publish-docs", parents=[common],
                         help="publish AgentContext markdown content to Firestore")
    pub.add_argument("path", help="the content/ directory")
    pub.add_argument("--dry-run", action="store_true")

    imp = sub.add_parser("import-notes", help="import legacy markdown notes", parents=[common])
    imp.add_argument("path", help="directory holding claude/ and gemini/ note folders")
    imp.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")

    args = parser.parse_args(argv)

    if args.command == "push":
        return cmd_push(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "import-notes":
        return cmd_import_notes(args)
    if args.command == "migrate-legacy":
        return cmd_migrate_legacy(args)
    if args.command == "prune-legacy":
        return cmd_prune_legacy(args)
    if args.command == "publish-docs":
        return cmd_publish_docs(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
