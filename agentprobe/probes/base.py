"""Probe interface.

A probe knows exactly one agent's storage format and nothing else. It answers
two questions: which sessions exist, and what does one of them look like as a
normalized `Session`.

Deliberately not an ABC with enforced methods - probes get written against
half-understood vendor formats, and a probe that can only do `discover` is still
useful. Subclasses override what they can.
"""

from __future__ import annotations

import getpass
import os
import socket
from typing import Iterable, Iterator, Optional

from ..schema import Session


class Probe:
    #: short id stored on every session doc, e.g. "claude"
    provider = "unknown"

    def __init__(self, user_email: str = "") -> None:
        self.user_email = user_email or os.environ.get("AGENTPROBE_EMAIL", "")

    # -- identity shared by every probe ---------------------------------- #

    @staticmethod
    def os_user() -> str:
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER", "unknown")

    @staticmethod
    def host() -> str:
        return socket.gethostname()

    @staticmethod
    def project_from_cwd(cwd: str) -> str:
        """Last path segment of cwd.

        Not the git repo name - plenty of sessions happen outside a repo, and
        cwd is always present. `/` degrades to "root" rather than "".
        """
        name = os.path.basename(os.path.normpath(cwd or ""))
        return name or "root"

    # -- to implement ---------------------------------------------------- #

    def discover(self) -> Iterable[str]:
        """Yield opaque session handles (usually file paths)."""
        return []

    def extract(self, handle: str) -> Optional[Session]:
        """Turn one handle into a normalized Session, or None if unusable."""
        raise NotImplementedError

    def sessions(self) -> Iterator[Session]:
        for handle in self.discover():
            try:
                session = self.extract(handle)
            except Exception as exc:  # one bad transcript must not stop the run
                print("agentprobe: failed to extract %s: %s" % (handle, exc))
                continue
            if session is not None:
                yield session
