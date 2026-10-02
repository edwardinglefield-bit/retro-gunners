"""Threads API: two-image carousel (art + wallpaper)."""
from __future__ import annotations

import time

from .base import Media, Publisher, PublishError
from .meta_common import call, maybe_refresh

BASE = "https://graph.threads.net/v1.0"
TOKEN_ENV = "THREADS_ACCESS_TOKEN"


class Threads(Publisher):
    name = "threads"

    @property
    def token(self) -> str | None:
        return self.vault.token("threads", TOKEN_ENV)

    def configured(self) -> bool:
        return bool(self.token)

    def switched_on(self) -> bool:
        return bool(self.cfg.get("publish.threads.enabled", True))

    def needs_public_urls(self) -> bool:
        return True

    def _wait(self, cid: str):
        for _ in range(10):
            st = call("GET", f"{BASE}/{cid}", fields="status,error_message", access_token=self.token)
            if st.get("status") in ("FINISHED", "PUBLISHED"):
                return
            if st.get("status") in ("ERROR", "EXPIRED"):
                raise PublishError(f"threads container {st}")
            time.sleep(6)
        raise PublishError("threads container not ready in time")

    def _carousel(self, uid: str, children: list[str], caption: str) -> dict:
        """Children report FINISHED a little before every Threads server can see them, so the first try
        can fail with 'Invalid carousel children' (subcode 4279004). Give it a minute before giving up."""
        for wait in (0, 10, 20, 30):
            time.sleep(wait)
            try:
                return call("POST", f"{BASE}/{uid}/threads", media_type="CAROUSEL", children=",".join(children),
                            text=caption, access_token=self.token)
            except PublishError as e:
                if "4279004" not in str(e) or wait == 30:
                    raise

    def publish(self, media: Media, caption: str, progress: dict | None = None) -> dict:
        prog = progress if progress is not None else {}
        if prog.get("id"):
            return dict(prog)
        uid = call("GET", f"{BASE}/me", fields="id", access_token=self.token)["id"]
        children = []
        for name in ("art", "wallpaper"):
            c = call("POST", f"{BASE}/{uid}/threads", media_type="IMAGE", image_url=media.urls[name],
                     is_carousel_item="true", access_token=self.token)
            children.append(c["id"])
        for cid in children:
            self._wait(cid)
        car = self._carousel(uid, children, caption)
        self._wait(car["id"])
        pub = call("POST", f"{BASE}/{uid}/threads_publish", creation_id=car["id"], access_token=self.token)
        prog["id"] = pub["id"]
        url = None
        try:
            url = call("GET", f"{BASE}/{pub['id']}", fields="permalink", access_token=self.token).get("permalink")
        except Exception:   # the post is live; a failed permalink lookup must not trigger a repost
            pass
        return {"id": pub["id"], "url": url}

    def refresh_token(self):
        maybe_refresh(self.vault, "threads", TOKEN_ENV, "https://graph.threads.net/refresh_access_token",
                      "th_refresh_token")

    def check(self) -> str:
        j = call("GET", f"{BASE}/me", fields="id,username", access_token=self.token)
        return f"@{j.get('username')}"
