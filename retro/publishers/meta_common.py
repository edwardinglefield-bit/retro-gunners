"""Shared bits for Instagram + Threads (both Meta Graph-style, both fetch images by URL)."""
from __future__ import annotations

import time

from ..http import session
from .base import PublishError

REFRESH_EVERY_DAYS = 7


def call(method: str, url: str, **params) -> dict:
    r = session().request(method, url, params=params if method == "GET" else None,
                          data=params if method != "GET" else None, timeout=90)
    try:
        j = r.json()
    except ValueError:
        j = {"raw": r.text[:300]}
    if not r.ok or "error" in j:
        raise PublishError(f"{url.split('?')[0]} {r.status_code}: {j.get('error', j)}")
    return j


def wait_ready(status_url: str, token: str, field: str = "status_code", tries: int = 10, delay: float = 6):
    for _ in range(tries):
        j = call("GET", status_url, fields=field, access_token=token)
        st = j.get(field) or j.get("status")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise PublishError(f"container {st}: {j}")
        time.sleep(delay)
    raise PublishError("container not ready in time")


def maybe_refresh(vault, name: str, env_var: str, refresh_url: str, grant: str):
    """Meta long-lived tokens last 60 days and can be refreshed once they're >24h old."""
    from ..config import env
    token = vault.token(name, env_var)
    if not token or not env("STATE_KEY"):
        return
    rec = vault.meta(name)
    if rec.get("seed") != (env(env_var) or "")[-12:]:
        # new secret pasted in: start the clock now, first refresh in a week
        vault.put(name, token, env_var, refreshed_at=time.time())
        return
    last = rec.get("refreshed_at", 0)
    if time.time() - last < REFRESH_EVERY_DAYS * 86400:
        return
    j = call("GET", refresh_url, grant_type=grant, access_token=token)
    vault.put(name, j["access_token"], env_var, refreshed_at=time.time(),
              expires_at=time.time() + int(j.get("expires_in", 0)))
