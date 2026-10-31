# Smart Highlights - notes for Claude

Turns a season of GameChanger clips for one player into YouTube highlight reels. Owner: Christopher
(christopher-013). This repo is public, so anything committed here can be read by anyone.

## Layout
- `engine/` - the working scripts (also run standalone on Christopher's Windows PC in `D:\GameChanger\<Team Season>\`).
  - `gc_to_youtube.py` - team settings, GameChanger browser (real Chrome over CDP port 9222), schedule scan,
    full-game uploads, YouTube auth/upload/playlists, quota wait. Settings block at the top.
  - `highlights.py` - player reels: `list`, `sample`, `build`, `upload`, `run` with `--quick` (Smart Highlights),
    `--top N`, `--caption always|brief`, `--again`. One run at a time (lock file next to the team folders).
  - `smart_cut.py` - key-play detection, zoom, optional vision-AI check (`anthropic_key.txt`).
  - `sh_config.py` - optional JSON settings file (`SMART_HIGHLIGHTS_CONFIG`), work folder
    (`SMART_HIGHLIGHTS_WORKDIR`), player/channel link parsing, title templates `{player} {team} {season} {n}`.
  - `make_reel.py` - the local prototype's front door (`Make Highlights.bat`): asks questions, saves
    `smart_highlights.json` in the team folder, runs `highlights.py`. `run --local` keeps the reel, `--keep` keeps
    files after uploading, `--limit N` is a trial reel, `check` tests the setup without changing anything.
- `server/local_app.py` - phase 1 product: the public site calls it at localhost:8765. It opens GameChanger in the
  engine's Chrome, builds the team and player menus from what those pages load, runs `make_reel.py --reel smart --local`.
  Top N, full clips and YouTube upload are the next milestone for the page.
- `server/install_app.py` (`Set Up Smart Highlights.bat`) - one-time setup: registers `smart-highlights://` so the
  site's Start button launches the app with no window. A browser can't start anything without this.
- `server/app.py` - parked hosted service: Google OAuth (connect YouTube), jobs (one at a time), emailed-code handoff.
  It has no page right now.
- `docs/` - the local app's page; opened anywhere else it says to start the app.
- `tests/` - `pytest tests` (needs ffmpeg). `tests/fake_engine.py` stands in for the engine in service tests.

## Locked format (release-v1.0 branch; tag v1.0.0 to be created on GitHub)
Change only on purpose, and re-validate against real sample clips when you do.
- Key moment: in play = last bat crack >= 30% of the loudest (cracks are 300-6000x background); strikeout =
  last glove pop (>=10x) after ~3 s of stillness; no crack + clip opens mid-action = play at clip start.
- GameChanger's clip time is 1-34 s AFTER the play (scorekeeper tap): search the whole clip up to it + 1 s.
- Keep 3 s before the key moment; end when field motion settles, per-play limits (single 4-9, double 8-13,
  triple 14-19, HR 12-24, strikeout 2-4, outs 3-6), +1 s.
- Caption: `Player - Play` / `Mon D, YYYY vs Opponent · Inning` / play-by-play sentence; 26 px, line spacing 4,
  bottom-left (x=14, y=h-th-12), on the whole clip by default. Label files must be written with `\n` endings.
- Skip blank/black/frozen clips. Top N by excitement, shown in game order.
- Titles: `Highlights - ...`, `Smart Highlights: ...`, `Smart Highlights: Top N - ...`. Uploads Unlisted.
- Any change to cut settings must change the clips-folder hash (bump the version string in `build()`).

## Conventions
- Write for a non-programmer user: plain-language log messages and docs.
- Run `python -m pyflakes engine server tests` and `python -m pytest -q tests` before committing.
- Never commit secrets or personal data: `client_secret*.json`, tokens, `anthropic_key.txt`, cookies,
  browser profiles, downloads, real player names/IDs (see `.gitignore`). Engine settings use placeholders.
- GameChanger passwords and YouTube tokens in the service live in memory per job only; job folders are deleted.

## Learned the hard way
- Keep `playwright` current: an old version hangs attaching to a newer Chrome (1.49 vs Chrome 156). Pins in
  `server/requirements.txt` follow what the real uploads used.
- GameChanger shows public teams to signed-out visitors, games included. Games on the schedule don't prove a sign-in;
  `looks_signed_out` does the check. Signed out, the clip list never loads.
- A clip list that stops early must stop the run (`read_all_pages`): one upload silently had 19 of 109 plays.
- Clip times are UTC; show the game's date. The engine's Chrome stays open between runs and is reused.

## Not verified yet
- Unattended GameChanger sign-in (`auto_login`) and emailed-code flow - selectors are guesses.
- Headless Chrome on a server: GameChanger once refused Playwright's own Chromium ("couldn't load your account").
- Docker image build; Google OAuth web flow against a real Google Cloud client.

## Before public launch
- GameChanger terms of service (automated access, using visitors' logins) - biggest risk.
- Google OAuth verification for YouTube scopes; YouTube API quota (~6 uploads/day by default).
- Pick a host for the service (Christopher's PC with a tunnel, or a small cloud server); give it a page again;
  enable Pages from `main` /docs.
