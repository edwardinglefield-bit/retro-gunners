"""Per-platform captions."""
from __future__ import annotations

LIMITS = {"x": 280, "threads": 500, "instagram": 2200}


def credit_line(item: dict) -> str:
    who = item.get("credit")
    return f"Original photo: {who}." if who else "Original photo: Arsenal FC."


def options(item: dict) -> list[str]:
    """Written caption options still on offer (none once you've replied with your own text)."""
    return [] if item.get("caption_override") else list(item.get("captions") or [])


def lead(item: dict) -> str:
    """The line that opens the post: your own text, else the option you picked, else the plain description."""
    opts = options(item)
    if opts:
        return opts[min(int(item.get("caption_pick") or 0), len(opts) - 1)].strip()
    note = item.get("caption_src") if item.get("source") == "telegram" else ""   # sent along with your photo
    return (item.get("caption_override") or note or item.get("headline") or "").strip()


def build(item: dict, cfg, platform: str) -> str:
    brand = cfg.get("brand", {}) or {}
    headline = lead(item)
    tags = brand.get("hashtags", "#Arsenal #COYG")
    if platform == "x":
        short_tags = " ".join(tags.split()[:2])
        parts = [headline, "Phone wallpaper in the 2nd image.", credit_line(item), short_tags]
    elif platform == "threads":
        parts = [headline, "Swipe for the phone wallpaper.", credit_line(item) + " Fan-made illustration.", tags]
    else:  # instagram
        parts = [headline, "",
                 f"{brand.get('name', '')} · mid-century print series".strip(" ·"),
                 "Phone wallpaper version is in our story.", "",
                 credit_line(item) + " " + brand.get("credit", "").replace("Original photo: Arsenal FC / Getty Images. ", ""),
                 "", tags]
    text = "\n".join(p for p in parts if p is not None).strip()
    text = "\n".join(line.rstrip() for line in text.splitlines())
    limit = LIMITS.get(platform, 2200)
    if len(text) > limit:   # drop from the middle-out: keep headline + credit
        text = "\n".join([headline[: limit - len(credit_line(item)) - 2], credit_line(item)])[:limit]
    return text
