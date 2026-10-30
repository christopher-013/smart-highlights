# Smart Highlights - notes for Claude

Turns a season of GameChanger clips for one player into YouTube highlight reels. Owner: Christopher
(christopher-013). Repo is private until the hosted service is ready.

## Layout
- `engine/` - the working scripts (also run standalone on Christopher's Windows PC in `D:\GameChanger\<Team Season>\`).
  - `gc_to_youtube.py` - team settings, GameChanger browser (real Chrome over CDP port 9222), schedule scan,
    full-game uploads, YouTube auth/upload/playlists, quota wait. Settings block at the top.
  - `highlights.py` - player reels: `list`, `sample`, `build`, `upload`, `run` with `--quick` (Smart Highlights),
    `--top N`, `--caption always|brief`, `--again`. One run at a time (lock file next to the team folders).
  - `smart_cut.py` - key-play detection, zoom, optional vision-AI check (`anthropic_key.txt`).
  - `sh_config.py` - optional JSON settings file (`SMART_HIGHLIGHTS_CONFIG`), work folder
    (`SMART_HIGHLIGHTS_WORKDIR`), player/channel link parsing, title templates `{player} {team} {season} {n}`.
- `server/app.py` - FastAPI service: Google OAuth (connect YouTube), jobs (one at a time), emailed-code handoff.
- `docs/` - GitHub Pages site (static). `docs/config.js` holds the API address (empty = service offline notice).
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

## Not verified yet
- Unattended GameChanger sign-in (`auto_login`) and emailed-code flow - selectors are guesses.
- Headless Chrome on a server: GameChanger once refused Playwright's own Chromium ("couldn't load your account").
- Docker image build; Google OAuth web flow against a real Google Cloud client.

## Before public launch
- GameChanger terms of service (automated access, using visitors' logins) - biggest risk.
- Google OAuth verification for YouTube scopes; YouTube API quota (~6 uploads/day by default).
- Pick a host for the service (Christopher's PC with a tunnel, or a small cloud server); set `docs/config.js`;
  enable Pages from `main` /docs.
