"""Offline end-to-end tests: fake Telegram, fake publishers, local style provider."""
import io
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from retro import captions, config, curate, render
from retro.providers.local import LocalPoster
from retro.publishers.base import Publisher
from retro.state import State

CAP = ("LONDON, ENGLAND - SEPTEMBER 20: Bukayo Saka of Arsenal celebrates scoring his team's second goal "
       "during the Premier League match between Arsenal FC and Chelsea FC at Emirates Stadium on September 20, "
       "2026 in London, England. (Photo by Stuart MacFarlane/Arsenal FC via Getty Images)")


def photo(w, h, seed) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), (rnd.randrange(255), rnd.randrange(255), rnd.randrange(255)))
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.ellipse((x, y, x + rnd.randrange(w // 3), y + rnd.randrange(h // 3)),
                  fill=(rnd.randrange(255), rnd.randrange(255), rnd.randrange(255)))
    b = io.BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


class FakeBot:
    def __init__(self):
        self.token, self.owner = "t", "42"
        self.sent, self.edits, self.texts, self.queue = [], [], [], []
        self._mid = 100

    enabled = True

    def updates(self, offset):
        q, self.queue = self.queue, []
        return q

    def send_preview(self, item, path, caption):
        assert path.exists()
        self._mid += 1
        self.sent.append((item["id"], self._mid, caption))
        return self._mid

    def edit_caption(self, mid, caption, keep_buttons_for=None):
        self.edits.append((mid, caption))

    def answer(self, *a):
        pass

    def text(self, t, reply_to=None):
        self.texts.append(t)

    def is_owner(self, chat):
        return str(chat) == self.owner

    def download_file(self, fid):
        return photo(1200, 1600, 99)

    def _call(self, *a, **k):
        return {}


class FakePub(Publisher):
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def configured(self):
        return True

    def needs_public_urls(self):
        return self.name != "x"

    def publish(self, media, caption, progress=None):
        for k in ("art", "wallpaper", "story", "feed"):
            assert media.paths[k].exists() and media.urls[k].startswith("https://raw.githubusercontent.com/")
        if self.name == "x":
            assert len(caption) <= 280
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(caption)
        return {"id": f"{self.name}-1", "url": f"https://example/{self.name}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/retro-gunners")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = config.load()
    cfg.raw["curation"]["scorer"] = "heuristic"
    cfg.raw["curation"]["min_score"] = 5
    images = {f"https://img/{i}.jpg": photo(*(3000, 2000) if i % 2 else (1800, 2500), seed=i) for i in range(6)}
    cands = [{"source": "arsenal_web", "key": f"k{i:011d}", "image_url": u, "caption_src": CAP.replace("Bukayo Saka", f"Player {i}"),
              "credit": "Stuart MacFarlane/Arsenal FC via Getty Images", "article_title": "Gallery",
              "article_url": "https://www.arsenal.com/news/x", "published_at": __import__("time").time()}
             for i, u in enumerate(images)]
    import retro.pipeline as pl
    monkeypatch.setattr(pl.arsenal, "discover", lambda cfg: [dict(c) for c in cands])
    monkeypatch.setattr(pl, "download", lambda u, f=None: images[u])
    monkeypatch.setattr(curate, "download", lambda u, f=None: images[u])
    monkeypatch.setattr(pl.Hosting, "is_live", lambda self, rel: True)
    return cfg


def make(cfg, pubs):
    from retro.pipeline import Pipeline
    bot = FakeBot()
    p = Pipeline(cfg, State(cfg.data_dir / "state.json"), bot=bot, provider=LocalPoster({}), publishers=pubs)
    return p, bot


def test_full_cycle(env, monkeypatch):
    x, th, ig = FakePub("x"), FakePub("threads"), FakePub("instagram")
    p, bot = make(env, [x, th, ig])
    p.tick()
    assert len(bot.sent) == 2, "should fill the review queue"
    assert len(p.state.by_status("pending")) == 2
    first, second = bot.sent[0][0], bot.sent[1][0]

    # approve one, skip the other, publish at an open slot
    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: True)
    bot.queue = [{"update_id": 1, "callback_query": {"id": "c1", "data": f"a:{first}", "from": {"id": 42},
                                                     "message": {"chat": {"id": 42}}}},
                 {"update_id": 2, "callback_query": {"id": "c2", "data": f"s:{second}", "from": {"id": 42},
                                                     "message": {"chat": {"id": 42}}}}]
    p.tick()
    assert p.state.items[first]["status"] == "posted"
    assert set(p.state.items[first]["posted"]) == {"x", "threads", "instagram"}
    assert p.state.items[second]["status"] == "skipped"
    assert p.state["telegram_offset"] == 3
    assert p.state.count_since("posts", 3600) == 1
    p.state.save()

    # strangers are ignored
    bot.queue = [{"update_id": 3, "callback_query": {"id": "c3", "data": f"a:{first}", "from": {"id": 7},
                                                     "message": {"chat": {"id": 7}}}}]
    p.tick()


def test_redo_and_budget(env):
    env.raw["schedule"]["max_generations_per_day"] = 100
    p, bot = make(env, [])
    p.tick()
    item = bot.sent[0][0]
    bot.queue = [{"update_id": 1, "callback_query": {"id": "c", "data": f"r:{item}", "from": {"id": 42},
                                                     "message": {"chat": {"id": 42}}}}]
    p.tick()
    it = p.state.items[item]
    assert it["gen"] == 2 and it["status"] == "pending" and it["media_dir"].endswith("-g2")
    # stale buttons from gen 1 must not approve gen 2
    bot.queue = [{"update_id": 2, "callback_query": {"id": "c", "data": f"a:{item}:1", "from": {"id": 42},
                                                     "message": {"chat": {"id": 42}}}}]
    p.tick()
    assert p.state.items[item]["status"] == "pending"
    # budget exhausted: a forced discovery must not generate more
    env.raw["schedule"]["max_generations_per_day"] = p.state.count_since("generations", 86400)
    p.state.set("last_discovery", 0)
    before = len(bot.sent)
    p.tick(force_discover=True)
    assert len(bot.sent) == before


def test_inbox_photo_priority(env):
    p, bot = make(env, [])
    bot.queue = [{"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42}, "caption": "Rice at dusk",
                                              "photo": [{"file_id": "f", "file_unique_id": "u1"}]}}]
    p.tick()
    it = p.state.items["tg-u1"]
    assert it["status"] == "pending" and it["caption_override"] == "Rice at dusk"
    assert bot.sent[0][0] == "tg-u1"


