"""
GameChanger -> YouTube (Unlisted) archiver
Team and channel are set in "team settings" below.

Commands
  python gc_to_youtube.py scan              Log in (first time), list every game on the schedule -> games.json
  python gc_to_youtube.py probe <game_id>   Debug one game: show which video streams were captured
  python gc_to_youtube.py details <id|all>  Preview YouTube descriptions (score, time, place, recap)
  python gc_to_youtube.py run [--limit N] [--parallel N]   Download each game's full video and upload it Unlisted
  python gc_to_youtube.py fix-descriptions  Rewrite descriptions of already-uploaded videos with the latest format

Safe to re-run: anything already in uploaded.json is never downloaded or uploaded again.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sh_config  # noqa: E402  (optional settings file, see sh_config.py)

# ---------------------------------------------------------------- team settings (the only part that differs per team)
TEAM_ID = "YOUR_TEAM_ID"                         # from https://web.gc.com/teams/<TEAM_ID>/<slug>
TEAM_SLUG = ""                                   # optional: the part after the team id in the URL
TEAM_NAME = ""                                   # filled in by 'scan' from GameChanger (saved in team.json)
SEASON = "Fall 2024"
CHANNEL_NAME = "My Sports Videos"
EXPECTED_HANDLE = ""                             # e.g. "mychannel" for youtube.com/@mychannel; uploads refused anywhere else ("" = any)
PLAYLIST_TITLE = "{team} - {season}, {range}"
PLAYLIST_DESCRIPTION = "Full game videos - {team}, {season}."
TAGS = ["{team}", "softball"]
SHARED_BROWSER_PROFILE = True                    # reuse one GameChanger login for all team folders next to this one
TITLE_FORMAT = "vs {opp} {date}"

# a settings file (SMART_HIGHLIGHTS_CONFIG), when given, replaces the team settings above
_OVERRIDES = sh_config.team_settings()
globals().update(_OVERRIDES)
BASE = sh_config.workdir(Path(__file__).resolve().parent)   # where logins, downloads and notes are kept
HEADLESS = sh_config.headless()

# ---------------------------------------------------------------- general settings
TEAM_JSON = BASE / "team.json"   # team name GameChanger uses (saved by 'scan')
if not TEAM_NAME:
    try:
        TEAM_NAME = json.loads(TEAM_JSON.read_text(encoding="utf-8")).get("name", "")
    except Exception:
        TEAM_NAME = ""
# Playlist name, description and tags may use {team}, {short} (team without coach), {season}, {range}
# ({range} = first - last game date, e.g. "Feb 24 - Apr 27"). Blank = sensible defaults.


def short_name():
    return re.sub(r"\s*\([^)]*\)\s*$", "", TEAM_NAME)     # "Team Name (Coach)" -> without coach


def date_range():
    try:
        dates = sorted(g.get("date") or "" for g in json.loads(GAMES_JSON.read_text(encoding="utf-8"))
                       if not g.get("skip") and g.get("date"))
    except Exception:
        dates = []
    if not dates:
        return ""
    f = lambda d: dt.date.fromisoformat(d).strftime("%b %d").replace(" 0", " ")
    return f"{f(dates[0])} - {f(dates[-1])}"


def fill(text):
    return text.format(team=TEAM_NAME, short=short_name(), season=SEASON, range=date_range())


def playlist_title():
    return fill(PLAYLIST_TITLE or "{short} {season}")


def playlist_description():
    return fill(PLAYLIST_DESCRIPTION or "Full game videos - {short}, {season}.")


def video_tags():
    if TAGS:
        return [fill(t) for t in TAGS]
    return [w for w in short_name().split() if len(w) > 1] + ["baseball"]
TEAM_URL = f"https://web.gc.com/teams/{TEAM_ID}" + (f"/{TEAM_SLUG}" if TEAM_SLUG else "")
SCHEDULE_URL = f"{TEAM_URL}/schedule"
PRIVACY = "unlisted"
CATEGORY_ID = "17"                               # Sports
MADE_FOR_KIDS = False
DELETE_AFTER_UPLOAD = True                       # leaves a same-named .txt note behind

ROOT = BASE
WORK = ROOT / "downloads"
PROFILE = (ROOT.parent if SHARED_BROWSER_PROFILE else ROOT) / "browser_profile"   # keeps you logged in to GameChanger
GAMES_JSON = ROOT / "games.json"
STATE_JSON = ROOT / "uploaded.json"
COOKIES_TXT = ROOT / "gc_cookies.txt"
CLIENT_SECRET = ROOT / "client_secret.json"      # Google Cloud OAuth client (Desktop app) - see README
TOKEN = ROOT / "youtube_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",          # needed to create and add to the playlist
]
PLAYLIST_PRIVACY = "unlisted"
globals().update({k: v for k, v in _OVERRIDES.items() if k in ("PRIVACY", "PLAYLIST_PRIVACY", "DELETE_AFTER_UPLOAD")})
UUID_RE = re.compile(r"/schedule/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")

# selectors tried (in order) to make each recording on the /videos page start loading
PARALLEL_DOWNLOADS = 3
MIN_FREE_GB = 15                                 # pause downloads when the drive gets this full
QUOTA_RETRY_HOURS = 1                            # after the midnight reset, retry uploads this often
MIN_FULL_GAME_MINUTES = 20                       # anything shorter is treated as a highlight clip

PLAY_SELECTORS = [
    "button[aria-label*='play' i]",
    "[data-testid*='video' i]",
    "[class*='VideoCard' i]",
    "[class*='video-card' i]",
    "[class*='Thumbnail' i]",
    "video",
]


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]+', "", s).strip()[:120]


FFMPEG = "ffmpeg"


def check_tools():
    """yt-dlp is run as a Python module, so it doesn't need to be on PATH; ffmpeg comes from PATH or imageio-ffmpeg."""
    global FFMPEG
    import importlib.util
    if importlib.util.find_spec("yt_dlp") is None:
        sys.exit("yt-dlp isn't installed for this Python. Run:  py -m pip install yt-dlp")
    found = shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            sys.exit("ffmpeg wasn't found. Run:  py -m pip install imageio-ffmpeg")
    FFMPEG = found


def need(tool):
    if not shutil.which(tool):
        sys.exit(f"'{tool}' is not on PATH. Install it first (see README).")


# ---------------------------------------------------------------- browser
CDP_PORT = 9222
_browser = None
_page = None


def find_chrome():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")


def open_browser(p, headless=False):
    """Start (or reuse) a normal Chrome window with its own profile and control it over the debugging port.
    GameChanger refuses to load accounts in Playwright's own automated browser, but works in regular Chrome."""
    global _browser
    global _page
    PROFILE.mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{CDP_PORT}"
    try:
        _browser = p.chromium.connect_over_cdp(url, timeout=15000)
    except Exception:
        if chrome_answers(url):
            sys.exit("Chrome is running but this program couldn't attach to it, usually because Playwright is older "
                     "than Chrome. Run:  py -m pip install -U playwright   (or close that Chrome window and try again)")
        chrome = find_chrome()
        if not chrome:
            sys.exit("Couldn't find Google Chrome. Install it, or edit find_chrome() with its path.")
        extra = (["--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1400,1000"]
                 if (headless or HEADLESS) else [])
        subprocess.Popen([chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}",
                          "--no-first-run", "--no-default-browser-check", *extra, "about:blank"])
        for _ in range(30):
            time.sleep(1)
            try:
                _browser = p.chromium.connect_over_cdp(url, timeout=3000)
                break
            except Exception:
                pass
        else:
            sys.exit("Chrome started but the script couldn't connect to it. Close all Chrome windows "
                     "that use the script's profile and try again.")
    ctx = _browser.contexts[0] if _browser.contexts else _browser.new_context()
    _page = page = ctx.new_page()
    return ctx, page


