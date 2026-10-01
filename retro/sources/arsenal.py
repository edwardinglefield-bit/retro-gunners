"""Discover fresh photos published by Arsenal on arsenal.com.

How arsenal.com serves photos (checked Sept 2026):
  * Article listing comes from a public GraphQL endpoint (GetArticlesByTaxonomy).
  * Gallery articles embed {"type":"GALLERY","id":"5620"} blocks in their __NEXT_DATA__.
  * GetSingleGallery(id) returns every image with its full-resolution "original" rendition
    and the Getty caption, which includes the photographer credit.
  * Asset renditions: assets.arsenal.com/prod/images/<rendition>/<key>.<ext>;
    `square_1000_1000` is fit-inside (keeps the real aspect ratio), most others are crops.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from ..http import session

log = logging.getLogger(__name__)

GRAPHQL = "https://afc-prd.graph.arsenal.com/graphql"
SITE = "https://www.arsenal.com"
ARTICLE_TYPES = "40,37,22,29,28"   # the set the website's own news page requests

Q_ARTICLES = """query GetArticlesByTaxonomy($taxonomy: String = "", $pageNumber: Float = 1, $pageSize: Float = 50,
 $articleTypes: String = "", $platform: String = "web") {
  getArticlesByTaxonomy(taxonomy: $taxonomy, pageNumber: $pageNumber, pageSize: $pageSize,
    articleTypes: $articleTypes, excludedArticles: [], sortField: "", sort: "", platform: $platform) {
    total
    articles { articleId title path taxonomies promoImage publicationDate }
  }
}"""

Q_GALLERY = """query GetSingleGallery($id: String!) {
  singleGallery(id: $id) {
    id title createDate
    images { filename caption description headline originalUrl list { width height url type } }
  }
}"""

ASSET_RE = re.compile(r"https://assets\.arsenal\.com/prod/images/([a-z0-9_]+)/([0-9a-f]{12})-([^/?#]+?)\.(jpe?g|webp|png)")
GALLERY_BLOCK_RE = re.compile(r'"type"\s*:\s*"GALLERY"\s*,\s*"id"\s*:\s*"(\d+)"')
CREDIT_RE = re.compile(r"\(Photo by ([^)]+)\)")

# Taxonomies that are never useful as art sources
SKIP_TAXONOMIES = {"ticket information", "quiz", "quizzes", "gamification", "retail", "competitions", "partners"}


def fix_mojibake(s: str) -> str:
    """Getty captions sometimes arrive double-encoded (teamâs)."""
    if not s or "â" not in s and "Ã" not in s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s.replace("â", "'")


def gql(query: str, variables: dict, op: str) -> dict:
    r = session().post(GRAPHQL, json={"operationName": op, "query": query, "variables": variables},
                       headers={"x-arsenal-request-source": "Arsenal-Web", "Origin": SITE, "Referer": SITE + "/"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(f"GraphQL {op} errors: {j['errors']}")
    return j["data"]


def recent_articles(limit: int = 40, page_size: int = 20, stop_before: datetime | None = None) -> list[dict]:
    """Newest first. NB: pageNumber is 0-indexed (page 1 skips the newest `pageSize` articles)."""
    out: list[dict] = []
    page = 0
    while len(out) < limit:
        data = gql(Q_ARTICLES, {"pageNumber": page, "pageSize": page_size, "articleTypes": ARTICLE_TYPES},
                   "GetArticlesByTaxonomy")
        batch = data["getArticlesByTaxonomy"]["articles"] or []
        out.extend(batch)
        oldest = min((parse_date(a.get("publicationDate")) for a in batch if a.get("publicationDate")), default=None)
        if len(batch) < page_size or (stop_before and oldest and oldest < stop_before):
            break
        page += 1
    return out[:limit]


def parse_date(s) -> datetime | None:
    if not s:
        return None
    try:
        if str(s).isdigit():
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def gallery_ids_for(path: str) -> list[str]:
    r = session().get(SITE + path, timeout=30)
    r.raise_for_status()
    return list(dict.fromkeys(GALLERY_BLOCK_RE.findall(r.text)))


def gallery_images(gallery_id: str) -> list[dict]:
    g = gql(Q_GALLERY, {"id": gallery_id}, "GetSingleGallery")["singleGallery"] or {}
    return g.get("images") or []


def asset_key(url: str) -> str:
    m = ASSET_RE.search(url or "")
    if m:
        return m.group(2)
    return hashlib.sha1((url or "").encode()).hexdigest()[:12]


def fit_url(url: str) -> str:
    """Rewrite any rendition URL to the aspect-preserving 1000px version."""
    return ASSET_RE.sub(lambda m: f"https://assets.arsenal.com/prod/images/square_1000_1000/{m.group(2)}-{m.group(3)}.jpg", url)


def team_of_taxonomies(taxes: set[str]) -> str:
    """arsenal.com tags every team article "Men" or "Women"; anything else is club/academy content."""
    if "women" in taxes:
        return "women"
    if "men" in taxes:
        return "men"
    return "academy" if "academy" in taxes else "club"


def is_galleryish(article: dict, keywords: list[str]) -> bool:
    hay = " ".join([article.get("title", "")] + list(article.get("taxonomies") or [])).lower()
    return any(k in hay for k in keywords)


def discover(cfg) -> list[dict]:
    """Return candidate dicts (not yet in state)."""
    scfg = cfg.get("sources.arsenal_web", {}) or {}
    lookback = timedelta(hours=scfg.get("lookback_hours", 72))
    keywords = [k.lower() for k in scfg.get("gallery_keywords", ["gallery", "photos"])]
    cutoff = datetime.now(timezone.utc) - lookback

    out: list[dict] = []
    articles = recent_articles(scfg.get("max_articles", 60), stop_before=cutoff)
    for a in articles:
        pub = parse_date(a.get("publicationDate"))
        if pub and pub < cutoff:
            continue
        taxes = {t.lower() for t in (a.get("taxonomies") or [])}
        if taxes & SKIP_TAXONOMIES:
            continue
        article_url = SITE + a["path"]
        base = {
            "source": "arsenal_web",
            "article_url": article_url,
            "article_title": a.get("title", ""),
            "published_at": pub.timestamp() if pub else None,
            "team": team_of_taxonomies(taxes),
        }
        # 1) Every article's hero image is a candidate (often the best shot of the day).
        if a.get("promoImage"):
            key = asset_key(a["promoImage"])
            out.append({**base, "key": key, "image_url": fit_url(a["promoImage"]),
                        "caption_src": a.get("title", ""), "credit": ""})
        # 2) Galleries: every photo, full resolution, with Getty caption.
        if is_galleryish(a, keywords):
            try:
                gids = gallery_ids_for(a["path"])
            except Exception as e:  # blocked / changed markup: promo image still counts
                log.warning("gallery lookup failed for %s: %s", a["path"], e)
                continue
            for gid in gids:
                try:
                    imgs = gallery_images(gid)
                except Exception as e:
                    log.warning("gallery %s failed: %s", gid, e)
                    continue
                for im in imgs:
                    renditions = {x["type"]: x for x in (im.get("list") or [])}
                    orig = renditions.get("original", {}).get("url") or im.get("originalUrl")
                    if not orig:
                        continue
                    cap = fix_mojibake(im.get("caption") or im.get("description") or "")
                    m = CREDIT_RE.search(cap)
                    out.append({**base, "key": asset_key(orig), "image_url": orig,
                                "fallback_url": fit_url(orig),
                                "caption_src": cap or im.get("filename", ""),
                                "credit": m.group(1) if m else "",
                                "width": int(renditions.get("original", {}).get("width") or 0),
                                "height": int(renditions.get("original", {}).get("height") or 0)})
    # de-dupe by asset key, keep the richest record (gallery > promo)
    best: dict[str, dict] = {}
    for c in out:
        k = c["key"]
        if k not in best or len(c.get("caption_src", "")) > len(best[k].get("caption_src", "")):
            best[k] = c
    log.info("arsenal_web: %d articles scanned, %d unique photos", len(articles), len(best))
    return list(best.values())


if __name__ == "__main__":  # quick manual check: python -m retro.sources.arsenal
    logging.basicConfig(level=logging.INFO)
    from .. import config
    res = discover(config.load())
    print(json.dumps(res[:5], indent=1))
