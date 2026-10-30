"""
Player highlight reel from GameChanger clips -> one YouTube video (Unlisted).

  py highlights.py list      fetch the player's clips and show which ones go in the reel (changes nothing)
  py highlights.py run       list -> download -> build the reel -> upload Unlisted
  py highlights.py build     download and build the reel only (no upload)
  py highlights.py upload    upload an already-built reel

Picks:  batting  - singles, doubles, triples, home runs
        defense  - strikeouts (incl. dropped 3rd strike), outs, fielder's choices, double/triple plays
Uses the team settings, GameChanger login and YouTube sign-in of gc_to_youtube.py in this folder.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
import gc_to_youtube as gc  # noqa: E402
import sh_config  # noqa: E402
import smart_cut  # noqa: E402

ROOT = gc.BASE                       # this folder, or SMART_HIGHLIGHTS_WORKDIR when set

# ---------------------------------------------------------------- settings
PLAYER_ID = "YOUR_PLAYER_ID"                     # from https://web.gc.com/teams/<team>/<slug>/players/<PLAYER_ID>
PLAYER_NAME = "Player Name"
VIDEO_TITLE = "Highlights - Player Name - Team Name Fall 2024"
OFFENSE_PLAYS = {"single", "double", "triple", "home_run", "inside_the_park_home_run", "inside_the_park_homerun"}
DEFENSE_PLAY_RE = re.compile(r"strikeout|dropped_third_strike|(^|_)outs?($|_)|fielders?_choice|double_play|"
                             r"triple_play|caught_stealing|pick_?off", re.I)   # pitching or fielding outs
ADD_TO_TEAM_PLAYLIST = True
QUICK_TITLE = "Smart Highlights: Player Name - Team Name Fall 2024"
# Quick reel: seconds kept BEFORE / AFTER the moment the play was recorded, by play type.
# If clips start too late, raise the first number; if they cut off too early, raise the second.
QUICK_WINDOWS = {
    "home_run": (22, 3), "inside_the_park_home_run": (22, 3), "triple": (18, 3), "double": (15, 3), "single": (13, 3),
    "strikeout": (8, 2), "dropped_third_strike": (9, 3), "dropped_third_strike_batter_out": (9, 3),
    "double_play": (14, 3), "triple_play": (16, 3), "caught_stealing": (10, 2),
    "_out": (12, 3),                                 # every other out / fielder's choice
}
QUICK_START_EARLIER = 4      # seconds: every quick clip starts this much earlier (they were a few seconds late)
QUICK_END_EARLIER = 1        # seconds: and ends this much earlier
QUICK = False
SMART_ZOOM = 1.2         # quick reel: zoom in gently toward the action (1 = no zoom)
AI_CHECK = True          # quick reel: let a vision model check each cut, ONLY if anthropic_key.txt is in this folder
TOP_N = 0                # --top N: keep only the N most exciting plays (0 = all)
TOP_TITLE = "Smart Highlights: Top {n} - Player Name - Team Name Fall 2024"
MIN_READY = 0.9           # don't build/upload the reel unless at least 90% of the picked clips downloaded
LABEL_SECONDS = 5            # how long the play name / game caption shows at the start of each clip
CAPTION_ALWAYS = True        # True: caption stays on for the whole clip; False: only the first LABEL_SECONDS
                             # (or choose per run:  --caption always  /  --caption brief)
WIDTH, HEIGHT, FPS = 1280, 720, 30
PARALLEL = 4

globals().update(sh_config.player_settings())    # a settings file (SMART_HIGHLIGHTS_CONFIG) replaces the above

PLAYER_URL = f"{gc.TEAM_URL}/players/{PLAYER_ID}"
API = "https://api.team-manager.gc.com"
WORK = ROOT / "highlights" / re.sub(r"[^\w]+", "_", PLAYER_NAME)
CLIPS_JSON = WORK / "clips.json"
REEL = WORK / f"{gc.safe_name(VIDEO_TITLE)}.mp4"
NOTE = WORK / f"{gc.safe_name(VIDEO_TITLE)}.txt"
log = gc.log


# ---------------------------------------------------------------- GameChanger
class GCSession:
    """Use the browser's GameChanger login to call the same API the website uses."""

    def __init__(self, p):
        self.ctx, self.page = gc.open_browser(p)
        gc.ensure_logged_in(self.page)
        self.headers, self.search_body, self.pb_headers = None, None, None
        self.reported = False

        def clean(req):
            try:
                hdrs = req.all_headers()                     # includes the login header the website adds
            except Exception:
                hdrs = req.headers
            return {k: v for k, v in hdrs.items()
                    if not k.startswith(":") and k.lower() not in
                    ("content-length", "host", "cookie", "accept-encoding", "connection")}

        def on_request(req):
            if "/playback-data" in req.url and req.method == "GET" and self.pb_headers is None:
                self.pb_headers = clean(req)                 # exactly what the website sends for playback
            if "/clips/search" in req.url and req.method == "POST" and self.headers is None:
                h = clean(req)
                try:
                    body = json.loads(req.post_data or "{}")
                except Exception:
                    body = {}
                if body.get("match_all", {}).get("player_id") == PLAYER_ID:
                    self.headers, self.search_body = h, body

        self.page.on("request", on_request)
        self.page.goto(PLAYER_URL, wait_until="domcontentloaded")
        for _ in range(30):
            if self.headers:
                break
            self.page.wait_for_timeout(1000)
        if not self.headers:
            self.page.remove_listener("request", on_request)
            sys.exit("The player page didn't load its clips. Check that you can see the player's clips on GameChanger.")
        self.page.remove_listener("request", on_request)
        self._clean = clean
        self.pb_data = {}

        def on_response(resp):
            m = re.search(r"/clips/([0-9a-f-]{36})/playback-data", resp.url)
            if m and resp.status == 200:
                try:
                    self.pb_data[m.group(1)] = resp.json()
                except Exception:
                    pass
                if self.pb_headers is None:
                    try:
                        self.pb_headers = clean(resp.request)
                    except Exception:
                        pass

        self.page.on("response", on_response)
        self.capture_playback_request()

    def capture_playback_request(self):
        """Play a clip the way the discovery run did, so the website makes one real playback request."""
        page = self.page
        for _ in range(10):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(500)
        for sel in ["button[aria-label*='play' i]", "[class*='Thumbnail' i]", "[class*='VideoCard' i]",
                    "[class*='clip' i] img", "video"]:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                try:
                    loc.nth(i).click(timeout=3000)
                except Exception:
                    continue
                for _ in range(12):
                    if self.pb_headers:
                        break
                    page.wait_for_timeout(500)
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                if self.pb_headers:
                    break
            if self.pb_headers:
                break
        # diagnostics either way: header names/format (never the login value) and what a clip card looks like
        try:
            card = page.evaluate("""() => { const v = [...document.querySelectorAll('img,video,[style*="url("]')]
                .slice(0, 6).map(e => e.outerHTML.slice(0, 400)); return v; }""")
        except Exception:
            card = []
        hdr = {k: (v if k.lower() in ("accept", "content-type", "origin", "referer") else "(set)")
               for k, v in (self.pb_headers or {}).items()}
        gc.save_json(WORK / "playback_debug.json", {"captured": bool(self.pb_headers), "headers": hdr,
                                                     "page_url": page.url, "elements": card})
        if not self.pb_headers:
            page.screenshot(path=str(WORK / "clip_page_problem.png"), full_page=False)

    def all_clips(self):
        clips, page_no, seen = [], 0, set()
        while True:
            sel = dict(self.search_body.get("select") or {}, include_totals=True)
            body = dict(self.search_body, select=sel, offset=page_no, limit=36)
            r = self.ctx.request.post(f"{API}/clips/search/v2", data=json.dumps(body),
                                      headers=dict(self.headers, **{"content-type": "application/json"}))
            data = r.json()
            hits = [x for x in data.get("hits", []) if x.get("clip_metadata_id") not in seen]
            seen.update(x.get("clip_metadata_id") for x in hits)
            clips += hits
            nxt = data.get("next_offset")
            total = data.get("total_count")
            # keep going until GameChanger says there are no more pages
            if not hits or nxt is None or nxt == page_no or (total is not None and len(clips) >= total):
                break
            page_no = nxt
        return clips

    def playback(self, clip):
        """Video address + access pass for one clip, using the website's own request settings."""
        if not self.pb_headers:
            return None
        clip_id = clip["clip_metadata_id"]
        if clip_id in self.pb_data:
            return self.pb_data[clip_id]
        url = f"{API}/clips/{clip_id}/playback-data"
        for attempt in range(3):
            try:
                r = self.ctx.request.get(url, headers=self.pb_headers)
                data = r.json()
                if isinstance(data, dict) and data.get("url"):
                    return data
                last = f"HTTP {r.status}: {r.text()[:200]}"
            except Exception as e:
                last = str(e)[:200]
            self.page.wait_for_timeout(1000 * (attempt + 1))
        if not self.reported:
            log(f"   no video address for clip {clip_id} ({last})")
            self.reported = True
        return None

    CARD_JS = r"""(names) => {
      // clip cards: an element with a line like "1 · Offense" / "3 · Defense" plus a picture or video
      const re = /^\s*[▲▼]?\s*\d+\s*·\s*(Offense|Defense)\s*$/i;
      const all = [...document.querySelectorAll('body *')].filter(e => re.test(e.innerText || ''));
      let labels = all.filter(e => ![...e.querySelectorAll('*')].some(d => re.test(d.innerText || '')));
      if (!labels.length) {   // older cards show only the play name ("Out", "Strikeout")
        const set = new Set(names.map(n => n.toLowerCase()));
        labels = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 &&
                   set.has((e.innerText || '').trim().toLowerCase()));
      }
      const out = [];
      labels.forEach(lbl => {
        let card = lbl;
        for (let k = 0; k < 8 && card; k++) {
          card = card.parentElement;
          if (card && card.querySelector('img, video, [style*="url("]')) break;
        }
        if (!card) return;
        const media = card.querySelector('img, video, [style*="url("]');
        const key = (media && (media.currentSrc || media.src || media.getAttribute('style'))) || '';
        const text = (card.innerText || '').trim().replace(/\s+/g, ' ');
        if (!card.hasAttribute('data-hl')) card.setAttribute('data-hl', String(window.__hlN = (window.__hlN || 0) + 1));
        out.push({id: card.getAttribute('data-hl'), key: key.slice(0, 300), text: text.slice(0, 200)});
      });
      return out;
    }"""

    card_names = []

    def capture_by_playing(self, want_ids, wanted_label):
        """Scroll the player's clip list, play each card whose label is a wanted play,
        and keep the stream + access pass the website loads for it."""
        page = self.page
        page.goto(PLAYER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        done_keys, idle, clicked = set(), 0, 0
        while idle < 6 and not want_ids <= set(self.pb_data):
            cards = page.evaluate(self.CARD_JS, self.card_names)
            new = [c for c in cards if (c["key"], c["text"]) not in done_keys]
            for c in new:
                done_keys.add((c["key"], c["text"]))
                if not wanted_label(c["text"]):
                    continue
                before = set(self.pb_data)
                card = page.locator(f"[data-hl='{c['id']}']")
                target = card.locator("img, video, [style*='url(']").first
                try:
                    (target if target.count() else card).scroll_into_view_if_needed(timeout=3000)
                    (target if target.count() else card).click(timeout=3000)
                except Exception:
                    continue
                for _ in range(16):
                    if set(self.pb_data) - before:
                        break
                    page.wait_for_timeout(500)
                clicked += 1
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                got = len(want_ids & set(self.pb_data))
                if clicked % 10 == 0:
                    log(f"   played {clicked} clips, captured {got} of {len(want_ids)}")
            idle = idle + 1 if not new else 0
            self.load_more(cards)
        if not want_ids <= set(self.pb_data):
            try:
                buttons = page.evaluate("""() => [...document.querySelectorAll('button, a, [role=button]')]
                    .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(Boolean).slice(0, 120)""")
            except Exception:
                buttons = []
            gc.save_json(WORK / "list_end_debug.json", {"cards_seen": len(done_keys), "buttons": buttons,
                                                        "url": page.url})
            page.screenshot(path=str(WORK / "list_end.png"), full_page=False)
        return {k: v for k, v in self.pb_data.items() if k in want_ids and isinstance(v, dict) and v.get("url")}

    def load_more(self, cards):
        """Bring in the next batch of clip cards: scroll the last card into view, jump to the bottom,
        and press any 'Load more' / 'Show more' button."""
        page = self.page
        if cards:
            try:
                page.locator(f"[data-hl='{cards[-1]['id']}']").scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
        try:
            page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); "
                          "document.querySelectorAll('*').forEach(e => { if (e.scrollHeight > e.clientHeight + 50 "
                          "&& getComputedStyle(e).overflowY.match(/auto|scroll/)) e.scrollTop = e.scrollHeight; }); }")
        except Exception:
            pass
        page.mouse.wheel(0, 2500)
        # GameChanger's "Load More" is a link at the bottom of the list
        more = page.get_by_text(re.compile(r"^\s*(load|show|see|view)\s+more\s*$", re.I))
        try:
            if more.count():
                more.last.scroll_into_view_if_needed(timeout=2000)
                more.last.click(timeout=3000)
                page.wait_for_timeout(2500)              # next batch of cards loads
        except Exception:
            pass
        page.wait_for_timeout(1000)

    def stream_passes(self):
        """{stream: access-pass cookies} from every clip the website has played so far."""
        out = {}
        for data in self.pb_data.values():
            if isinstance(data, dict) and data.get("url") and data.get("cookies"):
                out[stream_of(data["url"])] = data["cookies"]
        return out

    def play_a_clip_from_game(self, event_id):
        """Open a game's Videos page and play a clip there, so the website fetches an access pass."""
        before = len(self.pb_data)
        page = self.page
        page.goto(f"{gc.TEAM_URL}/schedule/{event_id}/videos", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        for sel in ["img[src*='/clips/']", "[class*='Thumbnail' i]", "[class*='VideoCard' i]",
                    "button[aria-label*='play' i]", "[class*='clip' i] img", "video"]:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                try:
                    loc.nth(i).scroll_into_view_if_needed(timeout=2000)
                    loc.nth(i).click(timeout=3000)
                except Exception:
                    continue
                for _ in range(10):
                    if len(self.pb_data) > before:
                        break
                    page.wait_for_timeout(500)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                if len(self.pb_data) > before:
                    return True
        return False

    def close(self):
        gc.close_browser(self.ctx)


def stream_of(url):
    """Camera stream folder of a clip, e.g. https://vod-archive.gc.com/ivs/v1/<acct>/<stream>."""
    return url.split("?")[0].split("/clips/")[0]


def pick(clips):
    chosen = []
    for c in clips:
        if c.get("player_metadata", {}).get("player_id") != PLAYER_ID:
            continue
        if not c.get("permissions", {}).get("can_view", True):
            continue
        side = (c.get("sport_metadata") or {}).get("play_side")
        play = (c.get("play_metadata", {}).get("play_type") or "").lower()
        if not side and (play in OFFENSE_PLAYS or DEFENSE_PLAY_RE.search(play)):
            chosen.append(c)                               # older clips have no offense/defense tag
        elif (side == "offensive" and play in OFFENSE_PLAYS) or (side == "defensive" and DEFENSE_PLAY_RE.search(play)):
            chosen.append(c)
    chosen.sort(key=lambda c: c["timestamp"])
    out = []
    for c in chosen:                                   # the same play can be tagged twice (sometimes seconds apart)
        t = dt.datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00"))
        pt = (c.get("play_metadata") or {}).get("play_type")
        dup = False
        for o in out[-4:]:
            ot = dt.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00"))
            if o["event_id"] == c["event_id"] and (o.get("play_metadata") or {}).get("play_type") == pt \
                    and abs((t - ot).total_seconds()) <= 15:
                dup = True
        if not dup:
            out.append(c)
    return out


def games_by_id():
    return {g["id"]: g for g in gc.load_json(gc.GAMES_JSON, [])}


def caption(c, games):
    g = games.get(c["event_id"], {})
    play = c.get("name") or c["play_metadata"]["play_type"].replace("_", " ").title()
    sm = c.get("sport_metadata", {})
    date = gc.game_date(g) if g else ""
    when = dt.date.fromisoformat(date).strftime("%b %d, %Y").replace(" 0", " ") if re.match(r"\d{4}-\d\d-\d\d$", date) else ""
    title = g.get("title", "")
    opp = g.get("opponent") or (re.split(r" (?:vs|@) ", title, maxsplit=1)[-1] if re.search(r" (?:vs|@) ", title) else "")
    inning = f"{'Top' if sm.get('inning_half') == 'top' else 'Bottom'} {sm.get('inning')}" if sm.get("inning") else ""
    game = f"{when} vs {opp}" if when and opp else (f"vs {opp}" if opp else when)
    line2 = " · ".join(x for x in [game, inning] if x)
    return play, line2


def fill_titles():
    """Titles may use {player} {team} {season} (and {n} for the Top N title); plain titles stay as they are."""
    global VIDEO_TITLE, QUICK_TITLE, TOP_TITLE, REEL, NOTE
    args = dict(player=PLAYER_NAME, team=gc.TEAM_NAME or "", season=gc.SEASON or "")
    VIDEO_TITLE = sh_config.fill_title(VIDEO_TITLE, **args)
    QUICK_TITLE = sh_config.fill_title(QUICK_TITLE, **args)
    TOP_TITLE = sh_config.fill_title(TOP_TITLE, n="{n}", **args)
    REEL = WORK / f"{gc.safe_name(VIDEO_TITLE)}.mp4"
    NOTE = WORK / f"{gc.safe_name(VIDEO_TITLE)}.txt"


def use_quick_mode(top=0):
    """Switch titles and working folders to the short 'Quick Highlight' (or 'Top N') reel."""
    global QUICK, VIDEO_TITLE, WORK, CLIPS_JSON, REEL, NOTE, LABEL_SECONDS, TOP_N
    QUICK = True
    TOP_N = top
    VIDEO_TITLE = TOP_TITLE.format(n=top) if top else QUICK_TITLE
    LABEL_SECONDS = 3
    WORK = ROOT / "highlights" / (re.sub(r"[^\w]+", "_", PLAYER_NAME) + "_quick")
    CLIPS_JSON = WORK / "clips.json"
    REEL = WORK / f"{gc.safe_name(VIDEO_TITLE)}.mp4"
    NOTE = WORK / f"{gc.safe_name(VIDEO_TITLE)}.txt"


# ---- finding the key moment by sound (bat on ball, ball into a glove)
SOUND_SEARCH_BEFORE = 20      # look this many seconds before GameChanger's recorded moment
SOUND_SEARCH_AFTER = 1
SOUND_MIN_CONTRAST = 6.0      # how much a sound must stand out from the background to count
AROUND_SOUND = {              # seconds kept (before, after) the detected sound
    "single": (3, 5), "double": (3, 9), "triple": (3, 13), "home_run": (3, 18), "inside_the_park_home_run": (3, 18),
    "strikeout": (4, 3), "dropped_third_strike": (4, 4), "dropped_third_strike_batter_out": (4, 4),
    "_out": (3, 6), "double_play": (3, 8), "triple_play": (3, 10),
}


def sharp_sounds(path, rate=16000, hop=0.01):
    """[(time, strength)] of sharp, sudden sounds (cracks/pops) in a clip, strength = how much each stands out."""
    import array
    raw = subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(rate), "-f", "s16le", "-"], capture_output=True).stdout
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    if len(a) < rate:
        return []
    step = int(rate * hop)
    env = []                                           # energy of the sample-to-sample change: favors sharp sounds
    prev = a[0]
    for i in range(0, len(a) - step, step):
        e = 0
        for x in a[i:i + step]:
            d = x - prev
            e += d * d
            prev = x
        env.append(e / step)
    out, win = [], int(1.0 / hop)                      # compare each moment with the second around it
    for i in range(1, len(env) - 1):
        if env[i] < env[i - 1] or env[i] < env[i + 1]:
            continue
        lo, hi = max(0, i - win), min(len(env), i + win)
        bg = sorted(env[lo:hi])[(hi - lo) // 2] or 1.0
        c = env[i] / bg
        if c >= SOUND_MIN_CONTRAST:
            out.append((round(i * hop, 2), round(c, 1)))
    return out


MOTION_FPS = 10
END_LIMITS = {               # (min, max) seconds kept after the key moment when ending on "action settles"
    "single": (4, 8), "double": (6, 12), "triple": (9, 16), "home_run": (12, 22), "inside_the_park_home_run": (12, 22),
    "strikeout": (2, 4), "dropped_third_strike": (3, 6), "dropped_third_strike_batter_out": (3, 6),
    "_out": (3, 8), "double_play": (5, 10), "triple_play": (6, 12),
}


def motion_profile(path, fps=MOTION_FPS):
    """[(time, motion)]: how much the picture changes from frame to frame (ffmpeg does the work)."""
    import tempfile
    tmp = Path(tempfile.mkstemp(suffix=".txt")[1])
    try:
        vf = (f"fps={fps},scale=320:-2,format=gray,tblend=all_mode=difference,signalstats,"
              f"metadata=print:key=lavfi.signalstats.YAVG:file={tmp.name}")
        subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf", vf,
                        "-f", "null", "-"], cwd=str(tmp.parent), capture_output=True)
        out, t = [], None
        for line in tmp.read_text(errors="replace").splitlines():
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1))
            m = re.search(r"YAVG=([\d.]+)", line)
            if m and t is not None:
                out.append((round(t, 2), float(m.group(1))))
        return out
    finally:
        tmp.unlink(missing_ok=True)