def chrome_answers(url):
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=3):
            return True
    except Exception:
        return False


def close_browser(ctx):
    """Close this run's tab and disconnect from Chrome; the window stays open and stays signed in."""
    try:
        _page.close()
    except Exception:
        pass
    try:
        _browser.close()
    except Exception:
        pass


def game_links(page):
    return page.eval_on_selector_all(
        "a[href*='/schedule/']",
        "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
    )


# GameChanger login for unattended runs (the hosted service). Read once, then removed from the environment so
# no helper program (yt-dlp, ffmpeg) ever sees it. Never written to disk or printed.
_GC_LOGIN = {k: os.environ.pop(k) for k in ("GC_EMAIL", "GC_PASSWORD") if os.environ.get(k)}
CODE_REQUEST = BASE / "gc_code_needed"          # the service shows "enter the code GameChanger emailed you"
CODE_FILE = BASE / "gc_code.txt"                # ...and writes the code here


def _first_visible(page, selectors, timeout_ms=0):
    end = time.time() + timeout_ms / 1000
    while True:
        for sel in selectors:
            loc = page.locator(sel)
            for k in range(min(loc.count(), 5)):
                if loc.nth(k).is_visible():
                    return loc.nth(k)
        if time.time() >= end:
            return None
        page.wait_for_timeout(500)


def _submit(page):
    btn = _first_visible(page, ["button[type=submit]", "button:has-text('Sign in')", "button:has-text('Log in')",
                                "button:has-text('Continue')", "button:has-text('Next')"])
    if btn:
        btn.click()
    else:
        page.keyboard.press("Enter")


def auto_login(page):
    """Fill in GameChanger's sign-in form: email, password and (if asked) the emailed code."""
    page.goto("https://web.gc.com/login", wait_until="domcontentloaded")
    email = _first_visible(page, ["input[type=email]", "input[name*=email i]", "input[autocomplete=username]"], 20000)
    if not email:
        sys.exit("GameChanger's sign-in page didn't show an email box.")
    email.fill(_GC_LOGIN.get("GC_EMAIL", ""))
    pw = _first_visible(page, ["input[type=password]"], 1500)
    if not pw:
        _submit(page)
        pw = _first_visible(page, ["input[type=password]"], 15000)
    if pw and _GC_LOGIN.get("GC_PASSWORD"):
        pw.fill(_GC_LOGIN["GC_PASSWORD"])
        _submit(page)
    code_box = ["input[autocomplete=one-time-code]", "input[name*=code i]", "input[inputmode=numeric]"]
    end = time.time() + 15 * 60
    while time.time() < end:
        page.wait_for_timeout(2000)
        if "/login" not in page.url and not _first_visible(page, ["input[type=password]"] + code_box):
            log("Signed in to GameChanger")
            return
        box = _first_visible(page, code_box)
        if box:
            CODE_FILE.unlink(missing_ok=True)
            CODE_REQUEST.write_text("GameChanger sent a sign-in code by email", encoding="utf-8")
            log("GameChanger asked for the sign-in code it emailed - waiting for it...")
            while time.time() < end and not CODE_FILE.exists():
                time.sleep(2)
            CODE_REQUEST.unlink(missing_ok=True)
            if not CODE_FILE.exists():
                break
            box.fill(CODE_FILE.read_text(encoding="utf-8").strip())
            CODE_FILE.unlink(missing_ok=True)
            _submit(page)
            continue
        err = _first_visible(page, ["[role=alert]", "text=/incorrect|invalid|wrong/i"])
        if err:
            sys.exit("GameChanger didn't accept the sign-in: " + (err.inner_text() or "").strip()[:200])
    sys.exit("Couldn't finish signing in to GameChanger.")


def looks_signed_out(page_text):
    """GameChanger shows public teams to anyone, with 'Sign In' and 'Join Us' in the page's top bar."""
    return bool(re.search(r"\bSign In\b.*\bJoin Us\b", page_text[:300], re.S))


def games_show(page, seconds=30):
    """Wait for the schedule's games to appear while signed in. The page can be slow, and being slow isn't being
    signed out; a signed-out visitor sees the games too, but none of the player's clips."""
    end = time.time() + seconds
    while True:
        if any(UUID_RE.search(a["href"]) for a in game_links(page)):
            return not looks_signed_out(page.evaluate("() => document.body.innerText") or "")
        if "/login" in page.url or time.time() >= end:
            return False
        page.wait_for_timeout(500)


def ensure_logged_in(page):
    page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    if games_show(page):
        return
    if _GC_LOGIN:
        auto_login(page)
    else:
        page.goto("https://web.gc.com/login", wait_until="domcontentloaded")
        if HEADLESS:
            sys.exit("Not signed in to GameChanger and no login was given (GC_EMAIL / GC_PASSWORD).")
        page.bring_to_front()
        print("\n>>> Sign in to GameChanger in the browser window that just opened.")
        print(">>> After you're signed in, come back here and press Enter.")
        try:
            input()
        except EOFError:
            sys.exit("GameChanger needs a sign-in. Run this from a window you can type in, and sign in when asked.")
    page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    if not games_show(page):
        sys.exit("Still can't see any games on the schedule page. Check the login and the team URL.")


def export_cookies(ctx, path=None):
    """Write the browser's cookies in Netscape format so yt-dlp can fetch signed video segments."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in ctx.cookies():
        domain = c["domain"]
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c["expires"])) if c.get("expires", -1) > 0 else "0",
            c["name"],
            c["value"],
        ]))
    Path(path or COOKIES_TXT).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- scan
def parse_schedule_json(data, out):
    items = data if isinstance(data, list) else (data.get("events") or data.get("data") or []) if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        ev = item.get("event", item)
        if not isinstance(ev, dict) or not ev.get("id"):
            continue
        pre = item.get("pregame_data") or {}
        start = (ev.get("start") or {}).get("datetime") if isinstance(ev.get("start"), dict) else ev.get("start")
        out[ev["id"]] = {
            "type": ev.get("event_type") or ev.get("type"),
            "start": start or "",
            "opponent": pre.get("opponent_name") or ev.get("opponent_name"),
            "home_away": pre.get("home_away"),
        }


def game_parts(info, link_text):
    """(date YYYY-MM-DD, opponent, away?) for one schedule entry."""
    date = ""
    if info.get("start"):
        try:
            date = dt.datetime.fromisoformat(info["start"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        except ValueError:
            pass
    opp = info.get("opponent")
    if not opp:
        first = [l for l in link_text.splitlines() if l.strip()]
        opp = first[0] if first else "Game"
    return date, opp, info.get("home_away") == "away"


def make_title(date, opp, away):
    t = TITLE_FORMAT.format(date=date, team=TEAM_NAME, sep="@" if away else "vs", opp=opp)
    return re.sub(r"\s+", " ", t).strip()[:100]


def game_date(g):
    """A game's date as YYYY-MM-DD, from its saved fields or its title."""
    if g.get("date"):
        return g["date"]
    m = re.search(r"\d{4}-\d{2}-\d{2}", g.get("title", ""))
    return m.group(0) if m else (g.get("start") or "")[:10]