def test_publish_failure_retries_then_gives_up(env, monkeypatch):
    bad = FakePub("threads", fail=True)
    good = FakePub("x")
    p, bot = make(env, [good, bad])
    p.tick()
    first = bot.sent[0][0]
    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: True)
    p.state.update(first, status="approved", approved_at=1)
    for _ in range(3):
        p.tick()
    it = p.state.items[first]
    assert it["status"] == "posted" and "x" in it["posted"] and it["attempts"]["threads"] == 3
    assert len(good.calls) == 1


def test_slot_logic(env):
    p, _ = make(env, [])
    tz = ZoneInfo("Europe/London")
    ts = lambda h, m=0: datetime(2026, 10, 3, h, m, tzinfo=tz).timestamp()
    assert not p.slot_open(ts(8, 0))        # before first slot
    assert p.slot_open(ts(8, 31))
    assert not p.slot_open(ts(23, 30))      # quiet hours
    p.state.d["posts"] = [ts(8, 31)]
    assert not p.slot_open(ts(11, 0))       # before 2nd slot
    assert p.slot_open(ts(13, 5))
    p.state.d["posts"] = [ts(8, 31), ts(12, 50)]
    assert not p.slot_open(ts(13, 5))       # min gap
    p.state.d["posts"] = [ts(8, 31), ts(13, 1), ts(19, 31)]
    assert not p.slot_open(ts(22, 0))       # daily cap


def test_render_sizes(env):
    lp = LocalPoster({})
    for (w, h) in [(3000, 2000), (1800, 2500), (2000, 2000)]:
        src = photo(w, h, 1)
        art = lp.stylize(src, "", w / h)
        portrait = lp.stylize(art, "", 9 / 16) if w / h > 0.75 else None
        o = render.build(w / h, art, portrait, env)
        assert abs(o.art.size[0] / o.art.size[1] - w / h) < 0.01 and max(o.art.size) == 2048
        assert o.wallpaper.size == (1290, 2796) and o.story.size == (1080, 1920)
        a = o.feed.size[0] / o.feed.size[1]
        assert 0.8 - 0.01 <= a <= 1.91 and o.feed.size[0] == 1080


def test_heuristic_and_captions(env):
    s, subj, head = curate.heuristic({"caption_src": CAP})
    assert subj == "Bukayo Saka" and head.startswith("Bukayo Saka celebrates scoring") and s >= 8
    item = {"headline": head, "credit": "Stuart MacFarlane/Arsenal FC via Getty Images"}
    for plat, lim in captions.LIMITS.items():
        c = captions.build(item, env, plat)
        assert len(c) <= lim and "MacFarlane" in c
    item["headline"] = "x" * 400
    assert len(captions.build(item, env, "x")) <= 280


