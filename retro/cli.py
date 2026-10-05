"""Entry points.

  python -m retro tick            # what the scheduled workflow runs
  python -m retro tick --discover # force a discovery pass now
  python -m retro doctor          # check every integration, report to stdout + Telegram
  python -m retro stylize in.jpg  # try the style on one local photo (writes ./out/)
  python -m retro sources         # list what arsenal.com currently offers (no API spend)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import config, render
from .state import State


def _pipeline(cfg):
    from .pipeline import Pipeline
    return Pipeline(cfg, State(cfg.data_dir / "state.json"))


def cmd_tick(args):
    cfg = config.load(args.config)
    p = _pipeline(cfg)
    try:
        p.tick(force_discover=args.discover)
    finally:
        changed = p.state.save()
        print(f"state {'changed' if changed else 'unchanged'}")


def cmd_doctor(args):
    cfg = config.load(args.config)
    from .config import env
    from .http import session
    from .pipeline import Pipeline
    p = Pipeline(cfg, State(cfg.data_dir / "state.json"))
    lines = []

    def check(label, fn):
        try:
            lines.append(f"✅ {label}: {fn()}")
        except Exception as e:
            lines.append(f"❌ {label}: {str(e)[:200]}")

    def src():
        from .sources import arsenal
        arts = arsenal.recent_articles(10)
        gids = []
        for a in arts:
            if arsenal.is_galleryish(a, ["gallery", "photos"]):
                gids = arsenal.gallery_ids_for(a["path"])
                break
        return f"{len(arts)} articles; gallery page readable: {'yes' if gids else 'no gallery in latest 10 / blocked'}"

    check("arsenal.com", src)
    check("OpenAI key", lambda: "set" if env("OPENAI_API_KEY") and session().get(
        "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"},
        timeout=30).ok else (_ for _ in ()).throw(RuntimeError("missing or invalid")))
    def tg():
        if not p.bot.enabled:
            raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
        me = p.bot._call("getMe")
        if not me:
            raise RuntimeError("Telegram rejected the bot token: re-run scripts/setup.sh and paste it again")
        hook = p.bot._call("getWebhookInfo") or {}
        if hook.get("url"):   # a webhook steals every update from getUpdates
            raise RuntimeError(f"a webhook is set ({hook['url'][:60]}), so taps never reach the pipeline")
        return "@" + me.get("username", "?") + f", {hook.get('pending_update_count', 0)} taps/messages waiting" + (
            "" if p.bot.owner else " (TELEGRAM_OWNER_ID not set: message the bot, it will tell you your id)")

    check("Telegram", tg)
    check("STATE_KEY", lambda: "set" if env("STATE_KEY") else (_ for _ in ()).throw(RuntimeError("missing")))
    from .publishers import Instagram, Threads, X
    for pub in (X(cfg, p.vault), Threads(cfg, p.vault), Instagram(cfg, p.vault)):
        if pub.configured():
            check(pub.name, pub.check)
        else:
            lines.append(f"➖ {pub.name}: not configured (optional)")
    report = "Retro Gunners doctor\n" + "\n".join(lines)
    print(report)
    p.bot.text(report)
    p.state.save()


def cmd_stylize(args):
    cfg = config.load(args.config)
    if args.provider:
        cfg.raw.setdefault("style", {})["provider"] = args.provider
    from .providers import get_provider
    prov = get_provider(cfg)
    src = Path(args.image).read_bytes()
    im = render.open_rgb(src)
    aspect = im.size[0] / im.size[1]
    art = prov.stylize(src, cfg.read_text(cfg.get("style.prompt_file")), aspect)
    portrait = prov.stylize(art, cfg.read_text(cfg.get("style.wallpaper_prompt_file")), 9 / 16) if aspect > 0.75 else None
    outs = render.build(aspect, art, portrait, cfg)
    paths = outs.save(Path(args.out))
    print(json.dumps({k: str(Path(args.out) / v) for k, v in paths.items()}, indent=1))


def cmd_sources(args):
    cfg = config.load(args.config)
    from .sources import arsenal
    from .curate import heuristic
    res = arsenal.discover(cfg)
    for c in sorted(res, key=lambda c: heuristic(c)[0], reverse=True)[: args.n]:
        s, subj, head = heuristic(c)
        print(f"[{s}] {subj or '-':<22} {head[:70]:<70} {c['image_url']}")
    print(f"{len(res)} photos found")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="retro")
    ap.add_argument("--config")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick"); t.add_argument("--discover", action="store_true"); t.set_defaults(fn=cmd_tick)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("stylize"); s.add_argument("image"); s.add_argument("--out", default="out")
    s.add_argument("--provider"); s.set_defaults(fn=cmd_stylize)
    so = sub.add_parser("sources"); so.add_argument("-n", type=int, default=25); so.set_defaults(fn=cmd_sources)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
