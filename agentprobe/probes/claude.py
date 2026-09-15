"""Claude Code probe.

Claude Code writes one JSONL transcript per session under
``~/.claude/projects/<slugified-cwd>/<session-id>.jsonl`` and fires lifecycle
hooks, which makes it the only provider that can push on SessionEnd instead of
being polled.

Record types seen in a transcript: user, assistant, system, attachment,
file-history-snapshot, permission-mode, last-prompt, mode, queue-operation.
Only user/assistant carry a `message`; the rest are UI and replay bookkeeping
and are counted but not mined.

Extraction is purely structural - every command and file path below is read
straight out of a `tool_use` block. No model is involved, so this costs nothing
and cannot invent detail that was not in the record.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from ..schema import Command, FileTouch, Session
from .base import Probe

# tool name -> where the path, produced content and replaced text sit in its input
FILE_TOOLS = {
    "Read":         {"path": "file_path",     "action": "read"},
    "Write":        {"path": "file_path",     "action": "write", "content": "content"},
    "Edit":         {"path": "file_path",     "action": "edit",
                     "content": "new_string", "replaced": "old_string"},
    "NotebookEdit": {"path": "notebook_path", "action": "edit",  "content": "new_source"},
}

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")


class ClaudeProbe(Probe):
    provider = "claude"

    def __init__(self, user_email: str = "", root: str = "") -> None:
        super().__init__(user_email)
        self.root = root or DEFAULT_ROOT

    # ------------------------------------------------------------------ #

    def discover(self) -> Iterable[str]:
        if not os.path.isdir(self.root):
            return []
        found: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if name.endswith(".jsonl"):
                    found.append(os.path.join(dirpath, name))
        return sorted(found)

    # ------------------------------------------------------------------ #

    def extract(self, handle: str) -> Optional[Session]:
        with open(handle, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()

        records = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # a torn final line is normal if the session is live

        if not records:
            return None

        session_id = ""
        cwd = ""
        git_branch = ""
        version = ""
        timestamps: List[str] = []
        message_count = 0
        preview = ""
        commands: List[Command] = []
        files: List[FileTouch] = []
        # tool_use id -> whether its result came back an error
        errored: Dict[str, bool] = {}

        for rec in records:
            session_id = session_id or rec.get("sessionId") or rec.get("session_id") or ""
            cwd = cwd or rec.get("cwd") or ""
            git_branch = git_branch or rec.get("gitBranch") or ""
            version = version or rec.get("version") or ""

            ts = rec.get("timestamp") or ""
            if ts:
                timestamps.append(ts)

            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue

            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            message_count += 1

            content = message.get("content")

            # First real user turn becomes the card preview. isMeta records are
            # command output and system injections, not something the user typed.
            if rtype == "user" and not preview and not rec.get("isMeta"):
                preview = _first_text(content)[:280]

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "tool_use":
                    _collect_tool_use(block, ts, commands, files)

                elif btype == "tool_result":
                    tool_id = block.get("tool_use_id") or ""
                    if tool_id:
                        errored[tool_id] = bool(block.get("is_error"))

        if not session_id:
            # fall back to the filename, which is the session id
            session_id = os.path.splitext(os.path.basename(handle))[0]

        # Fold tool_result outcomes back onto the commands that produced them.
        for cmd in commands:
            if cmd.tool_id in errored:
                cmd.exit_status = 1 if errored[cmd.tool_id] else 0

        timestamps.sort()
        started = timestamps[0] if timestamps else ""
        ended = timestamps[-1] if timestamps else ""

        return Session(
            provider=self.provider,
            session_id=session_id,
            date=started[:10] if started else "",
            started=started,
            ended=ended,
            cwd=cwd,
            project=self.project_from_cwd(cwd),
            user_email=self.user_email,
            os_user=self.os_user(),
            host=self.host(),
            git_branch=git_branch,
            agent_version=version,
            message_count=message_count,
            preview=preview,
            commands=commands,
            files=files,
            transcript=raw,
        )


# ---------------------------------------------------------------------- #


def _collect_tool_use(
    block: Dict[str, Any],
    ts: str,
    commands: List[Command],
    files: List[FileTouch],
) -> None:
    name = block.get("name") or ""
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return

    if name == "Bash":
        cmd = tool_input.get("command")
        if cmd:
            commands.append(
                Command(
                    ts=ts,
                    command=cmd,
                    description=tool_input.get("description") or "",
                    tool_id=block.get("id") or "",
                )
            )
        return

    spec = FILE_TOOLS.get(name)
    if spec:
        path = tool_input.get(spec["path"])
        if not path:
            return
        content = tool_input.get(spec["content"]) if spec.get("content") else ""
        replaced = tool_input.get(spec["replaced"]) if spec.get("replaced") else ""
        files.append(
            FileTouch(
                ts=ts,
                path=path,
                action=spec["action"],
                content=content if isinstance(content, str) else "",
                replaced=replaced if isinstance(replaced, str) else "",
            )
        )


def _first_text(content: Any) -> str:
    """Pull plain text out of either content shape.

    User content is a bare string for typed input, or a block list when it
    carries attachments and tool results.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "").strip()
    return ""
