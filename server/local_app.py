"""
Smart Highlights on your own computer: the page in docs/ plus the few actions behind it.

  Open Smart Highlights.bat     starts this and opens the page (double-click it)

  POST /api/open        open GameChanger in its own Chrome window; you sign in and go to your player's page
  GET  /api/status      is Chrome open, which player is on screen, how the reel is coming along
  POST /api/reels       make the Smart Highlights reel for that player, kept on this computer
  POST /api/reel/open   play the finished reel
  POST /api/reel/show   show the finished reel in its folder

You sign in to GameChanger yourself in that Chrome window, and this program never sees the password. It only
answers on this computer, and only to its own page: another website can't start a reel from your browser.

Settings (environment variables):
  SMART_HIGHLIGHTS_HOME   where team folders and the Chrome sign-in are kept (default: "Smart Highlights" in your home folder)
  SMART_HIGHLIGHTS_PORT   the page's port (default 8765)
  ENGINE_CMD              override the engine command (tests)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine"
sys.path.insert(0, str(ENGINE))
import sh_config  # noqa: E402

HOME = Path(os.environ.get("SMART_HIGHLIGHTS_HOME") or Path.home() / "Smart Highlights").resolve()
PORT = int(os.environ.get("SMART_HIGHLIGHTS_PORT", "8765"))
CDP = "http://127.0.0.1:9222"
GC_START = "https://web.gc.com/teams"
LAST_PLAYER = "last_player.json"
SEASON_SLUG_RE = re.compile(r"^(\d{4})-(spring|summer|fall|winter)", re.I)
STAGES = [(r"reading the schedule", "Reading the team schedule"),
          (r"clips tagged to|Playing \d+ clip", "Finding the player's clips"),
          (r"Downloading and preparing", "Downloading and cutting clips"),
          (r"Joining \d+ clips", "Joining the reel")]

app = FastAPI(title="Smart Highlights")
_lock = threading.Lock()
_read_lock = threading.Lock()
_seen = None                # the tabs open when GameChanger was opened: {tab id: address}. Older tabs don't count.
_opened_tab = None
_players = {}               # address -> what its page said
_job = None


@app.middleware("http")
async def only_this_computer(request: Request, call_next):
    """Refuse other websites, including ones pretending to be localhost."""
    host = request.headers.get("host", "").split(":")[0]
    origin = urllib.parse.urlparse(request.headers.get("origin", "")).hostname
    allowed = ("localhost", "127.0.0.1")
    if host not in allowed or (origin and origin not in allowed):
        return JSONResponse({"detail": "This page only works from your own computer."}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- reading the player's page
def parse_player_page(text, url):
    """{'name', 'team', 'season', 'clips'} from a GameChanger player page; '' for anything that isn't showing."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    name = team = season = ""
    for n, ln in enumerate(lines):
        if re.fullmatch(r"\d+-\d+(-\d+)?", ln) and n and not team:      # the team's record sits between name and season
            team = lines[n - 1]
            season = lines[n + 1] if n + 1 < len(lines) and re.search(r"\b\d{4}\b", lines[n + 1]) else ""
        numbered = re.fullmatch(r"(.+), #\d+", ln)
        if numbered and not name:
            name = numbered.group(1)
        elif ln.startswith("Bats:") and n and not name:
            name = lines[n - 1]
    if not season:
        m = SEASON_SLUG_RE.match(sh_config.parse_player_url(url)["team_slug"])
        season = f"{m.group(2).title()} {m.group(1)}" if m else ""
    clips = re.search(r"^([\d,]+) clips?$", text or "", re.M)
    return {"name": name, "team": team, "season": season, "clips": int(clips.group(1).replace(",", "")) if clips else None}


def read_page_text(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP, timeout=10000)
        try:
            page = next((t for c in browser.contexts for t in c.pages if t.url == url), None)
            return page.evaluate("document.body.innerText") if page else ""
        finally:
            browser.close()


