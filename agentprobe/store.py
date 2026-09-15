"""Firestore writer.

Talks to the REST API directly rather than pulling in google-cloud-firestore:
the dependency footprint of this thing matters because it runs inside a
SessionEnd hook on every machine, and `requests` + `cryptography` are already
present everywhere we care about.

Two credential paths, both minting a short-lived OAuth access token:

* **service account** - a Firebase key at /etc/agentcontext/sa.json. Signs a
  RS256 JWT and exchanges it. Not tied to any person's login, which is what
  makes it correct for hooks running as root/bench/ec2-user.
* **refresh token** - the credential `firebase login` leaves in configstore.
  Works with no setup, but it is one user's personal login and the CLI warns
  the mechanism is deprecated. Used only when no service account is present.

Writes go through `documents:commit`, which is atomic per batch and far cheaper
than one request per document - a session with 141 commands would otherwise be
141 round trips.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .config import Config
from .context import ContextItem
from .schema import Session

TOKEN_URL = "https://oauth2.googleapis.com/token"
FIRESTORE_ROOT = "https://firestore.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/datastore"

# The firebase-tools OAuth client. These are installed-application credentials
# shipped inside the public npm package - not a secret, and the only way to
# redeem a token that `firebase login` produced.
FIREBASE_TOOLS_CLIENT_ID = (
    "563584335869-fgrhgmd47bqnekij5i8b5pr03ho849e6.apps.googleusercontent.com"
)
FIREBASE_TOOLS_CLIENT_SECRET = "j9iVZfS8kkCEFUPaAeJV0sAi"

#: Firestore caps a commit at 500 writes.
MAX_WRITES = 450

TIMEOUT = 30


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


class Credentials:
    """Mints and caches an access token from whichever credential is present."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._token = ""
        self._expires_at = 0.0
        self.kind = ""

    def token(self) -> str:
        # 60s of slack so a long push cannot straddle the expiry
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        sa_path = self.config.service_account_path()
        if sa_path:
            self._token, ttl = self._from_service_account(sa_path)
            self.kind = "service-account"
        else:
            rt_path = self.config.refresh_token_path()
            if not rt_path:
                raise AuthError(
                    "no credential found - expected a service account at "
                    "/etc/agentcontext/sa.json or a firebase-tools refresh token"
                )
            self._token, ttl = self._from_refresh_token(rt_path)
            self.kind = "refresh-token"

        self._expires_at = time.time() + ttl
        return self._token

    # -- service account ------------------------------------------------- #

    @staticmethod
    def _from_service_account(path: str) -> Tuple[str, float]:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        with open(path, "r", encoding="utf-8") as fh:
            key = json.load(fh)

        for field in ("client_email", "private_key"):
            if not key.get(field):
                raise AuthError("service account %s is missing %r" % (path, field))

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": key["client_email"],
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }

        def seg(obj: Dict[str, Any]) -> bytes:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        signing_input = seg(header) + b"." + seg(claims)
        private_key = serialization.load_pem_private_key(
            key["private_key"].encode("utf-8"), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise AuthError("service account token exchange failed: %s" % resp.text[:300])
        body = resp.json()
        return body["access_token"], float(body.get("expires_in", 3600))

    # -- refresh token ---------------------------------------------------- #

    @staticmethod
    def _from_refresh_token(path: str) -> Tuple[str, float]:
        with open(path, "r", encoding="utf-8") as fh:
            store = json.load(fh)
        refresh = (store.get("tokens") or {}).get("refresh_token")
        if not refresh:
            raise AuthError("no refresh_token in %s" % path)

        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": FIREBASE_TOOLS_CLIENT_ID,
                "client_secret": FIREBASE_TOOLS_CLIENT_SECRET,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise AuthError("refresh token exchange failed: %s" % resp.text[:300])
        body = resp.json()
        return body["access_token"], float(body.get("expires_in", 3600))


# --------------------------------------------------------------------------- #
# value encoding
# --------------------------------------------------------------------------- #


def encode(value: Any) -> Dict[str, Any]:
    """Python -> Firestore typed value.

    bool is checked before int deliberately: bool subclasses int in Python, and
    encoding True as integerValue 1 silently changes the type in the database.
    """
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        # Firestore integers are 64-bit and arrive as strings over REST
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: encode(v) for k, v in value.items()}}}
    return {"stringValue": str(value)}


def encode_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: encode(v) for k, v in data.items()}


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


