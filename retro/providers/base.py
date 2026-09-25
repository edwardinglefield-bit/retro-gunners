from __future__ import annotations

import io

from PIL import Image


class ProviderError(RuntimeError):
    pass


class ProviderRefused(ProviderError):
    """The provider's safety system declined the request. Not worth retrying as-is."""


class StyleProvider:
    name = "base"
    cost_per_image_usd = 0.0   # rough, for the daily report only

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def stylize(self, image: bytes, prompt: str, aspect: float) -> bytes:
        """Return a stylised image (PNG/JPEG bytes). `aspect` = width / height wanted."""
        raise NotImplementedError

    # helpers ------------------------------------------------------------
    @staticmethod
    def prep_input(image: bytes, max_edge: int = 2048, fmt: str = "JPEG") -> bytes:
        im = Image.open(io.BytesIO(image))
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, fmt, quality=92)
        return buf.getvalue()
