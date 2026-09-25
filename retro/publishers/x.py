"""X (Twitter) via API v2: POST /2/media/upload then POST /2/tweets. OAuth 1.0a user context."""
from __future__ import annotations

from ..config import env
from ..http import session
from .base import Media, Publisher, PublishError

API = "https://api.x.com/2"
KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


class X(Publisher):
    name = "x"

    def configured(self) -> bool:
        return all(env(k) for k in KEYS)

    def switched_on(self) -> bool:
        return bool(self.cfg.get("publish.x.enabled", True))

    def _auth(self):
        from requests_oauthlib import OAuth1
        return OAuth1(env("X_API_KEY"), env("X_API_SECRET"), env("X_ACCESS_TOKEN"), env("X_ACCESS_SECRET"))

    def _upload(self, path) -> str:
        with open(path, "rb") as fh:
            r = session().post(f"{API}/media/upload", auth=self._auth(),
                               files={"media": (path.name, fh, "image/jpeg")},
                               data={"media_category": "tweet_image"}, timeout=120)
        if not r.ok:
            raise PublishError(f"X media upload {r.status_code}: {r.text[:300]}")
        j = r.json()
        return str((j.get("data") or {}).get("id") or j.get("media_id_string"))

    def publish(self, media: Media, caption: str, progress: dict | None = None) -> dict:
        ids = [self._upload(media.paths["art"]), self._upload(media.paths["wallpaper"])]
        r = session().post(f"{API}/tweets", auth=self._auth(),
                           json={"text": caption, "media": {"media_ids": ids}}, timeout=60)
        if not r.ok:
            raise PublishError(f"X tweet {r.status_code}: {r.text[:300]}")
        tid = r.json()["data"]["id"]
        return {"id": tid, "url": f"https://x.com/i/web/status/{tid}"}

    def check(self) -> str:
        r = session().get(f"{API}/users/me", auth=self._auth(), timeout=30)
        return f"@{r.json()['data']['username']}" if r.ok else f"error {r.status_code}: {r.text[:120]}"