def _smooth(vals, k=3):
    return [sum(vals[max(0, i - k):i + k + 1]) / len(vals[max(0, i - k):i + k + 1]) for i in range(len(vals))]


def motion_burst(motion, t, before=0.3, after=1.0):
    """How strongly the action picks up around time t, compared with the typical level in the clip."""
    if not motion:
        return 0.0
    base = sorted(m for _, m in motion)[len(motion) // 2] or 0.1
    near = [m for tt, m in motion if t - before <= tt <= t + after]
    return (max(near) / base) if near else 0.0


def action_end(motion, t, play_type):
    """When the action settles after the key moment, within the play type's limits."""
    lo, hi = END_LIMITS.get((play_type or "").lower()) or END_LIMITS["_out"]
    if not motion:
        return t + (lo + hi) / 2
    times = [tt for tt, _ in motion]
    sm = _smooth([m for _, m in motion])
    base = sorted(sm)[len(sm) // 2] or 0.1
    quiet_needed, run = int(1.0 * MOTION_FPS), 0
    for tt, m in zip(times, sm):
        if tt < t + lo:
            continue
        if tt > t + hi:
            return t + hi
        run = run + 1 if m <= base * 1.3 else 0
        if run >= quiet_needed:
            return tt - 1.0 + 0.5                  # a moment after things settle
    return t + hi


def find_key_play(sounds, motion, recorded_at, play_type):
    """(key time, how it was found) using both sound and picture, or None."""
    lo, hi = recorded_at - SOUND_SEARCH_BEFORE, recorded_at + SOUND_SEARCH_AFTER
    cand = [(t, c, motion_burst(motion, t)) for t, c in sounds if lo <= t <= hi]
    cand = [x for x in cand if not motion or x[2] >= 1.3]          # a real play has action on screen
    if cand:
        pt = (play_type or "").lower()
        score = lambda x: x[1] * min(x[2], 5)
        top = max(score(x) for x in cand)
        strong = [x for x in cand if score(x) >= top * 0.5]
        if pt in OFFENSE_PLAYS:
            pick = max(cand, key=score)
        elif "strikeout" in pt or "dropped" in pt:
            pick = strong[-1]
        else:
            pick = strong[0]
        return pick[0], "sound + picture"
    if motion:                                      # no clear sound: biggest burst of action
        times = [t for t, _ in motion]
        sm = _smooth([m for _, m in motion], 2)
        rises = [(times[i], sm[i] - sm[max(0, i - 5)]) for i in range(len(sm)) if lo <= times[i] <= hi]
        if rises:
            t, r = max(rises, key=lambda x: x[1])
            base = sorted(sm)[len(sm) // 2] or 0.1
            if r > base * 0.8:
                return t, "picture"
    return None


def key_moment(sounds, recorded_at, play_type):
    """Pick the sound that is the key play: the loudest crack for a hit, the last pop for a strikeout,
    the first strong crack for an out. Returns (time, strength) or None."""
    lo, hi = recorded_at - SOUND_SEARCH_BEFORE, recorded_at + SOUND_SEARCH_AFTER
    cand = [x for x in sounds if lo <= x[0] <= hi]
    if not cand:
        return None
    pt = (play_type or "").lower()
    top = max(c for _, c in cand)
    if pt in OFFENSE_PLAYS:
        return max(cand, key=lambda x: x[1])
    strong = [x for x in cand if x[1] >= top * 0.5]
    if "strikeout" in pt or "dropped" in pt:
        return strong[-1]
    return strong[0]


def quick_window(play_type):
    pt = (play_type or "").lower()
    before, after = QUICK_WINDOWS.get(pt) or QUICK_WINDOWS["_out"]
    return before + QUICK_START_EARLIER, after - QUICK_END_EARLIER


def play_offset(pb, clip, duration):
    """Seconds into the clip where GameChanger recorded the play, from the stream's own clock
    (#EXT-X-PROGRAM-DATE-TIME). Falls back to 10 s before the end of the clip."""
    import urllib.request
    from urllib.parse import urljoin
    cookies = pb.get("cookies") or {}
    if not cookies and pb.get("cookie_text"):
        for line in pb["cookie_text"].splitlines():
            parts = line.split("\t")
            if len(parts) == 7 and not line.startswith("#"):
                cookies[parts[5]] = parts[6]
    hdr = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
           "Referer": "https://web.gc.com/", "Origin": "https://web.gc.com"}

    def get(u):
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=20) as r:
            return r.read().decode("utf-8", "replace")

    try:
        url = pb["url"]
        text = get(url)
        if "#EXT-X-STREAM-INF" in text:
            lines = text.splitlines()
            variant = next(lines[i + 1].strip() for i, l in enumerate(lines)
                           if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines))
            url = urljoin(url, variant)
            text = get(url)
        m = re.search(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", text)
        if m:
            start = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            played = dt.datetime.fromisoformat(clip["timestamp"].replace("Z", "+00:00"))
            off = (played - start).total_seconds()
            if 3 <= off <= duration + 1:
                return min(off, duration), "stream clock"
    except Exception:
        pass
    return max(duration - 10, 0), "estimate"


def cmd_list(args, session=None):
    from playwright.sync_api import sync_playwright
    WORK.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        s = session or GCSession(p)
        clips = s.all_clips()
        s.close()
    chosen = pick(clips)
    games = games_by_id()
    gc.save_json(CLIPS_JSON, chosen)
    total = sum(c.get("duration") or 60 for c in chosen)
    log(f"{len(clips)} clips tagged to {PLAYER_NAME}; {len(chosen)} picked for the reel (about {total / 60:.0f} min)")
    from collections import Counter
    counts = Counter(c.get("name") or c["play_metadata"]["play_type"] for c in chosen)
    print("   " + ", ".join(f"{n} {k}" for k, n in counts.most_common()))
    for c in chosen:
        play, line2 = caption(c, games)
        print(f"   {c['timestamp'][:10]}  {play:<30} {line2}")
    return chosen


# ---------------------------------------------------------------- video
def cookie_text_from_pass(stream, cookies):
    host = re.match(r"https://([^/]+)", stream).group(1)
    return "# Netscape HTTP Cookie File\n" + "".join(
        "\t".join([host, "FALSE", "/", "TRUE", "0", k, v]) + "\n" for k, v in cookies.items())


def cookie_text_from_jar(jar):
    lines = ["# Netscape HTTP Cookie File"]
    for k in jar:
        d = k["domain"]
        lines.append("\t".join([d, "TRUE" if d.startswith(".") else "FALSE", k.get("path", "/"),
                                 "TRUE" if k.get("secure") else "FALSE",
                                 str(int(k["expires"])) if k.get("expires", -1) > 0 else "0", k["name"], k["value"]]))
    return "\n".join(lines) + "\n"


def cookie_file(pb, path):
    if pb.get("cookie_text"):
        path.write_text(pb["cookie_text"], encoding="utf-8")
        return
    path.write_text(cookie_text_from_pass(pb["url"].split("/clips/")[0], pb.get("cookies") or {}), encoding="utf-8")


def font_arg():
    for f in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(f):
            return "fontfile='" + f.replace("\\", "/").replace(":", "\\:") + "':"
    return ""


def has_audio(path):
    out = subprocess.run([gc.FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         errors="replace").stderr
    return " Audio: " in out


def normalize(src, dst, label_file, start=None, length=None, crop=""):
    """Re-encode one clip to a common format (so all games join cleanly) with a caption at the start."""
    vf_base = ((crop + ",") if crop else "") + (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
    # compact caption in the bottom-left corner, so the play stays visible
    text = (f",drawtext={font_arg()}textfile={label_file.name}:fontsize=26:fontcolor=white:line_spacing=4:"
            f"box=1:boxcolor=black@0.55:boxborderw=8:x=14:y=h-th-12" +
            ("" if CAPTION_ALWAYS else f":enable='lt(t,{LABEL_SECONDS})'"))
    audio_in = [] if has_audio(src) else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    maps = ["-map", "0:v:0", "-map", "0:a:0" if not audio_in else "1:a:0", "-shortest"]
    for vf in (vf_base + text, vf_base):              # caption needs the font; without it, plain video
        trim = (["-ss", f"{start:.2f}"] if start else []) + (["-t", f"{length:.2f}"] if length else [])
        cmd = [gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *trim[:2], "-i", src.name, *trim[2:], *audio_in,
               "-vf", vf, *maps, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k", "-movflags", "+faststart", dst.name]
        r = subprocess.run(cmd, cwd=str(src.parent), capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            return True
    log(f"   couldn't convert {src.name}: {r.stderr.strip()[-300:]}")
    return False


def is_blank(path, start=None, length=None):
    """True when the clip is (almost) all black or one flat colour - e.g. the camera feed was down."""
    trim = (["-ss", f"{start:.2f}"] if start else []) + (["-t", f"{length:.2f}"] if length else [])
    r = subprocess.run([gc.FFMPEG, "-hide_banner", *trim[:2], "-i", str(path), *trim[2:], "-an", "-vf",
                        "blackdetect=d=0.3:pix_th=0.12,fps=2,signalstats,metadata=print:key=lavfi.signalstats.YDIF",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    total = length or clip_seconds(path)
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", r.stderr))
    if total and black >= 0.6 * total:
        return True
    ydif = [float(x) for x in re.findall(r"YDIF=([\d.]+)", r.stderr)]
    return bool(ydif) and max(ydif) < 0.5                # nothing changes at all: a frozen/blank picture


def build(chosen):
    from playwright.sync_api import sync_playwright
    gc.check_tools()
    # each caption style gets its own clips folder, so switching back and forth never mixes styles
    tag = ("_always" if CAPTION_ALWAYS else "") if not QUICK else "_" + __import__("hashlib").md5(
        json.dumps([QUICK_WINDOWS, QUICK_START_EARLIER, QUICK_END_EARLIER, smart_cut.END_LIMITS, smart_cut.REGIONS,
                    SMART_ZOOM, bool(AI_CHECK and smart_cut.ai_key(ROOT)), "v9"] + (["caption-always"] if CAPTION_ALWAYS else []),
                   sort_keys=True).encode()).hexdigest()[:6]
    clips_dir = WORK / f"clips{tag}"
    clips_dir.mkdir(parents=True, exist_ok=True)
    games = games_by_id()

    # 1) video addresses: each clip's video sits next to its thumbnail (.jpg -> .m3u8). Access passes cover a
    #    whole camera stream, so we only need one pass per stream, got by playing one clip from that stream.
    todo = [(i, c) for i, c in enumerate(chosen, 1) if not (clips_dir / f"{i:03d}.mp4").exists()]
    if todo:
        want = {c["clip_metadata_id"] for _, c in todo}
        kinds = {((c.get("name") or "").strip().lower(), ((c.get("sport_metadata") or {}).get("play_side") or "")[:3])
                 for _, c in todo}

        def wanted_label(text):
            t = text.lower()
            side = "off" if "offense" in t else "def" if "defense" in t else ""
            return any(n and n in t and (sd == side or not sd) for n, sd in kinds)

        log(f"Playing {len(todo)} clip(s) on {PLAYER_NAME}'s page to capture their streams...")
        with sync_playwright() as p:
            s = GCSession(p)
            s.card_names = sorted({(c.get("name") or "").strip() for c in chosen if c.get("name")} |
                                  {"Walk", "Stole Base", "Passed Ball", "Hit by Pitch", "Error", "Sacrifice Fly",
                                   "Strikeout", "Out", "Single", "Double", "Triple", "Home Run"})
            got_pb = s.capture_by_playing(want, wanted_label)
            if QUICK:
                fetch_play_by_play(s.page, {c.get("event_id") for _, c in todo})
            s.close()
        for i, c in todo:
            if c["clip_metadata_id"] in got_pb:
                c["_pb"] = got_pb[c["clip_metadata_id"]]
        got = sum(1 for _, c in todo if c.get("_pb"))
        log(f"   captured {got} of {len(todo)} clip streams")
        if not got:
            sys.exit(f"No clip streams captured. Diagnostics saved in {WORK}.")

    # 2) download + convert, several at a time
    timing = {}
    used = set()
    pbps = {i: match_play(c, used) for i, c in enumerate(chosen, 1)} if QUICK else {}

    def work(item):
        i, c = item
        out = clips_dir / f"{i:03d}.mp4"
        if out.exists():
            return True
        if (clips_dir / f"{i:03d}.blank").exists():
            return False
        if not c.get("_pb"):
            return False
        raw = clips_dir / f"{i:03d}.raw.mp4"
        ck = clips_dir / f"{i:03d}.cookies.txt"
        lbl = clips_dir / f"{i:03d}.label.txt"
        try:
            cookie_file(c["_pb"], ck)
            subprocess.run([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part",
                            "--ffmpeg-location", gc.FFMPEG, "--cookies", str(ck),
                            "--add-header", "Referer: https://web.gc.com/", "--add-header", "Origin: https://web.gc.com",
                            "--remux-video", "mp4", "-o", str(raw), c["_pb"]["url"]], check=True)
            play, line2 = caption(c, games)
            pbp = pbps.get(i)
            line3 = f"\n{pbp['text'][:70]}" if pbp and pbp.get("text") else ""
            with open(lbl, "w", encoding="utf-8", newline="\n") as fh:   # Windows line endings showed as blank lines
                fh.write(f"{PLAYER_NAME} - {play}\n{line2}{line3}")
            start = length = None
            crop = ""
            if QUICK:
                dur = clip_seconds(raw)
                off, how = play_offset(c["_pb"], c, dur)
                pt = c["play_metadata"].get("play_type")
                cut = smart_cut.analyze(raw, gc.FFMPEG, off, pt, dur, pbp)
                excitement = None
                if cut:
                    how += f" + {cut['how']} (key play at {cut['key']}s)"
                    if AI_CHECK:
                        what = (c.get("name") or pt) + (f" - {pbp['text']} (pitches: {', '.join(pbp['pitches'])})"
                                                         if pbp else "")
                        ai = smart_cut.ai_refine(raw, gc.FFMPEG, what, cut, ROOT,
                                                 clips_dir / f"{i:03d}.frames.png", font_arg())
                        (clips_dir / f"{i:03d}.frames.png").unlink(missing_ok=True)
                        if ai:
                            cut.update(start=ai["start"], end=ai["end"], key=ai["key"])
                            how += f" + vision check ({ai['note']})"
                    start, length = cut["start"], min(dur, cut["end"]) - cut["start"]
                    excitement = cut["excitement"]
                    if SMART_ZOOM and SMART_ZOOM > 1:
                        crop = smart_cut.zoom_filter(cut["center"], SMART_ZOOM)
                else:
                    before, after = quick_window(pt)
                    start = max(0.0, off - before)
                    length = min(dur, off + after) - start
                    excitement = smart_cut.PLAY_WEIGHT.get((pt or "").lower(), 3) * 2 + (3 if pbp and pbp["scored"] else 0)
                timing[i] = {"play": c.get("name"), "clip_len": round(dur, 1), "play_at": round(off, 1),
                             "from": how, "kept": [round(start, 1), round(start + length, 1)],
                             "excitement": excitement, "details": cut, "play_by_play": pbp}
            if is_blank(raw, start, length):
                (clips_dir / f"{i:03d}.blank").write_text("black/blank video", encoding="utf-8")
                log(f"   clip {i} ({c.get('name')}) is a blank/black video - left out")
                return False
            return normalize(raw, out, lbl, start, length, crop)
        except Exception as e:
            log(f"   clip {i} failed: {e}")
            return False
        finally:
            for f in (raw, ck, lbl):
                f.unlink(missing_ok=True)

    log(f"Downloading and preparing {len(chosen)} clips ({PARALLEL} at a time)...")
    done = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for ok in pool.map(work, list(enumerate(chosen, 1))):
            done += 1
            if done % 10 == 0 or done == len(chosen):
                log(f"   {done}/{len(chosen)} clips ready")

    tfile = clips_dir / "quick_timing.json"
    old = gc.load_json(tfile, {}) if QUICK else {}
    if QUICK and timing:
        old.update({str(k): v for k, v in timing.items()})
        gc.save_json(tfile, old)
        shutil.copy(tfile, WORK / "quick_timing.json")
        est = sum(1 for v in timing.values() if v["from"].startswith("estimate"))
        if est:
            log(f"   {est} clip(s) had no stream clock; their play time was estimated")
    parts = [(i, c, clips_dir / f"{i:03d}.mp4") for i, c in enumerate(chosen, 1)]
    parts = [x for x in parts if x[2].exists()]
    if TOP_N and len(parts) > TOP_N:                     # most exciting plays only, still in game order
        ranked = sorted(parts, key=lambda x: -((old.get(str(x[0])) or {}).get("excitement") or 0))
        keep = {x[0] for x in ranked[:TOP_N]}
        log(f"   keeping the {TOP_N} most exciting of {len(parts)} plays")
        chosen = [c for i, c in enumerate(chosen, 1) if i in keep]
        parts = [x for x in parts if x[0] in keep]
    if not parts:
        sys.exit("No clips could be prepared.")
    blank = sum(1 for i, _ in enumerate(chosen, 1) if (clips_dir / f"{i:03d}.blank").exists())
    if blank:
        log(f"   {blank} blank/black clip(s) left out")
    missing = len(chosen) - len(parts) - blank
    if missing:
        log(f"   {missing} clip(s) couldn't be downloaded and are left out")
    if len(parts) < MIN_READY * (len(chosen) - blank):
        sys.exit(f"Only {len(parts)} of {len(chosen)} clips are ready, so the reel was NOT built or uploaded. "
                 f"Run again to retry the missing clips (finished clips are kept).")

    # 3) join (all clips share one format now, so this is lossless)
    lst = WORK / "reel.concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for _, _, p in parts), encoding="utf-8")
    log(f"Joining {len(parts)} clips into {REEL.name}...")
    subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(REEL)], check=True)
    lst.unlink(missing_ok=True)

    # 4) chapters for the description (YouTube turns "0:00 ..." lines into chapters)
    chapters, t = [], 0.0
    for _, c, p in parts:
        play, line2 = caption(c, games)
        m, s_ = divmod(int(t), 60)
        h, m = divmod(m, 60)
        stamp = f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"
        chapters.append(f"{stamp} {play} - {line2}")
        t += clip_seconds(p)
    gc.save_json(WORK / "chapters.json", chapters)
    log(f"Reel ready: {REEL} ({REEL.stat().st_size / 1e9:.2f} GB, {t / 60:.0f} min)")


def clip_seconds(path):
    out = subprocess.run([gc.FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 60.0


def description():
    chapters = gc.load_json(WORK / "chapters.json", [])
    head = (f"{VIDEO_TITLE}\n" + (f"The {TOP_N} most exciting plays of the season.\n" if TOP_N else
                                   "Just the key play from each clip.\n" if QUICK else "") +
            "Batting: singles, doubles, triples and home runs. Defense: strikeouts, outs and fielder's choices.\n"
            "Clips from GameChanger, in game order.\n\n")
    out = head
    for line in chapters:
        if len((out + line + "\n").encode("utf-8")) > 4900:
            break
        out += line + "\n"
    return out.strip()


def cmd_upload(args):
    if NOTE.exists() and not getattr(args, "again", False):
        sys.exit(f"Already uploaded: {NOTE.read_text(encoding='utf-8').strip()}")
    if not REEL.exists():
        sys.exit("No reel yet. Run:  py highlights.py build")
    yt = gc.youtube()
    log(f"Uploading {REEL.name} as {gc.PRIVACY}...")
    vid = gc.upload_with_quota_wait(yt, REEL, VIDEO_TITLE, description())
    log(f"   uploaded: https://youtu.be/{vid}")
    NOTE.write_text(f"{VIDEO_TITLE}\nUploaded {dt.datetime.now():%Y-%m-%d %H:%M} as {gc.PRIVACY}: "
                    f"https://youtu.be/{vid}\n", encoding="utf-8")
    if ADD_TO_TEAM_PLAYLIST:
        state = gc.load_json(gc.STATE_JSON, {})
        key = f"highlights-{PLAYER_ID}" + (f"-top{TOP_N}" if TOP_N else "-quick" if QUICK else "")
        state[key] = {"title": VIDEO_TITLE, "date": "9999-12-31", "videos": [vid]}   # sorts after all games
        gc.save_json(gc.STATE_JSON, state)
        gc.add_to_playlist(yt, state, key)
        log(f"   playlist: {gc.playlist_title()}")
    if gc.DELETE_AFTER_UPLOAD:
        REEL.unlink(missing_ok=True)
        for d in WORK.glob("clips*"):
            shutil.rmtree(d, ignore_errors=True)
        log("Removed the local reel and clip files (the .txt note has the link).")


def cmd_sample(args):
    """Download a few untrimmed clips (2-3 per kind of play) for review. No trimming, no upload."""
    from playwright.sync_api import sync_playwright
    gc.check_tools()
    chosen = cmd_list(args)
    groups = {"hit": [], "strikeout": [], "out": []}
    for c in chosen:
        pt = (c.get("play_metadata") or {}).get("play_type", "")
        k = "hit" if pt in OFFENSE_PLAYS else "strikeout" if "strikeout" in pt or "dropped" in pt else "out"
        groups[k].append(c)
    picks = groups["hit"][:3] + groups["strikeout"][:3] + groups["out"][:2]
    out_dir = ROOT / "highlights" / "samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    want = {c["clip_metadata_id"] for c in picks}
    kinds = {((c.get("name") or "").strip().lower(), ((c.get("sport_metadata") or {}).get("play_side") or "")[:3])
             for c in picks}

    def wanted_label(text):
        t = text.lower()
        side = "off" if "offense" in t else "def" if "defense" in t else ""
        return any(n and n in t and (sd == side or not sd) for n, sd in kinds)

    log(f"Capturing {len(picks)} sample clips...")
    with sync_playwright() as p:
        s = GCSession(p)
        s.card_names = sorted({(c.get("name") or "").strip() for c in picks})
        got = s.capture_by_playing(want, wanted_label)
        fetch_play_by_play(s.page, {c["event_id"] for c in picks})
        s.close()
    meta = []
    games = games_by_id()
    used = set()
    for n, c in enumerate(picks, 1):
        pbp = match_play(c, used)
        pb = got.get(c["clip_metadata_id"])
        if not pb:
            log(f"   sample {n}: not captured")
            continue
        raw = out_dir / f"sample{n:02d}_{c['play_metadata']['play_type']}.mp4"
        ck = out_dir / "cookies.txt"
        cookie_file(pb, ck)
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part", "--force-overwrites",
                            "--ffmpeg-location", gc.FFMPEG, "--cookies", str(ck),
                            "--add-header", "Referer: https://web.gc.com/", "--add-header", "Origin: https://web.gc.com",
                            "--remux-video", "mp4", "-o", str(raw), pb["url"]], check=True)
        except Exception as e:
            log(f"   sample {n}: download failed ({e})")
            continue
        finally:
            ck.unlink(missing_ok=True)
        dur = clip_seconds(raw)
        off, how = play_offset(pb, c, dur)
        play, line2 = caption(c, games)
        cut = smart_cut.analyze(raw, gc.FFMPEG, off, c["play_metadata"]["play_type"], dur, pbp)
        sheet = raw.with_suffix(".png")              # frames every second around the play, for review
        t0 = max(0, off - 22)
        label = "%{eif\\:t+" + str(int(t0)) + "\\:d}s"          # seconds into the clip
        tile = (f"fps=1,scale=480:-2,drawtext={font_arg()}text='{label}':"
                f"fontsize=40:fontcolor=yellow:box=1:boxcolor=black@0.6:x=8:y=8,tile=5x5")
        r = subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t0), "-t", "25",
                            "-i", str(raw), "-vf", tile, "-frames:v", "1", str(sheet)], capture_output=True, text=True)
        if r.returncode:
            subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t0), "-t", "25",
                            "-i", str(raw), "-vf", "fps=1,scale=480:-2,tile=5x5", "-frames:v", "1", str(sheet)])
        meta.append({"file": raw.name, "play": play, "type": c["play_metadata"]["play_type"], "game": line2,
                     "clip_len": round(dur, 2), "recorded_at": round(off, 2), "from": how,
                     "smart_cut": cut, "play_by_play": pbp, "sheet_starts_at": int(t0), "timestamp": c.get("timestamp"),
                     "event_id": c.get("event_id"), "clip_id": c.get("clip_metadata_id")})
        log(f"   sample {n}: {play} ({line2}) - recorded at {off:.1f}s of {dur:.0f}s; " +
            (f"key play {cut['key']}s, keep {cut['start']}-{cut['end']}s, excitement {cut['excitement']} "
             f"({cut['how']})" if cut else "no key play found"))
    gc.save_json(out_dir / "samples.json", meta)
    log(f"Saved {len(meta)} sample clips in {out_dir}")


