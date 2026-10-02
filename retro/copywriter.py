"""Write the opening line of a post in the account's voice (styles/caption_voice.txt)."""
from __future__ import annotations

import json
import logging
import re

from .config import env
from .http import session

log = logging.getLogger(__name__)

PROMPT = """{voice}

Write {n} different captions for one post, each from a different angle (the moment, the history, the feeling).
Each one opens the post: one or two sentences, at most {max_chars} characters, no hashtags.
Use only facts from the notes below. Never invent scores, dates, stats or quotes. If the notes don't name anyone, don't guess a name.

Notes about the picture:
{notes}
{examples}
Return JSON: {{"captions": ["...", "..."]}}"""

HASHTAG = re.compile(r"\s*#\w+")


def notes_for(item: dict) -> str:
    rows = [("Article headline", item.get("article_title")),
            ("Photographer's caption" if item.get("source") != "telegram" else "Owner's note", item.get("caption_src")),
            ("Team", {"men": "Arsenal men", "women": "Arsenal Women"}.get(item.get("team") or "")),
            ("What the picture shows", item.get("headline")),
            ("Main subject", item.get("subject"))]
    return "\n".join(f"- {k}: {str(v).strip()[:400]}" for k, v in rows if v) or "- (no notes: keep it general)"


def tidy(text: str, max_chars: int) -> str:
    text = " ".join(HASHTAG.sub("", str(text)).split()).strip(" \"“”")
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:–-") + "…"
    return text


def write(item: dict, cfg, examples: list[str] = ()) -> list[str]:
    """Caption options, best guess first. Empty when the writer is off or fails (the plain description is used)."""
    ccfg = cfg.get("captions", {}) or {}
    key = env("OPENAI_API_KEY")
    if ccfg.get("writer", "openai") != "openai" or not key:
        return []
    n, max_chars = int(ccfg.get("options", 3)), int(ccfg.get("max_chars", 200))
    own = (item.get("caption_src") or "").strip() if item.get("source") == "telegram" else ""
    shown = "\n".join(f"- {e}" for e in list(examples)[-8:])
    prompt = PROMPT.format(voice=cfg.read_text(ccfg.get("voice_file", "styles/caption_voice.txt")), n=n,
                           max_chars=max_chars, notes=notes_for(item),
                           examples=f"\nCaptions the account owner wrote themselves (match this voice):\n{shown}\n" if shown else "")
    models = list(dict.fromkeys([ccfg.get("model"), cfg.get("curation.model", "gpt-5-mini")]))
    for model in [m for m in models if m]:
        try:
            r = session().post("https://api.openai.com/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                     "response_format": {"type": "json_object"}}, timeout=180)
            if not r.ok:
                raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
            raw = json.loads(r.json()["choices"][0]["message"]["content"]).get("captions") or []
            out = list(dict.fromkeys(t for t in (tidy(c, max_chars) for c in raw) if t))
            if out:
                # a note you sent with your own photo stays available word for word, as the first option
                return ([own] + [c for c in out if c != own])[:n] if own else out[:n]
        except Exception as e:
            log.warning("caption writer (%s) failed: %s", model, e)
    return []
