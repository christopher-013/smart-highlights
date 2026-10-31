# Smart Highlights

Turn a season of GameChanger clips into a YouTube highlight reel: every hit, strikeout and out, trimmed to the
key play, with a caption on each clip.

- **Website** (`docs/`, GitHub Pages): visitors connect their YouTube channel, paste a GameChanger player link,
  choose the reels and caption style, sign in to GameChanger, and watch the progress.
- **Service** (`server/`): an API and worker that runs the engine for each request, one at a time.
- **Engine** (`engine/`): the scripts that do the work. They also run on their own on a Windows PC.

Tag `v1.0.0` is the locked format and logic described below.

## How plays are found

Checked frame by frame on real clips from four different camera setups.

- GameChanger's clip time is when the scorekeeper tapped the play, 1–34 seconds after it happened, so the whole
  clip up to that moment is searched.
- Balls in play: the last bat crack at least 30% as loud as the loudest one (fouls are cracks too).
- Strikeouts: the last glove pop that follows a still moment (everyone set for the pitch).
- GameChanger's play-by-play says whether the at-bat ended in play or on strike three, and supplies the caption sentence.
- Each clip starts 3 s before the key moment and ends when the field settles (limits per type of play), plus 1 s.
- Captions: `Player - Play` / `Mon D, YYYY vs Opponent · Inning` / play-by-play sentence; compact, bottom-left,
  on for the whole clip (or the first few seconds with `--caption brief`). Blank or frozen clips are skipped.
- Top N: ranked by type of play, crowd reaction, length of the action and runs scored; shown in game order.

## Run it on your own computer

Needs Python 3.12 and Google Chrome. Once, from this folder:

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r server\requirements.txt
```

Then double-click **Make Highlights.bat**. It asks for a team folder, the player's GameChanger link, which reel
you want, and whether to keep it on this computer or upload it. Answers are saved in the team folder as
`smart_highlights.json`. If a sign-in is needed, sign in to GameChanger in the Chrome window that comes up.

- Reels are kept in `<team folder>\highlights\<Player>_quick\` (or `<Player>\` for full clips).
- Uploading needs a Google Cloud OAuth client (type *Desktop app*, YouTube Data API v3 enabled) saved in the team
  folder as `client_secret.json`. Uploads are Unlisted, ask you to confirm first, and keep a copy on this computer.
- To try it quickly, or test the setup without changing anything (from this folder):

```
.venv\Scripts\python.exe engine\make_reel.py --folder "D:\GameChanger\My Team Fall 2024" --reel smart --limit 4 --local
.venv\Scripts\python.exe engine\make_reel.py --folder "D:\GameChanger\My Team Fall 2024" --check
```

`--limit N` makes a trial of the first N plays, with its own title, so it never replaces the full reel.

### Or run the scripts directly

Needs `pip install -r server\requirements.txt` in the Python you use.

1. Copy `engine/` to a folder per team (for example `D:\GameChanger\My Team Fall 2024\`).
2. Edit the settings at the top of `gc_to_youtube.py` (team) and `highlights.py` (player), or write a settings
   file (see `engine/sh_config.py`) and point `SMART_HIGHLIGHTS_CONFIG` at it.
3. Put a Google Cloud OAuth client (type *Desktop app*, YouTube Data API v3 enabled) in the folder as
   `client_secret.json`.
4. `py highlights.py sample` to check the cuts, then `py highlights.py run --quick` (Smart Highlights),
   `run --quick --top 10` (Top 10) or `run` (full clips).

## Run the hosted service

The service needs a machine with Chrome and ffmpeg; the Docker image includes both.

```
docker build -t smart-highlights .
docker run -p 8000:8000 -v sh-data:/data \
  -e GOOGLE_CLIENT_ID=... -e GOOGLE_CLIENT_SECRET=... \
  -e PUBLIC_API_URL=https://api.your-domain.example \
  -e SITE_ORIGINS=https://christopher-013.github.io \
  smart-highlights
```

- Google Cloud: an OAuth client of type *Web application* with redirect URI
  `<PUBLIC_API_URL>/auth/google/callback`, and YouTube Data API v3 enabled. Until Google verifies the app, only
  test users you add (up to 100) can connect a channel.
- Set `window.SMART_HIGHLIGHTS_API` in `docs/config.js` to the `PUBLIC_API_URL`.
- GameChanger passwords and YouTube sign-ins are kept in memory for one job only. Each job's folder (videos,
  browser profile, YouTube token) is deleted when it ends; progress records are deleted after 7 days.

## Publish the website

Repository Settings > Pages > Deploy from a branch > `main`, folder `/docs`.

## Tests

`python -m pytest tests`. They use real ffmpeg with GameChanger and YouTube faked, so nothing leaves the computer.
GitHub Actions runs them on every push.

## Before going public

- GameChanger's terms of service govern automated access to its site and videos. Check them, or ask GameChanger,
  before offering this to the public.
- Google requires app verification for the YouTube upload permission, and the default API quota allows about
  six uploads a day for the whole service; request a quota increase.
- Smart Highlights isn't affiliated with GameChanger or YouTube.
