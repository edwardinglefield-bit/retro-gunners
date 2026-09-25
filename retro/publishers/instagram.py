"""Instagram (Instagram API with Instagram Login). Feed post = art, Story = phone wallpaper."""
from __future__ import annotations

from ..config import env
from .base import Media, Publisher
from .meta_common import call, maybe_refresh, wait_ready

TOKEN_ENV = "IG_ACCESS_TOKEN"


class Instagram(Publisher):
    name = "instagram"

    @property
    def base(self) -> str:
        return f"https://graph.instagram.com/{self.cfg.get('publish.instagram.graph_version', 'v25.0')}"

    @property
    def token(self) -> str | None:
        return self.vault.token("instagram", TOKEN_ENV)

    def configured(self) -> bool:
        return bool(self.token)

    def switched_on(self) -> bool:
        return bool(self.cfg.get("publish.instagram.feed", True) or self.cfg.get("publish.instagram.story_wallpaper", True))

    def needs_public_urls(self) -> bool:
        return True

    def user_id(self) -> str:
        if env("IG_USER_ID"):
            return env("IG_USER_ID")
        j = call("GET", f"{self.base}/me", fields="user_id,username", access_token=self.token)
        return str(j.get("user_id") or j["id"])

    def _post(self, uid: str, **fields) -> str:
        c = call("POST", f"{self.base}/{uid}/media", access_token=self.token, **fields)
        wait_ready(f"{self.base}/{c['id']}", self.token)
        p = call("POST", f"{self.base}/{uid}/media_publish", creation_id=c["id"], access_token=self.token)
        return p["id"]

    def publish(self, media: Media, caption: str, progress: dict | None = None) -> dict:
        prog = progress if progress is not None else {}
        uid = self.user_id()
        if self.cfg.get("publish.instagram.feed", True) and not prog.get("id"):
            prog["id"] = self._post(uid, image_url=media.urls["feed"], caption=caption,
                                    alt_text="Mid-century modern style illustration of an Arsenal photo")
            try:
                prog["url"] = call("GET", f"{self.base}/{prog['id']}", fields="permalink",
                                   access_token=self.token).get("permalink")
            except Exception:   # post is live regardless
                pass
        if self.cfg.get("publish.instagram.story_wallpaper", True) and not prog.get("story_id"):
            prog["story_id"] = self._post(uid, image_url=media.urls["story"], media_type="STORIES")
        return dict(prog)

    def refresh_token(self):
        maybe_refresh(self.vault, "instagram", TOKEN_ENV, "https://graph.instagram.com/refresh_access_token",
                      "ig_refresh_token")

    def check(self) -> str:
        j = call("GET", f"{self.base}/me", fields="user_id,username", access_token=self.token)
        return f"@{j.get('username')} (id {j.get('user_id')})"
