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
from retro.telegram import Telegram

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

    keyboard = staticmethod(Telegram.keyboard)
    keyboard_for = Telegram.keyboard_for

    def edit_caption(self, mid, caption, keep_buttons_for=None, keyboard=None):
        self.edits.append((mid, caption))
        self.keyboards = getattr(self, "keyboards", []) + [keyboard]

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


def test_run_tops_up_a_full_queue(env):
    env.raw["schedule"]["max_generations_per_day"] = 100
    p, bot = make(env, [])
    p.tick()
    p.state.set("last_discovery", 0)
    p.tick()
    assert len(p.state.by_status("pending")) == 2, "a full queue waits for you on its own"
    bot.queue = [{"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42}, "text": "/run"}}]
    p.tick()
    assert len(p.state.by_status("pending")) == 4, "/run asks for fresh previews regardless"


def test_caption_options_pick_and_learn(env, monkeypatch):
    import retro.pipeline as pl
    seen = []

    def fake_write(item, cfg, examples=()):
        seen.append(list(examples))
        return ["North London is red.", "Highbury would be proud.", "That's our club."]

    th = FakePub("threads")
    p, bot = make(env, [th])
    p.tick()                                           # made before the writer existed: plain description
    first, second = bot.sent[0][0], bot.sent[1][0]
    monkeypatch.setattr(pl.copywriter, "write", fake_write)
    p.tick()                                           # waiting previews get their options and buttons
    assert p.state.items[first]["captions"][1] == "Highbury would be proud."
    assert "2\ufe0f\u20e3 Highbury would be proud." in bot.edits[-1][1]
    assert [b["text"] for b in bot.keyboards[-1]["inline_keyboard"][0]] == ["✅ Post 1", "✅ Post 2", "✅ Post 3"]

    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: True)
    monkeypatch.setattr(type(p), "quiet_now", lambda self, now=None: False)
    bot.queue = [{"update_id": 1, "callback_query": {"id": "c", "data": f"a:{first}:1:1", "from": {"id": 42},
                                                     "message": {"chat": {"id": 42}}}},
                 {"update_id": 2, "message": {"message_id": 9, "chat": {"id": 42}, "text": "Up the Arsenal, always.",
                                              "reply_to_message": {"message_id": p.state.items[second]["tg_message_id"]}}}]
    p.tick()
    assert th.calls[0].startswith("Highbury would be proud.")            # the tapped option is what posts
    assert captions.lead(p.state.items[second]) == "Up the Arsenal, always."   # your own text wins
    assert p.state.d["voice_examples"] == ["Up the Arsenal, always."]

    p.state.update(second, status="redo")              # the next write sees your caption as a voice example
    p.tick()
    assert seen[-1] == ["Up the Arsenal, always."]


def test_copywriter_cleans_and_keeps_your_note(env, monkeypatch):
    from retro import copywriter

    class R:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": '{"captions": ["Le Professeur arrives. #Arsenal #COYG", '
                                                        '"1996, and nothing was the same.", "' + "x " * 150 + '"]}'}}]}

    posted = {}
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(copywriter, "session", lambda: type("S", (), {"post": lambda self, url, **kw: posted.update(kw) or R()})())
    item = {"article_title": "The full inside story of Arsène Wenger's arrival in 1996", "team": "men", "headline": "Manager on pitch"}
    out = copywriter.write(item, env, ["Up the Arsenal."])
    assert out[0] == "Le Professeur arrives." and len(out) == 3 and len(out[2]) <= 201
    prompt = posted["json"]["messages"][0]["content"]
    assert "Wenger's arrival in 1996" in prompt and "- Up the Arsenal." in prompt and "lifelong Gooner" in prompt
    mine = copywriter.write({"source": "telegram", "caption_src": "Arteta, title night"}, env)
    assert mine[0] == "Arteta, title night" and len(mine) == 3
    env.raw["captions"]["writer"] = "off"
    assert copywriter.write(item, env) == []


def test_inbox_photo_priority(env):
    p, bot = make(env, [])
    bot.queue = [{"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42}, "caption": "Rice at dusk",
                                              "photo": [{"file_id": "f", "file_unique_id": "u1"}]}}]
    p.tick()
    it = p.state.items["tg-u1"]
    assert it["status"] == "pending" and captions.lead(it) == "Rice at dusk"
    assert bot.sent[0][0] == "tg-u1"


