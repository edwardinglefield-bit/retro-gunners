"""Free offline stand-in: a flat-colour poster filter. Only for testing the plumbing."""
from __future__ import annotations

import io
import random

from PIL import Image, ImageFilter

from .base import StyleProvider

PALETTE = [(236, 228, 211), (199, 84, 60), (214, 164, 72), (112, 140, 160), (60, 72, 84),
           (38, 38, 40), (170, 60, 48), (150, 160, 130)]


class LocalPoster(StyleProvider):
    name = "local"

    def stylize(self, image: bytes, prompt: str, aspect: float) -> bytes:
        im = Image.open(io.BytesIO(image)).convert("RGB")
        # match requested aspect with a centre crop, like a real provider would recompose
        w, h = im.size
        if w / h > aspect:
            nw = int(h * aspect)
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        elif w / h < aspect:
            nh = int(w / aspect)
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im.thumbnail((1024, 1024))
        im = im.filter(ImageFilter.ModeFilter(9)).filter(ImageFilter.SMOOTH_MORE)
        pal = Image.new("P", (1, 1))
        flat = [c for rgb in PALETTE for c in rgb]
        pal.putpalette(flat + [0] * (768 - len(flat)))
        im = im.quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
        rnd = random.Random(42)
        px = im.load()
        for _ in range(im.size[0] * im.size[1] // 12):
            x, y = rnd.randrange(im.size[0]), rnd.randrange(im.size[1])
            r, g, b = px[x, y]
            d = rnd.randint(-10, 10)
            px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