PLAYS_DIR = ROOT / "highlights" / "plays"
PITCH_RE = re.compile(r"^(ball \d|strike \d|foul|in play)", re.I)


def fetch_play_by_play(page, event_ids):
    """Layer 1: the pitch-by-pitch play list of each game (what the game's Plays page loads). Cached."""
    PLAYS_DIR.mkdir(parents=True, exist_ok=True)
    for eid in sorted(e for e in event_ids if e and not (PLAYS_DIR / f"{e}.json").exists()):
        got = {}

        def on_response(resp):
            if resp.url.split("?")[0].endswith(f"/game-stream-processing/{eid}/plays") and resp.status == 200:
                try:
                    got["plays"] = resp.json()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(f"{gc.TEAM_URL}/schedule/{eid}/plays", wait_until="domcontentloaded")
            for _ in range(20):
                if got:
                    break
                page.wait_for_timeout(500)
        except Exception as e:
            log(f"   play-by-play for game {eid}: {e}")
        page.remove_listener("response", on_response)
        if got:
            gc.save_json(PLAYS_DIR / f"{eid}.json", got["plays"])
    n = sum(1 for e in event_ids if (PLAYS_DIR / f"{e}.json").exists())
    log(f"   play-by-play available for {n} of {len(event_ids)} games")


