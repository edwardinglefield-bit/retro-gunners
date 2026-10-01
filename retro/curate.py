"""Pick which photos are worth turning into art."""
from __future__ import annotations

import base64
import io
import json
import logging
import re

import imagehash
from PIL import Image

from .config import env
from .http import download, session

log = logging.getLogger(__name__)

ACTION = re.compile(r"\b(celebrat\w*|scores?|scoring|goal|header|tackl\w*|saves?|shoots?|strike|volley|"
                    r"jumps?|sprints?|dribbl\w*|applaud\w*|embrace\w*|lifts?|trophy|wins?)\b", re.I)
WEAK = re.compile(r"\b(fans? (?:outside|arrive)|general view|mascot|sponsor|ticket|kit launch|boots?|"
                  r"graphic|logo|programme|press conference|interview|arrives?)\b", re.I)
WOMEN_RE = re.compile(r"\bWomen\b|Women's|\bWSL\b|Borehamwood|Meadow Park|Lionesses", re.I)
TEAM_WINDOW = 8   # how many recent previews the team mix is measured over
SUBJECT_RE = re.compile(r"(?::\s*|^)([A-Z][\w'\-\.]+(?: [A-Z][\w'\-\.]+){0,3})(?: and [A-Z][\w'\-\.]+(?: [A-Z][\w'\-\.]+)*)? of Arsenal")

SCORER_PROMPT = """You are the photo editor for a fan account that turns Arsenal FC photos into
mid-century modern geometric poster illustrations (flat colour planes, faceted shapes, 1950s-70s travel-poster feel).

Rate each photo 1-10 for how good the ILLUSTRATION would be:
+ one or two clearly readable Arsenal players/staff, strong pose or emotion (celebration, action, focus), clean silhouette
+ iconic settings (stadium, pitch at dusk, training ground) that simplify into bold shapes
- crowded, cluttered, blurry, tiny subjects, back-of-head, mostly opposition players
- graphics, text overlays, logos as the main subject, product shots, screenshots, press-conference backdrops
Recently featured subjects (prefer variety): {recent}

Return JSON: {{"items":[{{"id":"<id>","score":<1-10>,"subject":"<main person's name or short label>",
"headline":"<caption, max 90 chars, present tense, no hashtags, no emoji>",
"focus_x":<0-1 horizontal centre of the main subject>}}]}} with one entry per photo, same ids."""


def thumb(img_bytes: bytes, edge: int = 512) -> Image.Image:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    im.thumbnail((edge, edge))
    return im


def phash_of(im: Image.Image) -> str:
    return str(imagehash.phash(im))


def phash_close(a: str, b: str, max_dist: int = 8) -> bool:
    try:
        return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b) <= max_dist
    except Exception:
        return False


def team_of(c: dict) -> str:
    """men | women | club | academy. Taxonomy tag from the source when present, else a caption guess."""
    if c.get("team"):
        return c["team"]
    text = " ".join([c.get("caption_src") or "", c.get("article_title") or ""])
    return "women" if WOMEN_RE.search(text) else "men"


def heuristic(c: dict) -> tuple[int, str, str]:
    """(score, subject, headline) from caption text alone."""
    cap = c.get("caption_src") or ""
    s = 5
    if "of Arsenal" in cap:
        s += 2
    if ACTION.search(cap):
        s += 2
    if WEAK.search(cap):
        s -= 3
    if "training" in cap.lower():
        s += 1
    m = SUBJECT_RE.search(cap)
    subject = m.group(1) if m else ""
    headline = c.get("article_title") or ""
    if m:
        # "BOREHAMWOOD, ENGLAND - SEPTEMBER 22: Mariona Caldentey of Arsenal scores ... during the ..."
        tail = cap[m.start(1):]
        tail = re.split(r"\s+(?:during|at|in|before|after|ahead of)\s+the\b|\(Photo", tail)[0]
        headline = tail.replace(" of Arsenal", "").strip().rstrip(".")[:90]
    return max(1, min(10, s)), subject, headline


def _b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def vision_score(batch: list[tuple[dict, Image.Image]], model: str, recent: list[str]) -> dict[str, dict]:
    key = env("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    content: list[dict] = [{"type": "text", "text": SCORER_PROMPT.format(recent=", ".join(recent) or "none")}]
    for c, im in batch:
        content.append({"type": "text", "text": f"id={c['key']} | caption: {(c.get('caption_src') or '')[:300]}"})
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{_b64(im)}", "detail": "low"}})
    r = session().post("https://api.openai.com/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": model, "messages": [{"role": "user", "content": content}],
                             "response_format": {"type": "json_object"}}, timeout=180)
    if not r.ok:
        raise RuntimeError(f"scorer {r.status_code}: {r.text[:300]}")
    text = r.json()["choices"][0]["message"]["content"]
    return {str(i["id"]): i for i in json.loads(text).get("items", [])}