def cmd_scan(args):
    global TEAM_NAME
    from playwright.sync_api import sync_playwright
    api = {}
    with sync_playwright() as p:
        ctx, page = open_browser(p)

        names = set()

        def on_response(resp):
            if "gc.com" not in resp.url or "json" not in (resp.headers.get("content-type") or ""):
                return
            try:
                data = resp.json()
            except Exception:
                return
            if isinstance(data, dict) and data.get("id") == TEAM_ID and isinstance(data.get("name"), str):
                names.add(data["name"].strip())
            if "schedule" in resp.url:
                try:
                    parse_schedule_json(data, api)
                except Exception:
                    pass

        page.on("response", on_response)
        ensure_logged_in(page)
        page.reload(wait_until="networkidle")
        for _ in range(20):                      # scroll so the whole season renders
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(400)
        links = game_links(page)
        try:
            body = page.evaluate("() => document.body.innerText") or ""
        except Exception:
            body = ""
        close_browser(ctx)
    if names and not TEAM_NAME:
        TEAM_NAME = sorted(names)[0]                    # titles below use it
        save_json(TEAM_JSON, {"name": TEAM_NAME})
        log(f"Team name from GameChanger: {sorted(names)[0]} (saved to {TEAM_JSON.name})")
    elif names and TEAM_NAME not in names:
        log(f"NOTE: GameChanger calls this team {sorted(names)}; TEAM_NAME in the script is '{TEAM_NAME}'.")
    elif not names and TEAM_NAME not in body:
        log(f"NOTE: couldn't confirm the team name '{TEAM_NAME}' on the schedule page; check one preview with 'details'.")

    existing = {g["id"]: g for g in load_json(GAMES_JSON, [])}
    if not api and not existing:
        sys.exit("GameChanger's schedule details didn't load, so no game list was saved. Run again in a minute.")
    games, seen = [], set()
    for a in links:
        m = UUID_RE.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        gid = m.group(1)
        seen.add(gid)
        info = api.get(gid, {})
        if info.get("type") and info["type"] != "game":
            continue                              # skip practices / other events
        old = existing.get(gid, {})
        date, opp, away = game_parts(info, a["text"]) if info else \
            (game_date(old), old.get("opponent") or (re.split(r" (?:vs|@) ", old.get("title", ""), maxsplit=1)[-1]
                                                     if re.search(r" (?:vs|@) ", old.get("title", "")) else "Game"),
             old["away"] if "away" in old else " @ " in old.get("title", ""))
        g = {"id": gid, "title": make_title(date, opp, away), "date": date, "opponent": opp, "away": away,
             "start": info.get("start") or old.get("start", ""), "skip": old.get("skip", False)}
        games.append(g)
    games.sort(key=lambda g: g.get("start") or "")
    save_json(GAMES_JSON, games)
    log(f"Found {len(games)} games -> {GAMES_JSON.name} (edit titles or set \"skip\": true before running)")
    for g in games:
        print(f"  {g['id']}  {g['title']}")


# ---------------------------------------------------------------- capture
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")


def make_mobile(page):
    """Make this tab look like an iPhone (screen size, touch, user agent) using Chrome's device emulation."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.setUserAgentOverride", {"userAgent": IPHONE_UA, "platform": "iPhone"})
    cdp.send("Emulation.setDeviceMetricsOverride",
             {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True})
    cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})


DUR_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:\d+\s*s)?\s*$")


def long_video_badges(page):
    """Length labels on video thumbnails ('1h 31m', '26m') that are long enough to be a full game."""
    found = []
    loc = page.get_by_text(re.compile(r"^\s*(\d+\s*h\s*)?\d+\s*m(\s*\d+\s*s)?\s*$"))
    try:
        n = min(loc.count(), 60)
    except Exception:
        return found
    for i in range(n):
        try:
            m = DUR_RE.match(loc.nth(i).inner_text())
        except Exception:
            continue
        if m and (m.group(1) or m.group(2)):
            mins = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
            if mins >= MIN_FULL_GAME_MINUTES:
                found.append(loc.nth(i))
    return found


def open_full_event_videos(page, urls):
    """Try each Videos page address; return (full-event tab/link or None, page has full-game videos?)."""
    pat = re.compile(r"full\s*(event|game)\s*videos?", re.I)
    for url in urls:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception:
            continue
        page.wait_for_timeout(6000)
        for loc in (page.get_by_role("tab", name=pat), page.get_by_role("link", name=pat),
                    page.get_by_role("button", name=pat)):
            try:
                if loc.count() > 0:
                    return loc.first, True
            except Exception:
                pass
        try:
            heading = page.get_by_text(pat).count() > 0
        except Exception:
            heading = False
        if heading:                                   # mobile layout: a "Full Event Videos" section, not a tab
            page.get_by_text(pat).first.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(1500)
            return None, True
        if long_video_badges(page):
            return None, True
    return None, False


def capture_streams(ctx, page, game_id, verbose=False):
    """Open the game's /videos page, play each recording, and collect the HLS playlist URLs it loads."""
    url = f"{TEAM_URL}/schedule/{game_id}/videos"
    hits = []

    def on_request(req):
        u = req.url
        if (".m3u8" in u or re.search(r"\.mp4(\?|$)", u)) and u not in hits:
            hits.append(u)

    urls = [url, f"https://web.gc.com/teams/{TEAM_ID}/schedule/{game_id}/videos/clips"]
    page.on("request", on_request)
    mpage = None
    tab, found = open_full_event_videos(page, urls)
    if not found:
        # The desktop site doesn't always offer "Full Event Videos"; the mobile site does. Try it as an iPhone.
        if verbose:
            print("No 'Full Event Videos' on the desktop page; trying the mobile view.")
            page.screenshot(path=str(ROOT / f"probe_{game_id}_desktop.png"), full_page=True)
        mpage = ctx.new_page()
        make_mobile(mpage)
        mpage.on("request", on_request)
        tab, found = open_full_event_videos(mpage, urls)
        page.remove_listener("request", on_request)
        page = mpage
        if not found:
            if verbose:
                print("No full-game video in the mobile view either.")
                page.screenshot(path=str(ROOT / f"probe_{game_id}.png"), full_page=True)
            page.remove_listener("request", on_request)
            mpage.close()
            return []
    if tab is not None:
        try:
            tab.click(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

    # Play each full-length video (found by its length label); fall back to clicking every player.
    badges = long_video_badges(page)
    if verbose:
        print(f"Found {len(badges)} full-length video(s) by length label.")
    for b in badges:
        try:
            b.scroll_into_view_if_needed(timeout=3000)
            b.click(timeout=3000, force=True)
            page.wait_for_timeout(5000)
            if page.url.split("?")[0].rstrip("/").endswith(("/videos", "/clips")) is False:
                page.go_back()
                page.wait_for_timeout(3000)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
    selectors = [] if badges else PLAY_SELECTORS

    for sel in selectors:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 20)):
            try:
                loc.nth(i).click(timeout=3000)
                page.wait_for_timeout(4000)
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass

    # <video src> that never went through a request we saw
    for src in page.eval_on_selector_all("video", "v => v.map(x => x.currentSrc || x.src).filter(Boolean)"):
        if src.startswith("http") and src not in hits:
            hits.append(src)
    page.remove_listener("request", on_request)

    if verbose:
        page.screenshot(path=str(ROOT / f"probe_{game_id}.png"), full_page=True)
        print("Captured URLs:")
        for h in hits:
            print("  ", h[:200])
    if mpage is not None:
        mpage.close()

    m3u8 = [h for h in hits if ".m3u8" in h]
    if not m3u8:
        return [h for h in hits if ".mp4" in h] if verbose else []

    masters, medias = [], []
    for u in m3u8:
        try:
            body = ctx.request.get(u).text()
        except Exception:
            continue
        (masters if "#EXT-X-STREAM-INF" in body else medias).append(u)
    if masters:
        chosen = masters
    else:                                         # one rendition per recording (group by folder)
        chosen, dirs = [], set()
        for u in medias:
            d = u.split("?")[0].rsplit("/", 2)[0]
            if d not in dirs:
                dirs.add(d)
                chosen.append(u)
    # collapse the same recording requested with different signatures
    out, keys = [], set()
    for u in chosen:
        k = u.split("?")[0]
        if k not in keys:
            keys.add(k)
            out.append(u)

    # recordings in time order (addresses contain .../YYYY/M/D/H/M/...), so parts join correctly
    def rec_time(u):
        m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})/(\d{1,2})/(\d{1,2})/", u)
        return tuple(int(x) for x in m.groups()) if m else (9999,)
    out.sort(key=rec_time)

    # keep only full-game recordings; drop highlight clips
    full = []
    for u in out:
        mins = playlist_minutes(ctx, u)
        if verbose:
            print(f"   {'FULL GAME' if mins >= MIN_FULL_GAME_MINUTES else 'clip (skipped)'}: {mins:.1f} min")
        if mins >= MIN_FULL_GAME_MINUTES:
            full.append(u)
    return full