def match_play(c, used):
    """The play-by-play entry for this clip: same game half-inning, involves the player, same kind of play."""
    data = gc.load_json(PLAYS_DIR / f"{c.get('event_id')}.json", None)
    sm = c.get("sport_metadata") or {}
    if not data or not sm.get("inning"):
        return None
    names = {p["id"]: p.get("first_name") or "" for team in (data.get("team_players") or {}).values() for p in team}
    want = (c.get("name") or "").lower()
    cands = [pl for pl in data.get("plays", []) if pl.get("inning") == sm.get("inning")
             and pl.get("half") == sm.get("inning_half") and PLAYER_ID in json.dumps(pl)
             and (c.get("event_id"), pl.get("order")) not in used]
    if not cands:
        return None

    def score(pl):
        n = (pl.get("name_template") or {}).get("template", "").lower()
        return 2 if n == want else 1 if (want and (want in n or n in want or ("out" in want and "out" in n))) else 0

    best = max(cands, key=score)
    used.add((c.get("event_id"), best.get("order")))
    text = ((best.get("final_details") or [{}])[0]).get("template", "")
    text = re.sub(r"\$\{([^}]+)\}", lambda m: names.get(m.group(1), "") or "", text).strip()
    pitches = [d.get("template", "") for d in best.get("at_plate_details") or [] if PITCH_RE.match(d.get("template", ""))]
    return {"text": text, "pitches": pitches, "scored": bool(best.get("did_score_change"))}