def test_publish_failure_retries_then_gives_up(env, monkeypatch):
    bad = FakePub("threads", fail=True)
    good = FakePub("x")
    p, bot = make(env, [good, bad])
    p.tick()
    first = bot.sent[0][0]
    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: True)
    monkeypatch.setattr(type(p), "quiet_now", lambda self, now=None: False)
    p.state.update(first, status="approved", approved_at=1)
    for _ in range(3):
        p.tick()
    it = p.state.items[first]
    assert it["status"] == "posted" and "x" in it["posted"] and it["attempts"]["threads"] == 3
    assert len(good.calls) == 1


def test_half_posted_item_finishes_before_the_next_slot(env, monkeypatch):
    ig, th = FakePub("instagram"), FakePub("threads", fail=True)
    p, bot = make(env, [ig, th])
    p.tick()
    first = bot.sent[0][0]
    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: True)
    monkeypatch.setattr(type(p), "quiet_now", lambda self, now=None: False)
    p.state.update(first, status="approved", approved_at=1)
    p.tick()                                   # instagram goes out, threads fails
    assert set(p.state.items[first]["posted"]) == {"instagram"} and p.state.items[first]["status"] == "approved"
    monkeypatch.setattr(type(p), "slot_open", lambda self, now=None: False)   # next slot is hours away
    th.fail = False
    p.tick()
    it = p.state.items[first]
    assert it["status"] == "posted" and set(it["posted"]) == {"instagram", "threads"}
    assert len(ig.calls) == 1 and len(p.state["posts"]) == 1   # no double post, counts as one


def test_threads_carousel_waits_out_meta_propagation(monkeypatch):
    from retro.publishers import threads as th
    from retro.publishers.base import Media, PublishError
    made = []

    def fake_call(method, url, **params):
        made.append(params.get("media_type"))
        if params.get("media_type") == "CAROUSEL" and made.count("CAROUSEL") < 3:
            raise PublishError("400: {'error_subcode': 4279004, 'error_user_title': 'Invalid carousel children'}")
        return {"id": f"c{len(made)}", "status": "FINISHED", "permalink": "https://threads/p"}

    monkeypatch.setattr(th, "call", fake_call)
    monkeypatch.setattr(th.time, "sleep", lambda s: None)
    t = th.Threads({}, type("V", (), {"token": lambda self, name, env_var: "tok"})())
    res = t.publish(Media(paths={}, urls={"art": "https://a", "wallpaper": "https://w"}), "caption")
    assert made.count("CAROUSEL") == 3 and res["url"] == "https://threads/p"


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
        assert o.feed.size == (1080, 1350)


def test_men_first_with_some_women(env):
    def cand(i, team, score=8):
        return {"key": f"k{i}", "team": team, "score": score, "subject": f"player {i}", "published_at": i}
    pool = [cand(i, "women", 10) for i in range(6)] + [cand(10 + i, "men") for i in range(6)] + [cand(20, "club")]
    picks = [curate.team_of(c) for c in curate.choose(pool, env, [], [])]
    # men lead, one women's photo in four, club content once the men's run out, women's stay capped
    assert picks == ["men", "men", "men", "women", "men", "men", "men", "women", "club"]
    assert curate.choose([cand(1, "women")], env, [], ["women"] * 2 + ["men"] * 2) == []   # over the cap: wait
    assert curate.choose([cand(40, "away", 10)], env, [], []) == []      # Gunners in other teams' shirts
    own = {**cand(30, "women"), "priority": 1}
    assert curate.choose([own], env, [], ["women"] * 8) == [own]       # your own photos always go through
    assert curate.team_of({"caption_src": "BOREHAMWOOD, ENGLAND: Alessia Russo of Arsenal"}) == "women"
    from retro.sources.arsenal import team_of_taxonomies
    assert team_of_taxonomies({"news", "men"}) == "men" and team_of_taxonomies({"women", "news"}) == "women"
    assert team_of_taxonomies({"men", "internationals"}) == "away"


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