def test_mojibake():
    from retro.sources.arsenal import fix_mojibake, fit_url
    assert fix_mojibake("her teamâ\x80\x99s first") == "her team’s first"
    u = "https://assets.arsenal.com/prod/images/xl_landscape/709ff5314683-sunderland-england-david-raya.webp"
    assert fit_url(u) == "https://assets.arsenal.com/prod/images/square_1000_1000/709ff5314683-sunderland-england-david-raya.jpg"


def test_openai_drops_unsupported_param(monkeypatch):
    import base64
    from retro.providers import openai_images as oi
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    calls = []

    class R:
        def __init__(self, ok, j, status=200):
            self.ok, self._j, self.status_code, self.text = ok, j, status, ""

        def json(self):
            return self._j

    class S:
        def post(self, url, headers, data, files, timeout):
            calls.append(dict(data))
            if "input_fidelity" in data:
                return R(False, {"error": {"message": "Unknown parameter", "param": "input_fidelity"}}, 400)
            return R(True, {"data": [{"b64_json": base64.b64encode(b"img").decode()}]})

    monkeypatch.setattr(oi, "session", lambda: S())
    prov = oi.OpenAIImages({"model": "gpt-image-2", "extra": {"input_fidelity": "high"}})
    assert prov.stylize(photo(300, 200, 1), "p", 1.5) == b"img"
    assert calls[0]["size"] == "1536x1024" and "input_fidelity" not in calls[1]


def test_openai_refusal(monkeypatch):
    from retro.providers import openai_images as oi
    from retro.providers import ProviderRefused
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    class R:
        ok, status_code, text = False, 400, ""

        def json(self):
            return {"error": {"message": "Your request was rejected by the safety system", "code": "moderation_blocked"}}

    monkeypatch.setattr(oi, "session", lambda: type("S", (), {"post": lambda *a, **k: R()})())
    with pytest.raises(ProviderRefused):
        oi.OpenAIImages({}).stylize(photo(300, 200, 1), "p", 1.5)


def test_vault_and_token_refresh(tmp_path, monkeypatch):
    import time as _t
    from cryptography.fernet import Fernet
    from retro.publishers import meta_common
    from retro.vault import Vault
    monkeypatch.setenv("STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IG_ACCESS_TOKEN", "seed-token-abcdefghijkl")
    st = State(tmp_path / "s.json")
    v = Vault(st)
    calls = []
    monkeypatch.setattr(meta_common, "call", lambda m, u, **p: calls.append(p) or {"access_token": "new-token", "expires_in": 5184000})
    meta_common.maybe_refresh(v, "instagram", "IG_ACCESS_TOKEN", "u", "ig_refresh_token")
    assert not calls, "fresh secret: no refresh on day 0"
    v._data["instagram"]["refreshed_at"] = _t.time() - 8 * 86400
    meta_common.maybe_refresh(v, "instagram", "IG_ACCESS_TOKEN", "u", "ig_refresh_token")
    assert calls and v.token("instagram", "IG_ACCESS_TOKEN") == "new-token"
    st.save()
    assert "new-token" not in (tmp_path / "s.json").read_text(), "tokens must be encrypted at rest"
    assert Vault(State(tmp_path / "s.json")).token("instagram", "IG_ACCESS_TOKEN") == "new-token"
    monkeypatch.setenv("IG_ACCESS_TOKEN", "replaced-secret-zzzzzzzzzzzz")
    assert Vault(State(tmp_path / "s.json")).token("instagram", "IG_ACCESS_TOKEN") == "replaced-secret-zzzzzzzzzzzz"


