"""Encrypted store for auto-refreshed access tokens (Meta tokens rotate every 60 days).

Initial tokens come from GitHub secrets; refreshed ones are saved encrypted in state.json
(which lives on the public data branch) using STATE_KEY, which only exists as a secret.
"""
from __future__ import annotations

import json
import logging

from .config import env

log = logging.getLogger(__name__)


def _fernet():
    key = env("STATE_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


class Vault:
    def __init__(self, state):
        self.state = state
        self._data: dict = {}
        blob = state["vault"]
        f = _fernet()
        if blob and f:
            try:
                self._data = json.loads(f.decrypt(blob.encode()).decode())
            except Exception as e:
                log.warning("vault unreadable (STATE_KEY changed?): %s", e)

    def token(self, name: str, env_var: str) -> str | None:
        """Newest known token: refreshed copy if we have one and the secret wasn't changed since."""
        rec = self._data.get(name) or {}
        secret = env(env_var)
        if rec.get("token") and rec.get("seed") == (secret or "")[-12:]:
            return rec["token"]
        return secret

    def meta(self, name: str) -> dict:
        return self._data.get(name) or {}

    def put(self, name: str, token: str, env_var: str, **meta):
        f = _fernet()
        if not f:
            log.warning("STATE_KEY not set: refreshed %s token cannot be persisted", name)
            return
        self._data[name] = {"token": token, "seed": (env(env_var) or "")[-12:], **meta}
        self.state.set("vault", f.encrypt(json.dumps(self._data).encode()).decode())
