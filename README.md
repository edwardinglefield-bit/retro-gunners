# Retro Gunners

This pipeline takes Arsenal's official photos and turns them into mid-century geometric poster art, formatted for Instagram and Threads (X is supported but switched off). Nothing goes out until you tap **Post** in Telegram.

```
arsenal.com galleries ─┐                               ┌─ X: art + wallpaper (off)
photos you send the bot ┴► score (vision model) ► style (gpt-image-2) ► Telegram review ► Threads: 2-image carousel
                                                   ├ art (source aspect)                  └─ Instagram: 4:5 post + wallpaper story
                                                   └ wallpaper 1290×2796 (+ 9:16 story)
```

## Day to day
- About two previews wait for you at any time. Each shows the Threads art (left), the 4:5 Instagram post (middle) and the phone wallpaper (right; it goes to Threads and your IG Story). Tap ✅ Post, 🔁 Redo or ✖ Skip.
- Men's team photos come first; about 1 in 4 previews is from the women's team (`curation.women_share`).
- Approved items post at the next slot (08:30 / 13:00 / 19:30 UK), at most 3 a day, with at least 3h between posts.
- **Send the bot any photo** to have it illustrated next. If you add a caption, it becomes the post caption.
- **Reply to a preview** with text to replace its caption.
- `/status` · `/run` · `/pause` · `/resume`

## Cost
gpt-image-2 at high quality costs about $0.20 per image call. A landscape photo takes 2 calls (the art, then a recomposed tall wallpaper), and a portrait photo takes 1. The budget is **6 paid calls per rolling 24h**, redos included, which comes to about **$25–40 a month** at 2–3 posts a day. On top of that:

- X is off (`publish.x.enabled`); switching it on costs about $0.015 per post in X API credit.
- Scoring uses a mini vision model and costs pennies.
- GitHub Actions is free for public repos.

Set the knobs in `config.yaml`: `schedule.max_generations_per_day` and `style.providers.openai.quality`.

## Change things
| Want | Edit |
|---|---|
| Different art style | `styles/mcm_geo.txt` (or add a file and point `style.prompt_file` at it) |
| Another image provider | `style.provider: gemini` (needs `GEMINI_API_KEY`), or add a class in `retro/providers/` |
| Post without review | `review.mode: auto` |
| Posting times / volume | `schedule.*` in `config.yaml` |
| Brand name, hashtags, credit line | `brand.*` |

## Local commands
```bash
pip install -r requirements.txt
python -m retro sources                         # what arsenal.com has right now (free)
OPENAI_API_KEY=... python -m retro stylize photo.jpg    # try the style on one photo → ./out/
python -m retro stylize photo.jpg --provider local       # free offline dry run of the formats
python -m pytest -q
```

## Layout
`retro/sources` finds photos · `curate.py` scores and de-duplicates · `providers/` does the style transfer (swappable) · `render.py` makes the output formats · `telegram.py` handles review · `publishers/` posts to Threads and IG (X optional) · `pipeline.py` orchestrates one tick. State and media live on the `data` branch, which is force-pushed as a single commit so the repo doesn't grow.

## Fair use
Every post credits the photographer from the Getty caption and says it's fan-made and unaffiliated. Keep it non-commercial: no ads, no selling prints. If the club or a photographer asks, take the post down (`/pause` stops everything).