def playlist_minutes(ctx, url):
    """Total length of an HLS playlist in minutes (follows a master playlist to its first variant)."""
    from urllib.parse import urljoin
    try:
        body = ctx.request.get(url).text()
        if "#EXT-X-STREAM-INF" in body:
            lines = body.splitlines()
            variant = next(lines[i + 1].strip() for i, l in enumerate(lines)
                           if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines))
            body = ctx.request.get(urljoin(url, variant)).text()
        return sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", body)) / 60
    except Exception:
        return 0.0


def download(url, dest, cookies=None, quiet=False):
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-part", "--quiet",
        "--ffmpeg-location", FFMPEG,
        "--cookies", str(cookies or COOKIES_TXT),
        "--add-header", "Referer: https://web.gc.com/",
        "--add-header", "Origin: https://web.gc.com",
        "--concurrent-fragments", "4",
        "-f", "bv*+ba/b", "--merge-output-format", "mp4",
        "-o", str(dest), url,
    ]
    if not quiet:
        cmd.insert(5, "--progress")
    subprocess.run(cmd, check=True)
    return dest


def concat(parts, dest):
    lst = dest.with_suffix(".txt.concat")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(dest)], check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    finally:
        lst.unlink(missing_ok=True)


# ---------------------------------------------------------------- game details (score, time, place, recap, box score)
DETAILS_DIR = ROOT / "descriptions"              # preview of every description + raw page text for debugging
INCLUDE_BOX_SCORE = False                         # box score is hard to read in a YouTube description
DESC_LIMIT = 4900                                 # YouTube allows 5000 bytes
TAB_LABELS = {"recap", "insights", "box score", "plays", "game stats", "videos", "starting lineup",
              "info", "lineup", "stats", "spray chart", "team stats"}
FOOTER_RE = re.compile(r"©|privacy policy|terms of (service|use)|download the (gamechanger )?app|cookie|"
                       r"^copy recap story link$|^get the app$|^visit gc\.com$|^keyboard shortcuts$", re.I)
NOTE_RE = re.compile(r"^[A-Z0-9]{1,4}:\s")
DATE_RE = re.compile(
    r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,? [A-Z][a-z]{2,8} \d{1,2}(?:, \d{4})?,? \d{1,2}:\d{2} ?[AP]M"
    r"(?: ?[-–] ?\d{1,2}:\d{2} ?[AP]M)?(?: ?(?:PDT|PST|PT|MDT|MST|MT|CDT|CST|CT|EDT|EST|ET))?)")
CELL_RE = re.compile(r"^(\d+|X|x|-)$")
INFO_LABELS = {"location", "venue", "field", "address", "place", "where"}
OTHER_LABELS = {"keyboard shortcuts", "edit", "rsvp", "players", "staff / family", "date", "time", "arrival", "arrive", "notes", "uniform", "opponent", "home", "away",
                "event", "start", "end", "duration", "home/away", "type"}

TABLES_JS = """() => {
  const sel = 'table, [role="table"], [role="grid"]';
  const cellSel = 'th, td, [role="cell"], [role="columnheader"], [role="rowheader"], [role="gridcell"]';
  return [...document.querySelectorAll(sel)]
    .filter(t => !t.parentElement || !t.parentElement.closest(sel))
    .map(t => {
      const rows = [...t.querySelectorAll('tr, [role="row"]')]
        .map(r => [...r.querySelectorAll(cellSel)].map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim()))
        .filter(r => r.length);
      let cap = '';
      const c = t.querySelector('caption');
      if (c) cap = c.innerText.trim();
      let el = t;
      for (let i = 0; i < 5 && el && !cap; i++) {
        let p = el.previousElementSibling;
        while (p && !cap) {
          const tx = (p.innerText || '').trim();
          if (tx && tx.length < 80) cap = tx.split('\\n')[0];
          p = p.previousElementSibling;
        }
        el = el.parentElement;
      }
      return {caption: cap, rows};
    });
}"""


def body_lines(page):
    txt = page.evaluate("() => document.body.innerText") or ""
    return [l.strip() for l in txt.splitlines() if l.strip()]


def split_page(lines):
    """Split a game page into (header above the tab bar, content of the selected tab)."""
    for i, l in enumerate(lines):
        if l.lower() in TAB_LABELS and sum(1 for x in lines[i:i + 10] if x.lower() in TAB_LABELS) >= 3:
            j = i
            while j + 1 < len(lines) and lines[j + 1].lower() in TAB_LABELS:
                j += 1
            content = lines[j + 1:]
            for k, c in enumerate(content):
                if FOOTER_RE.search(c):
                    content = content[:k]
                    break
            return lines[:i], content
    return lines, []