class Firestore:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config.load()
        self.creds = Credentials(self.config)
        # Two forms of the same location. `name_base` is the resource name a
        # write carries inside the request body; `base` is the HTTP endpoint.
        # Firestore rejects a document name that starts with the API URL -
        # it must begin with "projects/".
        self.name_base = "projects/%s/databases/(default)/documents" % self.config.project_id
        self.base = "%s/%s" % (FIRESTORE_ROOT, self.name_base)
        self._session = requests.Session()

    # -- low level -------------------------------------------------------- #

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer %s" % self.creds.token(),
            "Content-Type": "application/json",
        }

    def commit(self, writes: List[Dict[str, Any]]) -> int:
        """Apply writes in batches. Returns how many were applied."""
        applied = 0
        for i in range(0, len(writes), MAX_WRITES):
            chunk = writes[i:i + MAX_WRITES]
            resp = self._session.post(
                "%s:commit" % self.base,
                headers=self._headers(),
                data=json.dumps({"writes": chunk}),
                timeout=TIMEOUT,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    "firestore commit failed (%s): %s" % (resp.status_code, resp.text[:400])
                )
            applied += len(chunk)
        return applied

    def get(self, path: str) -> Optional[Dict[str, Any]]:
        resp = self._session.get(
            "%s/%s" % (self.base, path), headers=self._headers(), timeout=TIMEOUT
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError("firestore get failed (%s): %s" % (resp.status_code, resp.text[:300]))
        return resp.json()

    def write(self, path: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """A full-document overwrite, expressed as an update with no mask."""
        return {
            "update": {
                "name": "%s/%s" % (self.name_base, path),
                "fields": encode_fields(fields),
            }
        }

    # -- domain ----------------------------------------------------------- #

    def session_path(self, session: Session) -> str:
        return "projects/%s/days/%s/sessions/%s" % (
            session.project,
            session.day_key,
            session.session_id,
        )

    def push_session(self, session: Session) -> Dict[str, int]:
        """Write one session: summary document plus transcript chunks and commands.

        Idempotent - every write targets a deterministic path, so re-running
        after a resumed session updates in place instead of duplicating.
        """
        path = self.session_path(session)
        writes: List[Dict[str, Any]] = [self.write(path, session.summary_doc())]

        for n, chunk in enumerate(session.transcript_chunks):
            writes.append(
                self.write(
                    "%s/parts/%04d" % (path, n),
                    {"index": n, "text": chunk, "chars": len(chunk)},
                )
            )

        for n, cmd in enumerate(session.commands):
            writes.append(self.write("%s/commands/%04d" % (path, n), cmd.to_dict()))

        # Files carry the content the agent produced, so a script it wrote is a
        # document you can open rather than something to reconstruct from a
        # transcript chunk. Without this the summary reports a file_count the
        # dashboard has nothing to render against.
        for n, touch in enumerate(session.files):
            writes.append(self.write("%s/files/%04d" % (path, n), touch.to_dict()))

        applied = self.commit(writes)
        return {
            "writes": applied,
            "chunks": len(session.transcript_chunks),
            "commands": len(session.commands),
            "files": len(session.files),
        }

    def push_context(self, items: Iterable[ContextItem]) -> int:
        writes = [self.write("context/%s" % i.doc_id, i.to_dict()) for i in items]
        return self.commit(writes) if writes else 0

    def push_notes(self, notes: Iterable[Any]) -> int:
        """Imported legacy markdown notes.

        Their own collection rather than `sessions`: most of them are
        hand-written notes with no session id, turn count or transcript, and
        filing them as sessions would put mostly-empty records on the timeline.
        Document ids derive from the source path, so re-importing updates in
        place instead of duplicating.
        """
        writes = [self.write("notes/%s" % n.doc_id, n.to_dict()) for n in notes]
        return self.commit(writes) if writes else 0


# --------------------------------------------------------------------------- #
# per-user state: what has already been pushed
# --------------------------------------------------------------------------- #


class State:
    """Tracks the transcript checksum last pushed for each session.

    Sessions are append-only and get resumed, so the checksum is what says
    whether there is anything new - comparing timestamps would re-push an
    unchanged transcript every run.
    """

    def __init__(self, config: Config) -> None:
        self.path = os.path.join(config.state_dir, "pushed.json")
        self._data: Dict[str, str] = {}
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (ValueError, OSError):
                self._data = {}

    def is_current(self, session: Session) -> bool:
        return self._data.get(session.session_id) == session.transcript_sha256

    def mark(self, session: Session) -> None:
        self._data[session.session_id] = session.transcript_sha256

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)
