"""One `tick` = read Telegram -> publish anything due -> redo requests -> discover + generate if the queue is low."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import captions, copywriter, curate, render
from .hosting import Hosting
from .http import download
from .providers import ProviderRefused, get_provider
from .publishers import enabled_publishers
from .publishers.base import Media
from .sources import arsenal
from .state import State, now_ts
from .telegram import Telegram
from .vault import Vault

log = logging.getLogger(__name__)
MAX_PUBLISH_ATTEMPTS = 3
STALE_GENERATING_SECONDS = 30 * 60
TICK_BUDGET_SECONDS = 15 * 60   # stop starting new generations after this (job limit is 25 min)

HELP = ("Commands:\n/status – queue + budget\n/run – look for new photos now\n/pause, /resume – stop/start everything\n"
        "Send me any photo to have it illustrated next; add a note (who, when) and the captions are written from it.\n"
        "Reply to a preview with text to use your own caption.")


class Pipeline:
    def __init__(self, cfg, state: State, bot: Telegram | None = None, provider=None, publishers=None):
        self.cfg = cfg
        self.state = state
        self.bot = bot or Telegram()
        self.vault = Vault(state)
        self._provider = provider
        self.publishers = publishers if publishers is not None else enabled_publishers(cfg, self.vault)
        self.hosting = Hosting(cfg)
        self.tz = ZoneInfo(cfg.get("timezone", "Europe/London"))
        self.force_discover = False
        self.top_up = False
        self.started = now_ts()

    # ------------------------------------------------------------------ utils
    @property
    def provider(self):
        if self._provider is None:
            self._provider = get_provider(self.cfg)
        return self._provider

    def c(self, key, default=None):
        return self.cfg.get(key, default)

    def notify_once(self, key: str, text: str, every_hours: float = 12):
        seen = self.state.d.setdefault("notified", {})
        if now_ts() - seen.get(key, 0) < every_hours * 3600:
            return
        seen[key] = now_ts()
        self.state.dirty = True
        log.warning(text)
        self.bot.text(text)

    def gens_left(self) -> int:
        """Paid image calls left in the rolling 24h budget."""
        return self.c("schedule.max_generations_per_day", 6) - self.state.count_since("generations", 86400)

    def time_left(self) -> bool:
        return now_ts() - self.started < TICK_BUDGET_SECONDS

    def stylize(self, image: bytes, prompt: str, aspect: float) -> bytes:
        """Every provider call counts against the budget, success or not, and is saved immediately."""
        try:
            return self.provider.stylize(image, prompt, aspect)
        finally:
            self.state.log("generations")
            self.state.save()

    def review_mode(self) -> str:
        # "telegram" (default): nothing is posted until you tap Post. "auto": post everything that clears min_score.
        return "auto" if self.c("review.mode", "telegram") == "auto" else "telegram"

    # ------------------------------------------------------------------ tick
    def tick(self, force_discover: bool = False):
        self.force_discover = self.top_up = force_discover
        self.started = now_ts()
        self.recover_stale()
        self.handle_updates()
        if self.state["paused"]:
            log.info("paused")
            return
        self.refresh_tokens()
        self.caption_waiting_previews()
        self.resend_missing_previews()
        self.publish_due()
        self.process_redos()
        if self.discovery_due():
            self.discover_and_generate()
        self.cleanup()

    def recover_stale(self):
        """A run killed mid-generation leaves items in 'generating'; put them back in line."""
        for it in self.state.by_status("generating"):
            if now_ts() - it.get("updated_at", it.get("created_at", 0)) > STALE_GENERATING_SECONDS:
                self.state.update(it["id"], status="redo" if it.get("media_dir") else "candidate")

    def resend_missing_previews(self):
        if self.review_mode() != "telegram" or not (self.bot.enabled and self.bot.owner):
            return
        for it in self.state.by_status("pending"):
            if not it.get("tg_message_id") and it.get("media_dir"):
                self.send_review(it)

    def send_review(self, it: dict):
        msg = self.bot.send_preview(it, self.cfg.data_dir / it["media_dir"] / it["files"]["preview"],
                                    self.review_caption(it))
        if msg:
            self.state.update(it["id"], tg_message_id=msg)

    # ------------------------------------------------------------------ telegram
    def handle_updates(self):
        if not self.bot.enabled:
            return
        ups = self.bot.updates(self.state["telegram_offset"])
        log.info("telegram: %d new updates", len(ups))
        for u in ups:
            self.state.set("telegram_offset", u["update_id"] + 1)
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception as e:  # never let one bad update block the queue
                log.exception("update failed: %s", e)

    def on_callback(self, cb: dict):
        chat = (cb.get("message") or {}).get("chat", {}).get("id") or cb["from"]["id"]
        if not (self.bot.is_owner(chat) and self.bot.is_owner(cb.get("from", {}).get("id"))):
            return
        action, item_id, gen, pick = ((cb.get("data") or "").split(":") + ["", "", ""])[:4]
        it = self.state.items.get(item_id)
        if not it or it["status"] != "pending" or (gen and str(it.get("gen")) != gen):
            self.bot.answer(cb["id"], "Already handled, or an older version")
            return
        msg_id = it.get("tg_message_id")
        if action == "a" and pick.isdigit():
            self.state.update(item_id, caption_pick=int(pick))
        head = captions.lead(it)
        if action == "a":
            self.state.update(item_id, status="approved", approved_at=now_ts())
            self.bot.answer(cb["id"], "Queued for the next slot")
            self.bot.edit_caption(msg_id, f"✅ Approved – queued\n{head}")
        elif action == "r":
            self.state.update(item_id, status="redo")
            self.bot.answer(cb["id"], "Redoing")
            self.bot.edit_caption(msg_id, f"🔁 Redo requested\n{head}")
        elif action == "s":
            self.state.update(item_id, status="skipped")
            self.bot.answer(cb["id"], "Skipped")
            self.bot.edit_caption(msg_id, f"✖ Skipped\n{head}")

    def on_message(self, m: dict):
        chat = m["chat"]["id"]
        if not self.bot.owner:
            # first-run helper: tell whoever messages the bot their id so it can be saved as a secret
            self.bot._call("sendMessage", chat_id=chat,
                           text=f"Your chat id is {chat}. Save it as the TELEGRAM_OWNER_ID secret.")
            return
        if not self.bot.is_owner(chat):
            return
        text = (m.get("text") or "").strip()
        photo = (m.get("photo") or [None])[-1]
        doc = m.get("document") if (m.get("document") or {}).get("mime_type", "").startswith("image/") else None
        if photo or doc:
            f = photo or doc
            key = "tg-" + f["file_unique_id"]
            if key in self.state.items and self.state.items[key]["status"] != "failed":
                self.bot.text("I already have that one.", reply_to=m["message_id"])
                return
            data = self.bot.download_file(f["file_id"])
            try:
                render.open_rgb(data or b"").size
            except Exception:
                self.bot.text("Couldn't read that image. Send it as a normal photo (JPEG/PNG).", reply_to=m["message_id"])
                return
            inbox = self.cfg.data_dir / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"{key}.jpg").write_bytes(data)
            cap = (m.get("caption") or "").strip()
            self.state.put({"id": key, "key": key, "source": "telegram", "local_path": f"inbox/{key}.jpg",
                            "priority": 1, "caption_src": cap, "headline": cap,
                            "published_at": now_ts(), "credit": ""})
            self.force_discover = True
            self.bot.text("Got it. It'll be illustrated on this run (budget permitting).", reply_to=m["message_id"])
            return
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/pause":
                self.state.set("paused", True)
                self.bot.text("Paused. /resume to restart.")
            elif cmd == "/resume":
                self.state.set("paused", False)
                self.bot.text("Resumed.")
            elif cmd == "/run":
                self.force_discover = self.top_up = True
                self.bot.text("Looking for new photos now; up to 2 new previews coming (budget permitting).")
            elif cmd == "/status":
                self.bot.text(self.status_text())
            else:
                self.bot.text(HELP)
            return
        reply = (m.get("reply_to_message") or {}).get("message_id")
        if reply and text:
            for it in self.state.items.values():
                if it.get("tg_message_id") == reply:
                    self.state.update(it["id"], caption_override=text)
                    # your own captions teach the writer your voice
                    self.state.set("voice_examples", (self.state.d.get("voice_examples") or [])[-11:] + [text])
                    if it["status"] == "pending":
                        self.bot.edit_caption(reply, self.review_caption(it), keyboard=self.bot.keyboard_for(it))
                    self.bot.text("Caption updated.", reply_to=m["message_id"])
                    return
        self.bot.text(HELP)

    def status_text(self) -> str:
        s = self.state
        pubs = ", ".join(p.name for p in self.publishers) or "none configured"
        est = self.state.count_since("generations", 86400) * getattr(self.provider, "cost_per_image_usd", 0.2) * 1.5
        return (f"Waiting for you: {len(s.by_status('pending'))}\nApproved, queued: {len(s.by_status('approved'))}\n"
                f"Posted last 24h: {s.count_since('posts', 86400)}\n"
                f"Generations last 24h: {s.count_since('generations', 86400)} (≈${est:.2f}), left: {max(0, self.gens_left())}\n"
                f"Platforms: {pubs}\nPaused: {s['paused']}")

    # ------------------------------------------------------------------ publishing
    def quiet_now(self, now: float | None = None) -> bool:
        hour = datetime.fromtimestamp(now or now_ts(), self.tz).hour
        q0, q1 = self.c("schedule.quiet_hours", [23, 7])
        return (q0 > q1 and (hour >= q0 or hour < q1)) or (q0 < q1 and q0 <= hour < q1)

    def slot_open(self, now: float | None = None) -> bool:
        now = now or now_ts()
        local = datetime.fromtimestamp(now, self.tz)
        if self.quiet_now(now):
            return False
        today = [t for t in self.state["posts"] if datetime.fromtimestamp(t, self.tz).date() == local.date()]
        if len(today) >= self.c("schedule.posts_per_day", 3):
            return False
        slots = sorted(dtime.fromisoformat(s) for s in self.c("schedule.post_slots", ["08:30", "13:00", "19:30"]))
        if len(today) < len(slots) and local.time() < slots[len(today)]:
            return False
        last = max(self.state["posts"], default=0)
        return now - last >= self.c("schedule.min_gap_hours", 3) * 3600

    def media_for(self, it: dict) -> Media:
        folder = self.cfg.data_dir / it["media_dir"]
        paths = {k: folder / v for k, v in it["files"].items()}
        urls = {k: self.hosting.url(f"{it['media_dir']}/{v}") for k, v in it["files"].items()}
        return Media(paths=paths, urls=urls)

    def publish_due(self):
        queue = sorted(self.state.by_status("approved"), key=lambda i: i.get("approved_at", 0))
        if not queue:
            return
        # A post that's already live on one platform finishes on the others right away (not at night);
        # anything else waits for its slot.
        if self.quiet_now() if queue[0]["posted"] else not self.slot_open():
            return
        if not self.publishers:
            self.notify_once("no-publishers", "An item is approved but no social accounts are configured yet "
                                              "(see SETUP.md). It'll wait in the queue.", 24)
            return
        it = queue[0]
        media = self.media_for(it)
        if any(p.needs_public_urls() for p in self.publishers) and not self.hosting.is_live(
                f"{it['media_dir']}/{it['files']['feed']}"):
            log.info("media for %s not publicly reachable yet; next run", it["id"])
            if now_ts() - it.get("approved_at", now_ts()) > 3600:
                self.notify_once("not-live", "Images aren't reachable at their public URL, so Instagram/Threads "
                                             "can't fetch them. Is the GitHub repo public?", 24)
            return
        attempts = it.setdefault("attempts", {})
        for p in self.publishers:
            if p.name in it["posted"] or attempts.get(p.name, 0) >= MAX_PUBLISH_ATTEMPTS:
                continue
            progress = it.setdefault("progress", {}).setdefault(p.name, {})
            try:
                res = p.publish(media, captions.build(it, self.cfg, p.name), progress)
                it["posted"][p.name] = {**res, "at": now_ts()}
                log.info("posted %s to %s: %s", it["id"], p.name, res.get("url"))
                if not it.get("counted"):
                    it["counted"] = True
                    self.state.log("posts")
                self.state.dirty = True
                self.state.save()
            except Exception as e:
                attempts[p.name] = attempts.get(p.name, 0) + 1
                it["errors"].append(f"{p.name}: {e}"[:400])
                self.notify_once(f"pub-{p.name}-{it['id']}", f"⚠️ {p.name} failed ({attempts[p.name]}/"
                                 f"{MAX_PUBLISH_ATTEMPTS}): {str(e)[:300]}", 1)
        self.state.dirty = True
        self.state.save()
        done = all(p.name in it["posted"] or attempts.get(p.name, 0) >= MAX_PUBLISH_ATTEMPTS
                   for p in self.publishers)
        if done:
            self.state.update(it["id"], status="posted" if it["posted"] else "failed", posted_at=now_ts())
            links = "\n".join(f"{k}: {v.get('url') or v.get('id')}" for k, v in it["posted"].items())
            self.bot.edit_caption(it.get("tg_message_id"),
                                  f"📣 Posted\n{captions.lead(it)}\n{links}"
                                  if it["posted"] else "❌ Publishing failed on every platform")

    def refresh_tokens(self):
        for p in self.publishers:
            try:
                p.refresh_token()
            except Exception as e:
                self.notify_once(f"refresh-{p.name}", f"⚠️ Couldn't refresh the {p.name} token: {e}", 24)

    # ------------------------------------------------------------------ generation
    def discovery_due(self) -> bool:
        if self.force_discover or self.state.by_status("candidate") and any(
                i.get("priority") for i in self.state.by_status("candidate")):
            return True
        waiting = len(self.state.by_status("pending", "generating", "redo"))
        if waiting >= self.c("schedule.pending_review_target", 2):
            return False
        return now_ts() - self.state["last_discovery"] >= self.c("schedule.discovery_every_hours", 2) * 3600

    def discover_and_generate(self):
        self.state.set("last_discovery", now_ts())
        lookback = self.c("sources.arsenal_web.lookback_hours", 72) * 3600
        new: list[dict] = []
        if self.c("sources.arsenal_web.enabled", True):
            try:
                found = arsenal.discover(self.cfg)
                for c in found:   # keep queued items' team tag in step with the source
                    old = self.state.items.get(c["key"])
                    if old and c.get("team") and old.get("team") != c["team"]:
                        self.state.update(c["key"], team=c["team"])
                new = [c for c in found if not self.state.is_seen(c["key"]) and c.get("team") != "away"]
            except Exception as e:
                self.notify_once("src-arsenal", f"⚠️ arsenal.com lookup failed: {e}. You can still send me photos.", 12)
        # your own photos arrive as unscored candidates
        inbox = [i for i in self.state.by_status("candidate") if i.get("priority") and "score" not in i]
        for i in inbox:
            i["_bytes"] = (self.cfg.data_dir / i["local_path"]).read_bytes()
        recent = self.state.recent_subjects(5)
        known = [i["phash"] for i in self.state.items.values() if i.get("phash")]
        scored = curate.score_candidates(new + inbox, self.cfg, recent, known) if (new or inbox) else []
        for i in inbox:
            i.pop("_bytes", None)
            if "phash" not in i:   # couldn't be read
                self.state.update(i["id"], status="failed")
        min_score = self.c("curation.min_score", 7)
        for c in scored:
            c.pop("_bytes", None)
            self.state.mark_seen(c["key"])
            keep = c.get("priority") or (c.get("score", 0) >= min_score and not c.get("reject"))
            if keep:
                c["id"] = c.get("id") or c["key"]
                c["status"] = "candidate"
                if c["id"] in self.state.items:
                    self.state.update(c["id"], **c)
                else:
                    self.state.put(c)
        pool = [i for i in self.state.by_status("candidate")
                if i.get("priority") or (i.get("published_at") or 0) >= now_ts() - lookback]
        recent_teams = [curate.team_of(i) for i in self.state.recent_generated(curate.TEAM_WINDOW)]
        chosen = curate.choose(pool, self.cfg, recent, recent_teams)
        waiting = len(self.state.by_status("pending", "generating"))
        target = self.c("schedule.pending_review_target", 2)
        need = target if self.top_up else max(0, target - waiting)   # /run asks for fresh previews regardless
        todo = [c for c in chosen if c.get("priority")] + [c for c in chosen if not c.get("priority")][:need]
        for c in todo:
            if not self.time_left():
                break
            if self.gens_left() <= 0:
                self.notify_once("budget", "Daily generation budget reached; picking up again tomorrow.", 12)
                break
            self.generate(self.state.items[c["id"]])

    def process_redos(self):
        for it in self.state.by_status("redo"):
            if self.gens_left() <= 0 or not self.time_left():
                break
            self.generate(it)

    def generate(self, it: dict):
        gen = it.get("gen", 0) + 1
        self.state.update(it["id"], status="generating", gen=gen)
        self.state.save()
        try:
            if it.get("local_path"):
                src = (self.cfg.data_dir / it["local_path"]).read_bytes()
            else:
                src = download(it["image_url"], it.get("fallback_url"))
            src_im = render.open_rgb(src)
            aspect = src_im.size[0] / src_im.size[1]
            style = self.cfg.read_text(self.c("style.prompt_file", "styles/mcm_geo.txt"))
            art = self.stylize(src, style, aspect)
            portrait = None
            if aspect > 0.75:   # landscape/square: recompose a tall version for the wallpaper
                wp_prompt = self.cfg.read_text(self.c("style.wallpaper_prompt_file", "styles/wallpaper_recompose.txt"))
                try:
                    portrait = self.stylize(art, wp_prompt, 9 / 16)
                except Exception as e:  # fall back to cropping the main art
                    log.warning("wallpaper recomposition failed, cropping instead: %s", e)
            outs = render.build(aspect, art, portrait, self.cfg, it.get("focus_x", 0.5))
            rel = f"media/{it['id']}-g{gen}"
            files = outs.save(self.cfg.data_dir / rel, self.c("output.jpeg_quality", 92))
            self.hosting.publish_files(self.cfg.data_dir / rel)
        except ProviderRefused as e:
            self.state.update(it["id"], status="failed")
            it["errors"].append(f"refused: {e}"[:400])
            self.notify_once(f"refused-{it['id']}", f"The image model declined this one ({e}). Skipped.\n"
                                                    f"{it.get('article_url', '')}", 24)
            return
        except Exception as e:
            log.exception("generation failed")
            it["errors"].append(f"generate: {e}"[:400])
            tries = len([x for x in it["errors"] if x.startswith("generate")])
            self.state.update(it["id"], status="failed" if tries >= 2 else "candidate")
            self.notify_once(f"gen-{it['id']}", f"⚠️ Generation failed: {str(e)[:300]}", 6)
            return
        self.state.update(it["id"], media_dir=rel, files=files, generated_at=now_ts(),
                          captions=copywriter.write(it, self.cfg, self.state.d.get("voice_examples") or []),
                          caption_pick=0)
        if self.review_mode() == "auto":
            self.state.update(it["id"], status="approved", approved_at=now_ts())
            return
        self.state.update(it["id"], status="pending", tg_message_id=None)
        if not (self.bot.enabled and self.bot.owner):
            log.warning("Telegram not configured: %s waits for review; preview is sent once it is", it["id"])
        else:
            self.send_review(it)
        self.state.save()

    def caption_waiting_previews(self):
        """Previews made before the caption writer existed (or while it was failing) get their options now."""
        for it in self.state.by_status("pending"):
            if it.get("captions") or it.get("caption_override") or not it.get("media_dir"):
                continue
            opts = copywriter.write(it, self.cfg, self.state.d.get("voice_examples") or [])
            if opts:
                self.state.update(it["id"], captions=opts, caption_pick=0)
                self.bot.edit_caption(it.get("tg_message_id"), self.review_caption(it),
                                      keyboard=self.bot.keyboard_for(it))

    def review_caption(self, it: dict) -> str:
        opts = captions.options(it)
        lines = ([f"{n}\ufe0f\u20e3 {c}" for n, c in enumerate(opts, 1)] if len(opts) > 1
                 else [captions.lead(it) or "(no caption)"])
        lines.append(f"Score {it.get('score', '?')}/10 · {it.get('subject') or 'unknown subject'}"
                     + (f" · redo #{it['gen'] - 1}" if it.get("gen", 1) > 1 else ""))
        if it.get("credit"):
            lines.append(f"📷 {it['credit']}")
        if it.get("article_url"):
            lines.append(it["article_url"])
        lines.append("Left: Threads · Middle: Instagram post · Right: wallpaper (Threads + IG Story)")
        lines.append("Tap the caption to post, or reply with your own." if len(opts) > 1
                     else "Reply with text to change the caption.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ housekeeping
    def cleanup(self):
        keep_media = self.c("hosting.keep_media_days", 21)
        dropped = self.state.prune(max(keep_media, 7))
        media_root = self.cfg.data_dir / "media"
        if media_root.exists():
            live = {i.get("media_dir", "").split("/")[-1] for i in self.state.items.values()
                    if i["status"] in ("pending", "approved", "redo", "generating")
                    or now_ts() - i.get("updated_at", i.get("created_at", 0)) < keep_media * 86400}
            for d in media_root.iterdir():
                if d.is_dir() and d.name not in live:
                    shutil.rmtree(d, ignore_errors=True)
        inbox = self.cfg.data_dir / "inbox"
        if inbox.exists():
            wanted = {Path(i["local_path"]).name for i in self.state.items.values()
                      if i.get("local_path") and i["status"] in ("candidate", "generating", "redo", "pending")}
            for f in inbox.iterdir():
                if f.name not in wanted:
                    f.unlink(missing_ok=True)
        if dropped:
            log.info("pruned %d old items", len(dropped))
