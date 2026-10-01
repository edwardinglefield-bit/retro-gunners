"""Turn provider output into the deliverable formats."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


def open_rgb(b: bytes) -> Image.Image:
    return Image.open(io.BytesIO(b)).convert("RGB")


def crop_to_aspect(im: Image.Image, aspect: float, fx: float = 0.5, fy: float = 0.5) -> Image.Image:
    """Largest crop of `im` with width/height == aspect, centred on focus (fx, fy) in 0..1."""
    w, h = im.size
    if abs(w / h - aspect) < 1e-3:
        return im
    if w / h > aspect:          # too wide -> trim sides
        nw = round(h * aspect)
        left = int(min(max(fx * w - nw / 2, 0), w - nw))
        return im.crop((left, 0, left + nw, h))
    nh = round(w / aspect)      # too tall -> trim top/bottom
    top = int(min(max(fy * h - nh / 2, 0), h - nh))
    return im.crop((0, top, w, top + nh))


def resize_long_edge(im: Image.Image, long_edge: int) -> Image.Image:
    w, h = im.size
    s = long_edge / max(w, h)
    return im.resize((round(w * s), round(h * s)), Image.LANCZOS)


@dataclass
class Outputs:
    art: Image.Image          # same aspect ratio as the source photo
    wallpaper: Image.Image    # phone lock/home screen
    story: Image.Image        # 9:16 for IG stories
    feed: Image.Image         # Instagram post, 4:5 portrait
    preview: Image.Image      # side-by-side for the Telegram review message

    def save(self, folder: Path, quality: int = 92) -> dict[str, str]:
        folder.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name in ("art", "wallpaper", "story", "feed", "preview"):
            p = folder / f"{name}.jpg"
            getattr(self, name).save(p, "JPEG", quality=quality if name != "preview" else 85,
                                     optimize=True, progressive=True)
            paths[name] = p.name
        return paths


def build(source_aspect: float, art_bytes: bytes, portrait_bytes: bytes | None, cfg,
          focus_x: float = 0.5) -> Outputs:
    o = cfg.get("output", {}) or {}
    wp_w, wp_h = o.get("wallpaper_size", [1290, 2796])
    st_w, st_h = o.get("story_size", [1080, 1920])
    paper = o.get("paper_color", [236, 228, 211])

    gen = open_rgb(art_bytes)
    art = resize_long_edge(crop_to_aspect(gen, source_aspect, fx=0.5), o.get("art_long_edge", 2048))

    # Portrait base for wallpaper/story: a dedicated recomposition if we have one,
    # otherwise the art itself (portrait sources) cropped around the subject.
    if portrait_bytes:
        base, fx = open_rgb(portrait_bytes), 0.5
    else:
        base, fx = gen, focus_x
    wallpaper = crop_to_aspect(base, wp_w / wp_h, fx=fx).resize((wp_w, wp_h), Image.LANCZOS)
    story = crop_to_aspect(base, st_w / st_h, fx=fx).resize((st_w, st_h), Image.LANCZOS)

    # Instagram post: 4:5 portrait. Wide sources use the tall recomposition (drawn for portrait framing,
    # subject in the lower-middle, quiet sky on top) so nobody gets cropped out; tall ones trim the art.
    fw, fa = o.get("feed_width", 1080), o.get("feed_aspect", 0.8)
    feed = crop_to_aspect(base, fa, fx=fx, fy=0.6 if portrait_bytes else 0.5)
    feed = feed.resize((fw, round(fw / fa)), Image.LANCZOS)

    # Review preview, left to right: art (Threads) | Instagram post | wallpaper (Threads + IG Story)
    ph, gap = 1000, 24
    panels = [im.resize((round(im.size[0] * ph / im.size[1]), ph), Image.LANCZOS) for im in (art, feed, wallpaper)]
    preview = Image.new("RGB", (sum(p.size[0] for p in panels) + gap * (len(panels) + 1), ph + gap * 2), tuple(paper))
    x = gap
    for p in panels:
        preview.paste(p, (x, gap))
        x += p.size[0] + gap
    return Outputs(art=art, wallpaper=wallpaper, story=story, feed=feed, preview=preview)