def score_candidates(cands: list[dict], cfg, recent_subjects: list[str], known_hashes: list[str]) -> list[dict]:
    """Download thumbs, de-duplicate near-identical frames, score. Returns candidates with score fields."""
    ccfg = cfg.get("curation", {}) or {}
    # cheap pre-rank so we only pay to vision-score the most promising
    for c in cands:
        c["h_score"], c["subject"], c["headline"] = heuristic(c)
    cands.sort(key=lambda c: (c.get("priority", 0), team_of(c) == "men", c["h_score"], c.get("published_at") or 0),
               reverse=True)
    cands = cands[: ccfg.get("max_scored_per_run", 36)]

    kept: list[tuple[dict, Image.Image]] = []
    hashes = list(known_hashes)
    for c in cands:
        try:
            raw = c.pop("_bytes", None) or download(c.get("fallback_url") or c["image_url"], c.get("image_url"))
            im = thumb(raw)
        except Exception as e:
            log.warning("thumb failed %s: %s", c.get("image_url"), e)
            continue
        c["phash"] = phash_of(im)
        c["aspect"] = round(im.size[0] / im.size[1], 4)
        if not c.get("priority") and any(phash_close(c["phash"], h) for h in hashes):
            c["score"], c["reject"] = 0, "near-duplicate"
            continue
        hashes.append(c["phash"])
        kept.append((c, im))

    use_vision = ccfg.get("scorer", "openai") == "openai" and env("OPENAI_API_KEY")
    for i in range(0, len(kept), 8):
        batch = kept[i:i + 8]
        results: dict[str, dict] = {}
        if use_vision:
            try:
                results = vision_score(batch, ccfg.get("model", "gpt-5-mini"), recent_subjects)
            except Exception as e:
                log.warning("vision scoring failed, using heuristic: %s", e)
        for c, _ in batch:
            r = results.get(c["key"])
            if r:
                c["score"] = int(r.get("score", c["h_score"]))
                c["subject"] = (r.get("subject") or c["subject"])[:60]
                c["headline"] = (r.get("headline") or c["headline"])[:120]
                try:
                    c["focus_x"] = float(min(1, max(0, float(r.get("focus_x", 0.5)))))
                except (TypeError, ValueError):
                    c["focus_x"] = 0.5
            else:
                c["score"] = c["h_score"]
                c.setdefault("focus_x", 0.5)
            if c.get("priority"):
                c["score"] = 10   # your own picks always go through
    return [c for c, _ in kept] + [c for c in cands if c.get("reject")]


def choose(scored: list[dict], cfg, recent_subjects: list[str], recent_teams: list[str] = ()) -> list[dict]:
    """Best first. Your own photos lead; then men's team photos, with women's team photos
    mixed in up to `curation.women_share` of recent previews (never above it)."""
    ccfg = cfg.get("curation", {}) or {}
    min_score = ccfg.get("min_score", 7)
    share = float(ccfg.get("women_share", 0.25))
    avoid = {s.lower() for s in recent_subjects[: ccfg.get("avoid_repeat_subject_last_n", 2)] if s}
    ok = [c for c in scored if c.get("score", 0) >= min_score and not c.get("reject")]
    ok.sort(key=lambda c: (c.get("priority", 0), (c.get("subject") or "").lower() not in avoid,
                           c["score"], c.get("published_at") or 0), reverse=True)
    picks = [c for c in ok if c.get("priority")]
    women = [c for c in ok if not c.get("priority") and team_of(c) == "women"]
    others = [c for c in ok if not c.get("priority") and team_of(c) != "women"]
    others.sort(key=lambda c: team_of(c) == "men", reverse=True)   # stable: keeps the ranking within each group
    window = list(recent_teams)[:TEAM_WINDOW]
    while women or others:
        women_ok = women and (window.count("women") + 1) / (len(window) + 1) <= share
        if women_ok and (window.count("women") / max(len(window), 1) < share or not others):
            c = women.pop(0)
        elif others:
            c = others.pop(0)
        else:
            break
        picks.append(c)
        window = [team_of(c)] + window[:TEAM_WINDOW - 1]
    return picks