def parse_header(header):
    """Header lines look like: date | FINAL | RNCH | TR12 | 1<tab>2<tab>3 | 2<tab>0<tab>5 | 1<tab>0<tab>1 | R<tab>H<tab>E | 7<tab>3<tab>0 | 4<tab>2<tab>0"""
    out = {}
    joined = " ".join(header)
    m = DATE_RE.search(joined)
    if m:
        out["when"] = m.group(1).strip()
    m = re.search(r"Arrive\s*(\d{1,2}:\d{2}\s?[AP]M)", joined)
    if m:
        out["arrive"] = m.group(1)
    split = lambda l: re.split(r"\s+", l.strip())
    for i, l in enumerate(header):
        if re.fullmatch(r"1(\s+\d+)*", l) and i >= 2 and i + 2 < len(header):
            innings = split(l)
            names = [x for x in header[max(0, i - 2):i] if not re.search(r"\d\s", x)]
            vals = [split(header[i + 1]), split(header[i + 2])]
            try:
                r = header.index("R\tH\tE", i) if "R\tH\tE" in header else \
                    next(k for k in range(i, len(header)) if split(header[k]) == ["R", "H", "E"])
                rhe = [split(header[r + 1]), split(header[r + 2])]
            except StopIteration:
                break
            if len(names) == 2 and all(len(v) == 3 for v in rhe):
                out["innings"] = innings
                out["rows"] = [(names[k], vals[k] + rhe[k]) for k in range(2)]
                # the scoreboard ends with both full team names, in the same order as the line-score rows
                full = [x for x in header[r + 3:] if not re.search(r"\d", x) or not re.fullmatch(r"[\d\s]+", x)]
                full = [x for x in full if not re.fullmatch(r"[\d\s]+", x)][:2]
                if len(full) == 2:
                    out["row_full"] = full
            break
    return out


def parse_location(info, recap):
    def block(label):
        for i, l in enumerate(info):
            if l.lower().rstrip(":") == label:
                got = []
                for x in info[i + 1:i + 5]:
                    low = x.lower().rstrip(":")
                    if low in INFO_LABELS or low in OTHER_LABELS:
                        break
                    if re.fullmatch(r"no (location|notes|address|field)( set| added)?", low):
                        continue                       # GameChanger's placeholder text
                    got.append(x)
                return got
        return []
    addr = []
    for lab in ("location", "venue", "address", "where", "field", "place"):
        addr = addr or block(lab)
    notes = block("notes")
    m = re.search(r" at ((?:[A-Z0-9][\w.'()-]*\s)*?[A-Z0-9]?[\w.'()-]*?(?:Park|Field|Fields|School|Complex|Center|"
                  r"Stadium|Academy|Diamond|Road|Rd|Street|Avenue|Drive|Blvd))\b", " ".join(recap))
    parts = ([m.group(1)] if m else []) + addr
    where = ", ".join(parts)
    if notes and where and len(" ".join(notes)) <= 60:
        where += f" ({' '.join(notes)})"
    return where


def collapse_numbers(lines):
    out = []
    for l in lines:
        if out and CELL_RE.match(l) and re.search(r"(^|\s)(\d+|-)$", out[-1]):
            out[-1] += " " + l
        else:
            out.append(l)
    return out


def format_box(tables, box_lines):
    stat_tables = [t for t in tables
                   if len(t.get("rows") or []) >= 2 and len(t["rows"][0]) >= 4
                   and t["rows"][0][0] and not t["rows"][0][0].isdigit()]
    starts = [(i - 1, box_lines[i - 1]) for i, l in enumerate(box_lines)
              if l.upper() in ("LINEUP", "BATTING") and i > 0]
    pitch_idx = [i for i, l in enumerate(box_lines) if l.upper() == "PITCHING"]

    def team_at(i):
        k = max((n for n, (s0, _) in enumerate(starts) if s0 <= i), default=None)
        return k

    notes = {}
    for i, l in enumerate(box_lines):
        if NOTE_RE.match(l):
            k = team_at(i)
            if k is None:
                continue
            nxt = starts[k + 1][0] if k + 1 < len(starts) else len(box_lines)
            pitching = any(starts[k][0] < p < i for p in pitch_idx if p < nxt)
            notes.setdefault((k, pitching), []).append(l.replace("\xa0", " "))

    blocks, k = [], -1
    for t in stat_tables:
        hdr, rows = t["rows"][0], t["rows"][1:]
        kind = hdr[0].upper()
        if kind in ("LINEUP", "BATTING"):
            k += 1
            if k < len(starts):
                blocks.append(f"== {starts[k][1]} ==")
        pitching = kind == "PITCHING"
        label = "Pitching" if pitching else "Batting" if kind in ("LINEUP", "BATTING") else hdr[0].title()
        lines = [f"{label} ({' '.join(hdr[1:])})"]
        lines += [f"{r[0]}: {' '.join(r[1:])}" for r in rows if any(r)]
        lines += notes.get((max(k, 0), pitching), [])
        blocks.append("\n".join(lines))
    if blocks:
        return "\n\n".join(blocks).replace("\n\n== ", "\n\n\n== ")
    return "\n".join(collapse_numbers(box_lines))


def clean(s):
    return s.replace("<", "").replace(">", "")


