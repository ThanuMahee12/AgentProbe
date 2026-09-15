"""Machine-global configuration.

Everything lives under /etc/agentcontext so a single root-owned setup serves
every user on the box - the same shape as /opt/shellrc. Nothing is per-user
except the state file, which tracks what each account has already pushed.

Resolution order for every value: environment variable, then the global config
file, then a default. The environment wins so a one-off run can point at a
different project without editing anything.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

CONFIG_DIR = "/etc/agentcontext"
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

#: Preferred credential: a Firebase service-account key. Root-owned, readable by
#: whichever accounts run probes. Not tied to any person's login.
SERVICE_ACCOUNT_PATH = os.path.join(CONFIG_DIR, "sa.json")

#: Fallback: the refresh token the firebase CLI stores after `firebase login`.
#: Works immediately but is one user's personal credential, and the CLI warns
#: the mechanism is deprecated - hence the service account above.
FIREBASE_TOOLS_TOKEN = os.path.expanduser(
    "~/.config/configstore/firebase-tools.json"
)
FIREBASE_TOOLS_FALLBACKS = [
    "/home/thanumahee/.config/configstore/firebase-tools.json",
    "/root/.config/configstore/firebase-tools.json",
]

DEFAULT_PROJECT = "agentcontext-sessions"


class Config:
    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self._data = data or {}

    # -- loading --------------------------------------------------------- #

    @classmethod
    def load(cls) -> "Config":
        data: Dict[str, Any] = {}
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (ValueError, OSError):
                pass
        return cls(data)

    def _get(self, key: str, env: str, default: Any = None) -> Any:
        val = os.environ.get(env)
        if val:
            return val
        if key in self._data and self._data[key]:
            return self._data[key]
        return default

    # -- values ---------------------------------------------------------- #

    @property
    def project_id(self) -> str:
        return self._get("project_id", "AGENTPROBE_PROJECT", DEFAULT_PROJECT)

    @property
    def user_email(self) -> str:
        return self._get("user_email", "AGENTPROBE_EMAIL", "")

    @property
    def enabled(self) -> bool:
        if os.environ.get("AGENTPROBE_DISABLE"):
            return False
        return bool(self._data.get("enabled", True))

    @property
    def state_dir(self) -> str:
        """Per-user, because each account pushes its own sessions."""
        return self._get(
            "state_dir",
            "AGENTPROBE_STATE",
            os.path.expanduser("~/.local/state/agentprobe"),
        )

    def service_account_path(self) -> Optional[str]:
        for candidate in (
            os.environ.get("AGENTPROBE_CREDENTIALS"),
            self._data.get("service_account"),
            SERVICE_ACCOUNT_PATH,
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        ):
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def refresh_token_path(self) -> Optional[str]:
        """First configstore that actually holds a refresh token.

        Existence is not enough: the firebase CLI creates a configstore the
        moment it runs, so a machine where someone only ever used
        FIREBASE_TOKEN has an empty one. Picking it by filename alone shadows a
        working credential further down the list.
        """
        candidates = [self._data.get("firebase_tools_token"), FIREBASE_TOOLS_TOKEN]
        candidates.extend(FIREBASE_TOOLS_FALLBACKS)
        for candidate in candidates:
            if not candidate or not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    store = json.load(fh)
            except (ValueError, OSError):
                continue
            if (store.get("tokens") or {}).get("refresh_token"):
                return candidate
        return None

    def describe(self) -> Dict[str, Any]:
        sa = self.service_account_path()
        rt = self.refresh_token_path()
        return {
            "config_file": CONFIG_PATH if os.path.isfile(CONFIG_PATH) else None,
            "project_id": self.project_id,
            "user_email": self.user_email or "(unset)",
            "enabled": self.enabled,
            "state_dir": self.state_dir,
            "credential": "service-account" if sa else ("refresh-token" if rt else None),
            "credential_path": sa or rt,
        }