def open_tabs():
    """Chrome's open tabs, or None when that Chrome isn't running."""
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=2) as r:
            return [t for t in json.load(r) if t.get("type") == "page"]
    except (OSError, ValueError):
        return None


def tab_on_player(tabs):
    """The player tab reached since GameChanger was opened: the tab we opened first, then the most recent."""
    if _seen is None:
        return None
    fresh = [t for t in tabs if sh_config.PLAYER_URL_RE.search(t.get("url", "")) and _seen.get(t.get("id")) != t["url"]]
    return next((t for t in fresh if t.get("id") == _opened_tab), fresh[0] if fresh else None)


def current_player(tabs):
    tab = tab_on_player(tabs) if tabs else None
    if not tab:
        return None
    url = tab["url"]
    if url in _players:
        return _players[url]
    if not _read_lock.acquire(blocking=False):
        return {"url": url, "reading": True}
    try:
        text = read_page_text(url)
    except Exception:
        return {"url": url, "reading": True}
    finally:
        _read_lock.release()
    found = dict(parse_player_page(text, url), url=url, signed_out=sh_config.looks_signed_out(text))
    if found["signed_out"]:
        return found
    if not found["name"]:                       # the page is still loading
        found["reading"] = bool(text.count("\n") < 20)
        return found
    _players[url] = found
    return found


# ---------------------------------------------------------------- opening GameChanger
def start_address():
    """The last player's page when there is one, so a repeat visit needs no clicking around."""
    try:
        return json.loads((HOME / LAST_PLAYER).read_text(encoding="utf-8"))["url"]
    except (OSError, ValueError, KeyError):
        return GC_START


def start_chrome(url):
    chrome = sh_config.find_chrome()
    if not chrome:
        raise HTTPException(500, "Couldn't find Google Chrome. Please install it and try again.")
    HOME.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome, "--remote-debugging-port=9222", f"--user-data-dir={HOME / 'browser_profile'}",
                      "--no-first-run", "--no-default-browser-check", url])
    for _ in range(40):
        if open_tabs() is not None:
            return
        time.sleep(0.5)
    raise HTTPException(500, "Chrome started but didn't answer. Close its window and try again.")