def cmd_build(args):
    chosen = cmd_list(args)
    if not chosen:
        sys.exit("No matching clips.")
    build(chosen)


def cmd_run(args):
    if NOTE.exists() and not args.again:
        sys.exit(f"Already uploaded: {NOTE.read_text(encoding='utf-8').strip()}")
    cmd_build(args)
    cmd_upload(args)                                  # uploads straight away, Unlisted


_LOCK = None


def wait_for_other_runs():
    """Only one highlights run at a time: runs share the GameChanger browser, and a second run closing it
    leaves the first one stuck. A second run waits here until the first one finishes."""
    global _LOCK
    path = ROOT.parent / "highlights_running.lock"
    _LOCK = open(path, "a+")
    told = False
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                _LOCK.seek(0)
                msvcrt.locking(_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return                                    # released automatically when this run ends
        except OSError:
            if not told:
                log("Another highlights run is using the GameChanger browser - waiting for it to finish...")
                told = True
            import time
            time.sleep(30)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "build", "upload", "run", "sample"):
        sp = sub.add_parser(name)
        sp.add_argument("--quick", action="store_true", help="short reel: just the key play from each clip")
        sp.add_argument("--top", type=int, default=0, help="with --quick: only the N most exciting plays")
        sp.add_argument("--caption", choices=["always", "brief"],
                        help="caption on for the whole clip, or just the first few seconds")
        if name in ("upload", "run"):
            sp.add_argument("--again", action="store_true",
                            help="make and upload a new reel even if one was uploaded before")
    args = ap.parse_args()
    global CAPTION_ALWAYS
    if args.cmd != "list":
        wait_for_other_runs()
    if getattr(args, "caption", None):
        CAPTION_ALWAYS = args.caption == "always"
    if not gc.GAMES_JSON.exists() or not gc.TEAM_NAME:
        log("First run for this team: reading the schedule from GameChanger...")
        gc.cmd_scan(args)
    fill_titles()
    if getattr(args, "quick", False) or getattr(args, "top", 0):
        use_quick_mode(getattr(args, "top", 0))
    {"list": cmd_list, "build": cmd_build, "upload": cmd_upload, "run": cmd_run, "sample": cmd_sample}[args.cmd](args)


if __name__ == "__main__":
    main()
