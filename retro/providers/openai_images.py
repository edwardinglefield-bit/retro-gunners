"""OpenAI Images edit endpoint (gpt-image-* models)."""
from __future__ import annotations

import base64
import logging

from ..config import env
from ..http import session
from .base import ProviderError, ProviderRefused, StyleProvider

log = logging.getLogger(__name__)

URL = "https://api.openai.com/v1/images/edits"
STANDARD = {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"}
REFUSAL_HINTS = ("safety", "moderation", "content_policy", "rejected", "not allowed")


class OpenAIImages(StyleProvider):
    name = "openai"
    cost_per_image_usd = 0.2

    def size_for(self, aspect: float) -> str:
        sizes = {**STANDARD, **(self.cfg.get("sizes") or {})}
        if aspect >= 1.2:
            return sizes["landscape"]
        if aspect <= 0.83:
            return sizes["portrait"]
        return sizes["square"]

    def stylize(self, image: bytes, prompt: str, aspect: float) -> bytes:
        key = env("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        fields = {
            "model": self.cfg.get("model", "gpt-image-2"),
            "prompt": prompt,
            "size": self.size_for(aspect),
            "quality": self.cfg.get("quality", "high"),
            "n": "1",
            **{k: str(v) for k, v in (self.cfg.get("extra") or {}).items()},
        }
        img = self.prep_input(image)
        dropped: set[str] = set()
        for attempt in range(4):
            data = {k: v for k, v in fields.items() if k not in dropped}
            r = session().post(URL, headers={"Authorization": f"Bearer {key}"}, data=data,
                               files={"image": ("source.jpg", img, "image/jpeg")}, timeout=300)
            if r.ok:
                item = r.json()["data"][0]
                if item.get("b64_json"):
                    return base64.b64decode(item["b64_json"])
                if item.get("url"):
                    return session().get(item["url"], timeout=120).content
                raise ProviderError("OpenAI returned no image")
            try:
                err = r.json().get("error", {}) or {}
            except ValueError:
                err = {"message": r.text[:300]}
            msg = (err.get("message") or "").lower()
            code = (err.get("code") or "") or ""
            param = err.get("param")
            if r.status_code == 400 and any(h in msg or h in code for h in REFUSAL_HINTS):
                raise ProviderRefused(err.get("message") or "refused")
            # Unsupported/invalid optional parameter: drop it and retry (keeps config forward-compatible)
            if r.status_code == 400 and param and param in data and param not in ("model", "prompt", "image"):
                log.warning("OpenAI rejected param %r (%s); retrying without it", param, err.get("message"))
                if param == "size":
                    fields["size"] = "auto"
                else:
                    dropped.add(param)
                continue
            raise ProviderError(f"OpenAI {r.status_code}: {err.get('message') or r.text[:300]}")
        raise ProviderError("OpenAI: too many parameter retries")
