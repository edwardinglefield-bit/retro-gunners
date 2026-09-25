"""Google Gemini image models ("Nano Banana" family) via the REST API."""
from __future__ import annotations

import base64

from ..config import env
from ..http import session
from .base import ProviderError, ProviderRefused, StyleProvider

RATIOS = {"1:1": 1.0, "2:3": 2 / 3, "3:2": 1.5, "3:4": 0.75, "4:3": 4 / 3, "4:5": 0.8,
          "5:4": 1.25, "9:16": 9 / 16, "16:9": 16 / 9}


class GeminiImages(StyleProvider):
    name = "gemini"
    cost_per_image_usd = 0.04

    def stylize(self, image: bytes, prompt: str, aspect: float) -> bytes:
        key = env("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        model = self.cfg.get("model", "gemini-2.5-flash-image")
        ratio = min(RATIOS, key=lambda k: abs(RATIOS[k] - aspect))
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(self.prep_input(image)).decode()}},
            ]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ratio}},
        }
        r = session().post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           params={"key": key}, json=body, timeout=300)
        if not r.ok:
            raise ProviderError(f"Gemini {r.status_code}: {r.text[:300]}")
        j = r.json()
        for cand in j.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                blob = part.get("inlineData") or part.get("inline_data")
                if blob and blob.get("data"):
                    return base64.b64decode(blob["data"])
            if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY"):
                raise ProviderRefused(cand["finishReason"])
        raise ProviderError("Gemini returned no image")