def test_arsenal_source_parses_real_shapes(monkeypatch):
    """Shapes captured from arsenal.com's GraphQL + article HTML on 2026-09-25."""
    from datetime import datetime, timezone
    from retro.sources import arsenal as A
    now = datetime.now(timezone.utc).isoformat()
    articles = [
        {"articleId": "88483", "title": "Gallery: 30 snaps from our win over HB Koge Women",
         "path": "/news/gallery-30-snaps-from-our-win-over-hb-koge-women-aheXO5t031B0",
         "taxonomies": ["News", "Match gallery", "Women"], "publicationDate": now,
         "promoImage": "https://assets.arsenal.com/prod/images/xl_landscape/6cbe2adb5ab0-borehamwood-england-mariona-caldentey-of-arsenal-scores-her-teams-first-goal-fro.webp"},
        {"articleId": "88465", "title": "Quiz: Name the XI", "path": "/quiz/x", "taxonomies": ["Quiz", "Gamification"],
         "publicationDate": now, "promoImage": "https://assets.arsenal.com/prod/images/xl_landscape/34c035c60d9e-leicester.webp"},
        {"articleId": "1", "title": "Old", "path": "/news/old", "taxonomies": ["News"],
         "publicationDate": "2025-09-14T13:54:00.000Z", "promoImage": "https://assets.arsenal.com/prod/images/xl_landscape/aaaaaaaaaaaa-old.webp"},
        {"articleId": "88532", "title": "Emily Fox signs new contract", "path": "/news/emily-fox-signs-new-contract-arH0o2D6J7sA",
         "taxonomies": ["Women", "News"], "publicationDate": now,
         "promoImage": "https://assets.arsenal.com/prod/images/xl_landscape/ce63942ef7ef-z926411fvznurfn.webp"},
    ]
    cap = ("BOREHAMWOOD, ENGLAND - SEPTEMBER 22: Mariona Caldentey of Arsenal scores her teamâ\x80\x99s first goal from the "
           "penalty spot during the UEFA Women's Champions League match. (Photo by Alex Burstow/Arsenal FC via Getty Images)")
    gallery = {"singleGallery": {"id": "gallery-5620", "images": [
        {"filename": "x.jpeg", "caption": cap, "description": cap, "headline": "",
         "originalUrl": "https://assets.arsenal.com/prod/images/square_1000_1000/6cbe2adb5ab0-borehamwood-england-mariona-caldentey-of-arsenal-scores-her-teams-first-goal-fro.jpg",
         "list": [{"width": "3246", "height": "2124", "type": "original",
                   "url": "https://assets.arsenal.com/prod/images/original/6cbe2adb5ab0-borehamwood-england-mariona-caldentey-of-arsenal-scores-her-teams-first-goal-fro.jpeg"}]}]}}
    calls = []

    def fake_gql(q, variables, op):
        calls.append((op, variables))
        if op == "GetArticlesByTaxonomy":
            return {"getArticlesByTaxonomy": {"articles": articles if variables["pageNumber"] == 0 else []}}
        return gallery

    monkeypatch.setattr(A, "gql", fake_gql)
    html = '<script id="__NEXT_DATA__">{"body":[{"type":"TEXT"},{"type":"GALLERY","id":"5620"},{"type":"PROMOTEDARTICLE","id":"88480"}]}</script>'
    monkeypatch.setattr(A, "gallery_ids_for", lambda path: ["5620"] if "gallery" in path else [])
    assert A.GALLERY_BLOCK_RE.findall(html) == ["5620"]

    class Cfg:
        def get(self, k, d=None):
            return {"sources.arsenal_web": {"lookback_hours": 72}}.get(k, d)

    res = {c["key"]: c for c in A.discover(Cfg())}
    assert calls[0][1]["pageNumber"] == 0
    assert set(res) == {"6cbe2adb5ab0", "ce63942ef7ef"}          # quiz + old article dropped, promo/gallery merged
    g = res["6cbe2adb5ab0"]
    assert g["image_url"].endswith("/original/6cbe2adb5ab0-borehamwood-england-mariona-caldentey-of-arsenal-scores-her-teams-first-goal-fro.jpeg")
    assert g["credit"] == "Alex Burstow/Arsenal FC via Getty Images" and "team’s" in g["caption_src"]
    assert res["ce63942ef7ef"]["image_url"].startswith("https://assets.arsenal.com/prod/images/square_1000_1000/")
    s, subj, head = curate.heuristic(g)
    assert subj == "Mariona Caldentey" and head.startswith("Mariona Caldentey scores her team’s first goal")


def test_recovers_stale_generating_and_resends_previews(env):
    p, bot = make(env, [])
    p.bot.owner = None                     # Telegram not set up yet
    p.tick()
    pend = p.state.by_status("pending")
    assert pend and not bot.sent and all(not i.get("tg_message_id") for i in pend)
    p.bot.owner = "42"                     # owner configured later -> previews go out
    p.tick()
    assert len(bot.sent) == len(pend)
    # a job killed mid-generation
    it = pend[0]
    p.state.update(it["id"], status="generating")
    it["updated_at"] = 0
    p.recover_stale()
    assert p.state.items[it["id"]]["status"] == "redo"
    # re-sending the same Telegram photo doesn't reset a finished item
    p.state.put({"id": "tg-u9", "key": "tg-u9", "status": "posted"})
    bot.queue = [{"update_id": 9, "message": {"message_id": 1, "chat": {"id": 42},
                                              "photo": [{"file_id": "f", "file_unique_id": "u9"}]}}]
    p.handle_updates()
    assert p.state.items["tg-u9"]["status"] == "posted"