def open_new_tab(url):
    req = urllib.request.Request(f"{CDP}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        tab = json.load(r)
    urllib.request.urlopen(f"{CDP}/json/activate/{tab['id']}", timeout=5).close()
    return tab["id"]


@app.post("/api/open")
def open_gamechanger():
    global _seen, _opened_tab
    if job_running():
        raise HTTPException(409, "Your reel is being made in that window. Please wait for it to finish.")
    url = start_address()
    tabs = open_tabs()
    if tabs is None:
        _seen, _opened_tab = {}, None
        start_chrome(url)
    else:
        _seen = {t.get("id"): t.get("url") for t in tabs}
        _opened_tab = open_new_tab(url)
    return {"ok": True}


# ---------------------------------------------------------------- making the reel
def job_running():
    return bool(_job) and _job["status"] == "running"


def folder_for(player):
    ids = sh_config.parse_player_url(player["url"])
    team = player.get("team") or ids["team_slug"] or ids["team_id"]
    if player["season"] and player["season"].lower() not in team.lower():
        team = f"{team} {player['season']}"
    return HOME / (re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", team).strip(" .") or ids["team_id"])


def engine_cmd(player, folder, limit):
    custom = os.environ.get("ENGINE_CMD")
    start = custom.split() if custom else [sys.executable, "-u", str(ENGINE / "make_reel.py")]
    args = ["--folder", str(folder), "--player-url", player["url"], "--player-name", player["name"],
            "--season", player["season"] or time.strftime("%Y"), "--reel", "smart", "--local", "--no-questions"]
    return start + args + (["--limit", str(limit)] if limit else [])


def read_line(job, line):
    with _lock:
        job["log"] = (job["log"] + [line])[-300:]
        job["stage"] = next((s for pat, s in STAGES if re.search(pat, line)), job["stage"])
        ready = re.search(r"(\d+)/(\d+) clips ready", line)
        if ready:
            job["done"], job["total"] = int(ready.group(1)), int(ready.group(2))
        reel = re.search(r"Reel ready: (.+) \(", line)      # up to the last bracket: titles can have their own
        if reel:
            job["reel"] = reel.group(1)
            job["length"] = (re.search(r"(\d+ min)\)", line) or [None, ""])[1]
        unsure = re.search(r"(\d+) clip\(s\) were cut with less certainty", line)
        if unsure:
            job["unsure"] = int(unsure.group(1))


def make_reel(job, cmd, folder):
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(cmd, cwd=str(folder), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            if line.strip():
                read_line(job, line.rstrip())
        proc.wait()
        worked = proc.returncode == 0 and job.get("reel")
        reason = "" if worked else (job["log"][-1] if job["log"] else "It stopped without saying why.")
    except OSError as e:
        worked, reason = False, str(e)
    with _lock:
        job.update(status="done" if worked else "failed", stage="Done" if worked else reason)


class ReelRequest(BaseModel):
    player_name: str = Field(default="", max_length=80)     # only when the page didn't show a name
    limit: int = Field(default=0, ge=0, le=500)             # a quick trial: only the first N plays


@app.post("/api/reels")
def start_reel(req: ReelRequest):
    global _job
    if job_running():
        raise HTTPException(409, "A reel is already being made.")
    player = current_player(open_tabs())
    if not player or player.get("reading"):
        raise HTTPException(400, "Open your player's page in the GameChanger window first.")
    if player.get("signed_out"):
        raise HTTPException(400, "Sign in to GameChanger in that window first.")
    player = dict(player, name=req.player_name.strip() or player.get("name", ""))
    if not player["name"]:
        raise HTTPException(400, "Please enter the player's name.")
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / LAST_PLAYER).write_text(json.dumps({"url": player["url"]}), encoding="utf-8")
    folder = folder_for(player)
    with _lock:
        _job = {"status": "running", "stage": "Starting", "player": player["name"], "team": player.get("team", ""),
                "season": player["season"], "done": 0, "total": 0, "log": [], "reel": "", "unsure": 0, "length": ""}
    threading.Thread(target=make_reel, args=(_job, engine_cmd(player, folder, req.limit), folder), daemon=True).start()
    return {"ok": True}


def finished_reel():
    reel = Path(_job["reel"]) if _job and _job.get("reel") else None
    if not reel or not reel.exists():
        raise HTTPException(404, "The reel isn't there any more.")
    return reel


@app.post("/api/reel/open")
def open_reel():
    reel = finished_reel()
    if os.name == "nt":
        os.startfile(reel)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(reel)])
    return {"ok": True}


@app.post("/api/reel/show")
def show_reel():
    reel = finished_reel()
    if os.name == "nt":
        subprocess.Popen(["explorer", f"/select,{reel}"])
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(reel.parent)])
    return {"ok": True}


@app.get("/api/status")
def status():
    with _lock:
        job = dict(_job) if _job else None
    if job and job["status"] == "running":      # the engine is using Chrome: leave it alone
        return {"chrome": True, "opened": True, "player": None, "job": job}
    tabs = open_tabs()
    return {"chrome": tabs is not None, "opened": _seen is not None, "player": current_player(tabs), "job": job}


app.mount("/", StaticFiles(directory=HERE.parent / "docs", html=True), name="page")


def already_running(url):
    try:
        with urllib.request.urlopen(f"{url}/api/status", timeout=3):
            return True
    except OSError:
        return False


def main():
    url = f"http://localhost:{PORT}"
    if already_running(url):
        webbrowser.open(url)
        return
    import uvicorn
    print(f"Smart Highlights is running at {url}\nKeep this window open while you use it. Close it when you're done.")
    threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