def fetch_details(page, game, dump=True):
    gid = game["id"]
    base = f"{TEAM_URL}/schedule/{gid}"
    d = {"header": [], "recap": [], "box": [], "box_tables": [], "info": []}
    page.goto(base + "/recap", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    d["header"], d["recap"] = split_page(body_lines(page))
    tabs = (("Box score", "box"), ("Info", "info")) if INCLUDE_BOX_SCORE else (("Info", "info"),)
    for label, key in tabs:
        try:
            page.get_by_text(re.compile(rf"^\s*{label}\s*$", re.I)).first.click(timeout=5000)
            page.wait_for_timeout(4000)
            _, d[key] = split_page(body_lines(page))
            if key == "box":
                d["box_tables"] = page.evaluate(TABLES_JS)
        except Exception as e:
            log(f"   couldn't open the {label} tab ({e.__class__.__name__})")
    if dump:
        DETAILS_DIR.mkdir(exist_ok=True)
        save_json(DETAILS_DIR / f"{gid}.raw.json", d)
    return d


def build_description(game, d):
    gid = game["id"]
    game_url = f"{TEAM_URL}/schedule/{gid}"
    h = parse_header(d["header"])
    title = game["title"]
    opp = game.get("opponent") or (re.split(r" (?:vs|@) ", title, maxsplit=1)[-1]
                                   if re.search(r" (?:vs|@) ", title) else "Opponent")
    away = game["away"] if "away" in game else " @ " in title
    date = game_date(game)
    year = date[:4]

    top = []
    us = them = None
    row_names = None

    def abbrev_fits(ab, name):
        it = iter(re.sub(r"[^A-Z0-9]", "", name.upper()))
        return all(ch in it for ch in ab.upper())

    if "rows" in h:
        n = len(h["innings"])
        (a_ab, a_v), (b_ab, b_v) = h["rows"]
        a, b = int(a_v[n]), int(b_v[n])
        a_us = abbrev_fits(a_ab, TEAM_NAME) and not abbrev_fits(a_ab, opp)
        b_us = abbrev_fits(b_ab, TEAM_NAME) and not abbrev_fits(b_ab, opp)
        full = [x.lower() for x in h.get("row_full", [])]
        if TEAM_NAME.lower() in full:                       # most reliable: full names on the scoreboard
            first_is_us = full[0] == TEAM_NAME.lower()
            away = first_is_us                              # first row is the visiting team
        else:
            first_is_us = a_us if a_us != b_us else away
        us, them = (a, b) if first_is_us else (b, a)
        row_names = [TEAM_NAME, opp] if first_is_us else [opp, TEAM_NAME]
    top.insert(0, f"{TEAM_NAME} {'@' if away else 'vs'} {opp}, {date}".strip(", "))
    if us is not None:
        res = "W" if us > them else "L" if us < them else "T"
        top.append(f"Final: {TEAM_NAME} {us}, {opp} {them} ({res})")
    if h.get("when"):
        when = h["when"]
        if year and not re.search(r"\d{4}", when):
            when = re.sub(r"^(\w+,? [A-Z][a-z]+ \d{1,2})", rf"\1, {year}", when)
        top.append("When: " + when + (f" (arrive {h['arrive']})" if h.get("arrive") else ""))
    loc = parse_location(d["info"], d["recap"])
    if loc:
        top.append("Where: " + loc)
    linescore = ""
    if "rows" in h:
        n = len(h["innings"])
        ls = ["Line score (innings " + " ".join(h["innings"]) + ")"]
        for k, (name, vals) in enumerate(h["rows"]):
            name = row_names[k] if row_names else name
            ls.append(f"{name}: {' '.join(vals[:n])}  |  R {vals[n]}  H {vals[n + 1]}  E {vals[n + 2]}")
        linescore = "\n".join(ls)

    rc_lines = d["recap"]
    for k, c in enumerate(rc_lines):
        if FOOTER_RE.search(c):
            rc_lines = rc_lines[:k]
            break
    if any(l.startswith(("Oops", "Error loading")) for l in rc_lines[:3]):
        rc_lines = []                                  # GameChanger has no recap for this game
    recap = "\n\n".join(rc_lines).strip()
    box = format_box(d.get("box_tables") or [], d["box"]).strip() if INCLUDE_BOX_SCORE else ""
    footer = f"Full game video from GameChanger: {game_url}"

    def assemble(rc, bx):
        parts = ["\n".join(top)]
        if linescore:
            parts.append(linescore)
        if rc:
            parts.append("RECAP\n" + rc)
        if bx:
            parts.append("BOX SCORE\n" + bx)
        parts.append(footer)
        return clean("\n\n".join(parts))

    def size(s):
        return len(s.encode("utf-8"))

    desc = assemble(recap, box)
    if size(desc) > DESC_LIMIT:                    # trim the box score first, then the recap
        room = DESC_LIMIT - size(assemble(recap, "")) - 40
        box = (box[:max(room, 0)].rsplit("\n", 1)[0] + "\n... (full box score on GameChanger)") if room > 200 else ""
        desc = assemble(recap, box)
    if size(desc) > DESC_LIMIT:
        room = DESC_LIMIT - size(assemble("", box)) - 10
        recap = recap[:max(room, 0)].rsplit(" ", 1)[0] + " ..."
        desc = assemble(recap, box)
    DETAILS_DIR.mkdir(exist_ok=True)
    (DETAILS_DIR / f"{safe_name(title)}.txt").write_text(desc, encoding="utf-8")
    return desc


# ---------------------------------------------------------------- youtube
class QuotaExceeded(Exception):
    pass


def safe_console():
    """Team names can hold emoji; printing one must never stop a run (Windows consoles and redirected output)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def youtube(check_only=False):
    """Signed-in YouTube client. check_only: just test the saved sign-in - never open a sign-in page or remove it."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not CLIENT_SECRET.exists() and not TOKEN.exists():
        sys.exit(f"Missing {CLIENT_SECRET.name}. Create one in Google Cloud Console (see README).")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if TOKEN.exists():
        saved = set(json.loads(TOKEN.read_text()).get("scopes") or [])
        if not set(SCOPES) <= saved:                     # new permission (playlists) needed: sign in again
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:                         # revoked or too old: sign in again
                creds = None
        if creds and creds.valid:
            pass
        elif HEADLESS or check_only:
            sys.exit("The YouTube sign-in has expired - connect YouTube again.")
        else:
            print(f"\n>>> A Google sign-in page will open. Choose the '{CHANNEL_NAME}' channel.\n")
            creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES).run_local_server(
                port=0, prompt="consent select_account")
        TOKEN.write_text(creds.to_json())
    yt = build("youtube", "v3", credentials=creds)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    handle = (items[0]["snippet"].get("customUrl", "") if items else "").lstrip("@").lower()
    if EXPECTED_HANDLE and handle != EXPECTED_HANDLE:
        if not check_only:
            TOKEN.unlink(missing_ok=True)
        sys.exit(f"Signed in to channel '@{handle}', not '@{EXPECTED_HANDLE}'. Run again and pick the right channel.")
    log(f"YouTube: {'signed in to' if check_only else 'uploading to'} @{handle}")
    return yt


def get_playlist(yt, state):
    """Find the season playlist by title, or create it (Unlisted)."""
    pid = state.get("_playlist")
    if pid:
        return pid
    req = yt.playlists().list(part="snippet", mine=True, maxResults=50)
    while req is not None:
        resp = req.execute()
        for item in resp.get("items", []):
            if item["snippet"]["title"].strip() == playlist_title():
                pid = item["id"]
                break
        if pid:
            break
        req = yt.playlists().list_next(req, resp)
    if not pid:
        pid = yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": playlist_title(),
                        "description": playlist_description()},
            "status": {"privacyStatus": PLAYLIST_PRIVACY},
        }).execute()["id"]
        log(f"Created playlist: https://www.youtube.com/playlist?list={pid}")
    state["_playlist"] = pid
    save_json(STATE_JSON, state)
    return pid


def add_to_playlist(yt, state, gid):
    """Add a game's uploaded video(s) to the playlist once, at its date position; safe to call repeatedly."""
    from googleapiclient.errors import HttpError
    entry = state.get(gid) or {}
    done = set(entry.get("in_playlist", []))
    todo = [v for v in entry.get("videos", []) if v not in done]
    if not todo:
        return
    my_key = game_date(entry)                             # playlist stays in date order
    try:
        pid = get_playlist(yt, state)
        for vid in todo:
            earlier = sum(len(v.get("in_playlist", [])) for k, v in state.items()
                          if not k.startswith("_") and isinstance(v, dict) and k != gid
                          and game_date(v) < my_key)
            position = earlier + len(done)
            body = {"snippet": {"playlistId": pid, "position": position,
                                "resourceId": {"kind": "youtube#video", "videoId": vid}}}
            for attempt in range(6):
                try:
                    yt.playlistItems().insert(part="snippet", body=body).execute()
                    break
                except HttpError as e:
                    # a brand-new playlist can answer 409/5xx for a short while
                    if e.resp.status in (409, 500, 503) and attempt < 5:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if e.resp.status == 400 and "position" in body["snippet"]:
                        body["snippet"].pop("position")        # fall back to adding at the end
                        continue
                    raise
            done.add(vid)
            log(f"   added to playlist: {entry.get('title', vid)}")
    except HttpError as e:
        log(f"   couldn't add to playlist yet ({e.resp.status}); will retry next run")
    entry["in_playlist"] = sorted(done)
    state[gid] = entry
    save_json(STATE_JSON, state)


def upload(yt, path, title, description):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {"title": title, "description": description, "categoryId": CATEGORY_ID,
                    "tags": video_tags()},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": MADE_FOR_KIDS},
    }
    media = MediaFileUpload(str(path), chunksize=16 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, retries, last_pct = None, 0, -1
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                pct = int(status.progress() * 100) // 10 * 10
                if pct != last_pct:
                    log(f"   uploading {title[:60]}: {pct}%")
                    last_pct = pct
        except HttpError as e:
            reason = str(e)
            if (e.resp.status == 429 or "quotaExceeded" in reason or "uploadLimitExceeded" in reason
                    or "rateLimitExceeded" in reason or "Quota exceeded" in reason):
                raise QuotaExceeded(reason)
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
        except (ConnectionError, TimeoutError, OSError) as e:
            # dropped connection (VPN switch, Wi-Fi blip): wait and resume the same upload where it left off
            if retries >= 10:
                raise
            retries += 1
            wait = min(15 * retries, 120)
            log(f"   connection dropped ({e.__class__.__name__}); resuming upload in {wait}s")
            time.sleep(wait)
    return resp["id"]


# ---------------------------------------------------------------- run
def wait_for_disk_space(title):
    """Hold a download while the drive is nearly full; uploads free space as they finish."""
    warned = False
    while shutil.disk_usage(WORK).free / 1e9 < MIN_FREE_GB:
        if not warned:
            log(f"   {title}: waiting for disk space (less than {MIN_FREE_GB} GB free)")
            warned = True
        time.sleep(60)


def download_game(job, cookies):
    """Runs in a worker thread: download every recording of one game and join them. Returns [(path, title)]."""
    g, streams = job["game"], job["streams"]
    title, base = g["title"], safe_name(g["title"])
    final = WORK / f"{base}.mp4"
    if len(streams) > 1 and final.exists():
        return [(final, title)]                       # joined on an earlier run
    parts_done = [WORK / f"{base} - part{i}.mp4" for i in range(1, len(streams) + 1)]
    if all(pth.exists() and not Path(str(pth) + ".ytdl").exists() for pth in parts_done):
        log(f"   already downloaded: {title}")
    else:
        wait_for_disk_space(title)
        t0 = time.time()
        log(f"   download started: {title}")
    parts = [download(s, WORK / f"{base} - part{i}.mp4", cookies=cookies, quiet=True)
             for i, s in enumerate(streams, 1)]
    if "t0" in locals():
        size = sum(pth.stat().st_size for pth in parts) / 1e9
        log(f"   download finished: {title} ({size:.1f} GB, {(time.time() - t0) / 60:.0f} min)")
    if len(parts) == 1:
        return [(parts[0], title)]
    if concat(parts, final):
        for pth in parts:
            pth.unlink(missing_ok=True)
        return [(final, title)]
    log(f"   {title}: recordings couldn't be joined losslessly; uploading as separate parts")
    return [(pth, f"{title} (Part {i})"[:100]) for i, pth in enumerate(parts, 1)]


def next_quota_retry():
    """YouTube quotas reset at midnight Pacific: retry hourly, or right after midnight if that comes sooner."""
    now = dt.datetime.now()                                  # this PC is on Pacific time
    after_midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return min(after_midnight, now + dt.timedelta(hours=QUOTA_RETRY_HOURS))


def upload_with_quota_wait(yt, pth, t, desc):
    """Upload, and if the daily limit is used up, wait for the reset and try again (downloads keep running)."""
    while True:
        try:
            return upload(yt, pth, t, desc)
        except QuotaExceeded:
            when = next_quota_retry()
            log(f"YouTube's daily upload limit is used up. Downloads continue; next upload try at {when:%a %I:%M %p}.")
            while dt.datetime.now() < when:
                time.sleep(30)


def uploader(yt, state, jobs_q, results):
    """Runs in its own thread: uploads games in date order as their downloads finish.
    Games that fail get one more try at the end of the run."""
    retry, second_pass = [], False
    while True:
        job = jobs_q.get() if not second_pass else (retry.pop(0) if retry else None)
        if job is None:
            if not second_pass and retry:
                second_pass = True
                log(f"Retrying {len(retry)} game(s) that failed earlier in this run")
                results["failed"].clear()
                continue
            return
        g = job["game"]
        gid, title = g["id"], g["title"]
        game_url = f"{TEAM_URL}/schedule/{gid}"
        try:
            uploads = job["future"].result()
        except Exception as e:
            log(f"{title}: download failed ({e}); will retry next run")
            results["failed"].append(title)
            continue
        finally:
            job["cookies"].unlink(missing_ok=True)
        log(f"Uploading {title}")
        ids = []
        try:
            for pth, t in uploads:
                vid = upload_with_quota_wait(yt, pth, t, job["desc"])
                ids.append(vid)
                log(f"   uploaded: https://youtu.be/{vid}")
                if DELETE_AFTER_UPLOAD:
                    pth.unlink(missing_ok=True)
                    pth.with_suffix(".txt").write_text(
                        f"{t}\nUploaded {dt.datetime.now():%Y-%m-%d %H:%M} as {PRIVACY}: https://youtu.be/{vid}\n"
                        f"Source: {game_url}\n", encoding="utf-8")
        except Exception as e:
            log(f"{title}: upload failed ({e}); " + ("will retry next run" if second_pass else "will retry at the end of this run"))
            results["failed"].append(title)
            if not second_pass and not ids:
                retry.append(job)
            if ids:
                state[gid] = {"title": title, "date": game_date(g), "videos": ids, "partial": True}
                save_json(STATE_JSON, state)
            continue
        state[gid] = {"title": title, "date": game_date(g), "videos": ids,
                      "uploaded": dt.datetime.now().isoformat(timespec="seconds")}
        save_json(STATE_JSON, state)
        add_to_playlist(yt, state, gid)
        results["uploaded"] += 1


def cmd_run(args):
    if not TEAM_NAME:
        sys.exit("The team name isn't known yet. Run:  py gc_to_youtube.py scan")
    import queue
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from playwright.sync_api import sync_playwright
    check_tools()
    games = load_json(GAMES_JSON, [])
    if not games:
        sys.exit("No games.json yet. Run:  python gc_to_youtube.py scan")
    state = load_json(STATE_JSON, {})
    todo = [g for g in games if not g.get("skip") and g["id"] not in state]
    already = [k for k, v in state.items() if not k.startswith("_") and isinstance(v, dict)
               and set(v.get("videos", [])) - set(v.get("in_playlist", []))]
    if not todo and not already:
        log("Nothing left to do. Every game is already uploaded and in the playlist.")
        return
    yt = youtube()
    WORK.mkdir(exist_ok=True)
    for gid in already:                                  # earlier uploads not yet in the playlist
        add_to_playlist(yt, state, gid)
    if not todo:
        log("Done.")
        return
    limit = args.limit or len(todo)
    log(f"{len(todo)} game(s) left; downloading {args.parallel} at a time. If YouTube's daily limit is "
        f"reached, this keeps downloading and resumes uploading after the reset. Leave it running.")

    # Uploads run in their own thread, in date order, as soon as each download is ready.
    jobs_q, results = queue.Queue(), {"uploaded": 0, "failed": []}
    up_thread = threading.Thread(target=uploader, args=(yt, state, jobs_q, results), daemon=True)
    up_thread.start()

    skipped, queued = [], 0
    pool = ThreadPoolExecutor(max_workers=max(1, args.parallel))
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            ensure_logged_in(page)
            for n, g in enumerate(todo, 1):
                if queued >= limit:
                    break
                gid, title = g["id"], g["title"]
                log(f"({n}/{len(todo)}) checking {title}")
                streams = capture_streams(ctx, page, gid)
                if not streams:
                    log("   no full-game video for this game; skipping")
                    skipped.append(title)
                    continue
                try:
                    desc = build_description(g, fetch_details(page, g))
                except Exception as e:
                    log(f"   couldn't read game details ({e}); using a short description")
                    desc = (f"{title}\nFull game video from GameChanger: "
                            f"{TEAM_URL}/schedule/{gid}")
                cookies = ROOT / f"gc_cookies_{gid[:8]}.txt"
                export_cookies(ctx, cookies)
                job = {"game": g, "streams": streams, "desc": desc, "cookies": cookies}
                job["future"] = pool.submit(download_game, job, cookies)
                jobs_q.put(job)
                queued += 1
            close_browser(ctx)
        log(f"Finished checking games: {queued} to upload, {len(skipped)} without a full-game video.")
        jobs_q.put(None)
        while up_thread.is_alive():
            up_thread.join(timeout=1)                    # short waits keep Ctrl+C working
    except KeyboardInterrupt:
        log("Stopped. Run again any time; finished uploads are saved and downloaded files are reused.")
        pool.shutdown(wait=False, cancel_futures=True)
        return
    pool.shutdown(wait=True)

    log(f"Uploaded {results['uploaded']} game(s) this run.")
    if skipped:
        log(f"Skipped {len(skipped)} game(s) with no full-game video:")
        for t in skipped:
            print("   ", t)
    if results["failed"]:
        log(f"{len(results['failed'])} game(s) had a problem and will be retried next run:")
        for t in results["failed"]:
            print("   ", t)
    log("Done.")


def cmd_probe(args):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        ensure_logged_in(page)
        streams = capture_streams(ctx, page, args.game_id, verbose=True)
        print("\nWould download:")
        for s in streams:
            print("  ", s[:200])
        print(f"\nScreenshot saved: probe_{args.game_id}.png")
        input("Press Enter to close the browser...")
        close_browser(ctx)


def cmd_netlog(args):
    """Diagnostic: record what GameChanger's servers send for one game's pages (to locate full-game videos)."""
    from playwright.sync_api import sync_playwright
    gid = args.game_id
    log_items = []

    def on_response(resp):
        u = resp.url
        ctype = resp.headers.get("content-type") or ""
        if "gc.com" not in u or not any(k in ctype for k in ("json", "mpegurl", "text/plain")):
            return
        try:
            body = resp.text()
        except Exception:
            body = ""
        try:
            req_headers = sorted(resp.request.headers.keys())     # header names only, never values
        except Exception:
            req_headers = []
        log_items.append({"url": u, "status": resp.status, "type": ctype, "method": resp.request.method,
                          "request_headers": req_headers, "body": body[:200000]})

    with sync_playwright() as p:
        ctx, page = open_browser(p)
        ensure_logged_in(page)
        page.on("response", on_response)
        base = f"https://web.gc.com/teams/{TEAM_ID}"
        for u in (f"{TEAM_URL}/schedule/{gid}/videos", f"{base}/schedule/{gid}/videos/clips",
                  f"{TEAM_URL}/schedule/{gid}/recap"):
            log(f"loading {u}")
            page.goto(u, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            for _ in range(15):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(400)
            if u.endswith("/videos"):
                tab = page.get_by_text(re.compile(r"full\s*(event|game)\s*videos?", re.I))
                if tab.count():
                    log("clicking Full Event Videos and playing the first video")
                    tab.first.click(timeout=5000)
                    page.wait_for_timeout(5000)
                    for sel in PLAY_SELECTORS:
                        loc = page.locator(sel)
                        if loc.count():
                            try:
                                loc.first.click(timeout=3000)
                                page.wait_for_timeout(6000)
                                break
                            except Exception:
                                pass
        page.remove_listener("response", on_response)
        close_browser(ctx)
    out = ROOT / f"netlog_{gid[:8]}.json"
    save_json(out, log_items)
    hits = [i["url"] for i in log_items if re.search(r"m3u8|\.mp4|stream|vod|video", i["body"] + i["url"], re.I)]
    log(f"Saved {len(log_items)} responses to {out.name}; {len(hits)} mention video.")


def cmd_fix_descriptions(args):
    """Rebuild the description of every uploaded video and update it on YouTube (title and settings untouched)."""
    state = load_json(STATE_JSON, {})
    games = {g["id"]: g for g in load_json(GAMES_JSON, [])}
    gids = [k for k, v in state.items() if not k.startswith("_") and isinstance(v, dict) and v.get("videos")]
    if not gids:
        log("No uploaded videos yet.")
        return
    yt = youtube()
    for gid in gids:
        g = games.get(gid, {"id": gid, "title": state[gid].get("title", gid)})
        raw = DETAILS_DIR / f"{gid}.raw.json"
        d = load_json(raw, None)
        if d is None:
            log(f"   no saved page text for {g['title']}; run 'details all' first")
            continue
        desc = build_description(g, d)
        for vid in state[gid]["videos"]:
            items = yt.videos().list(part="snippet", id=vid).execute().get("items", [])
            if not items:
                log(f"   {vid} not found on YouTube")
                continue
            sn = items[0]["snippet"]
            if sn.get("description", "") == desc:
                log(f"   {g['title']}: already up to date")
                continue
            body = {"id": vid, "snippet": {"title": sn["title"], "categoryId": sn["categoryId"],
                                           "description": desc, "tags": sn.get("tags", [])}}
            yt.videos().update(part="snippet", body=body).execute()
            log(f"   updated description: {g['title']}")
    log("Done.")


def cmd_details(args):
    """Preview descriptions without downloading or uploading anything."""
    if not TEAM_NAME:
        sys.exit("The team name isn't known yet. Run:  py gc_to_youtube.py scan")
    from playwright.sync_api import sync_playwright
    games = load_json(GAMES_JSON, [])
    if args.game_id != "all":
        games = [g for g in games if g["id"] == args.game_id] or [{"id": args.game_id, "title": args.game_id}]
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        ensure_logged_in(page)
        for g in games:
            log(g["title"])
            desc = build_description(g, fetch_details(page, g))
            if len(games) == 1:
                print("\n" + desc + "\n")
        close_browser(ctx)
    log(f"Previews saved in {DETAILS_DIR}")


def main():
    safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    pr = sub.add_parser("probe")
    pr.add_argument("game_id")
    sub.add_parser("fix-descriptions")
    nl = sub.add_parser("netlog")
    nl.add_argument("game_id")
    de = sub.add_parser("details")
    de.add_argument("game_id", help="a game id, or 'all'")
    rn = sub.add_parser("run")
    rn.add_argument("--limit", type=int, default=0, help="only handle the next N games with video (default: all)")
    rn.add_argument("--parallel", type=int, default=PARALLEL_DOWNLOADS, help="games to download at once")
    args = ap.parse_args()
    {"scan": cmd_scan, "probe": cmd_probe, "details": cmd_details, "fix-descriptions": cmd_fix_descriptions, "netlog": cmd_netlog, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
