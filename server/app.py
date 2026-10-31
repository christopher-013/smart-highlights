"""
Smart Highlights - hosted service.

The public website (GitHub Pages, docs/) talks to this API:

  GET  /auth/google/start?return=<site url>   send the visitor to Google to connect their YouTube channel
  GET  /auth/google/callback                    Google sends them back here; we return to the site
  POST /api/jobs                                start a highlight reel (settings + GameChanger sign-in)
  GET  /api/jobs/<id>                           progress, log, YouTube links when done
  POST /api/jobs/<id>/code                      the sign-in code GameChanger emailed (if it asks for one)
  GET  /api/health

Jobs run one at a time (each one drives a real Chrome and a lot of video work). GameChanger passwords and
YouTube sign-ins are held in memory only for the job, handed to the engine through its environment, and
everything the job downloaded is deleted when it finishes.

Settings (environment variables):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   Google Cloud OAuth client of type "Web application"
  PUBLIC_API_URL                           e.g. https://api.example.com  (Google redirect = <this>/auth/google/callback)
  SITE_ORIGINS                             comma-separated sites allowed to call the API (default: GitHub Pages site)
  DATA_DIR                                 working storage (default ./data)
  MAX_QUEUE                                waiting jobs allowed (default 5)
  ENGINE_CMD                               override the engine command (tests)
"""
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine"
sys.path.insert(0, str(ENGINE))
import sh_config  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", HERE / "data")).resolve()
JOBS_DIR = DATA / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
SITE_ORIGINS = [o.strip() for o in os.environ.get(
    "SITE_ORIGINS", "https://christopher-013.github.io,http://localhost:8080").split(",") if o.strip()]
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "5"))
JOB_HOURS = float(os.environ.get("JOB_HOURS", "4"))
KEEP_DAYS = 7
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
]

app = FastAPI(title="Smart Highlights")
app.add_middleware(CORSMiddleware, allow_origins=SITE_ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"])

# ---------------------------------------------------------------- in-memory secrets (never written to disk)
_lock = threading.Lock()
_oauth_states = {}        # state -> (return url, created)
_yt_sessions = {}         # session id -> {"creds": {...}, "handle": str, "created": t}
_job_secrets = {}         # job id -> {"gc_email", "gc_password", "yt_creds"}
_queue = queue.Queue()
SESSION_TTL = 3 * 3600


def _expire():
    now = time.time()
    with _lock:
        for d, ttl in ((_oauth_states, 900), (_yt_sessions, SESSION_TTL)):
            for k in [k for k, v in d.items() if now - (v[1] if isinstance(v, tuple) else v["created"]) > ttl]:
                d.pop(k, None)


# ---------------------------------------------------------------- job records (no secrets inside)
def _job_path(job_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", job_id or ""):
        raise HTTPException(404, "No such job")
    return JOBS_DIR / f"{job_id}.json"


def load_job(job_id):
    p = _job_path(job_id)
    if not p.exists():
        raise HTTPException(404, "No such job")
    return json.loads(p.read_text(encoding="utf-8"))


def save_job(job):
    p = _job_path(job["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(p)
            return
        except PermissionError:                     # Windows: someone is reading the progress file right now
            if attempt == 19:
                raise
            time.sleep(0.1)


def update_job(job_id, **changes):
    with _lock:
        job = load_job(job_id)
        log_add = changes.pop("log_add", None)
        job.update(changes)
        if log_add:
            job["log"] = (job.get("log", []) + [log_add])[-300:]
        job["updated"] = time.time()
        save_job(job)
        return job


# ---------------------------------------------------------------- Google / YouTube connection
def _google_flow(state=None):
    from google_auth_oauthlib.flow import Flow
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise HTTPException(503, "YouTube connection isn't set up on this server yet.")
    cfg = {"web": {"client_id": cid, "client_secret": secret,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": [f"{PUBLIC_API_URL}/auth/google/callback"]}}
    return Flow.from_client_config(cfg, scopes=SCOPES, state=state,
                                   redirect_uri=f"{PUBLIC_API_URL}/auth/google/callback")


def _allowed_return(url):
    u = urlparse(url or "")
    origin = f"{u.scheme}://{u.netloc}"
    return url if origin in SITE_ORIGINS else SITE_ORIGINS[0] + "/"


@app.get("/auth/google/start")
def google_start(request: Request, ret: str = ""):
    _expire()
    ret = _allowed_return(ret or request.query_params.get("return", ""))
    flow = _google_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                        include_granted_scopes="true")
    with _lock:
        _oauth_states[state] = (ret, time.time())
    return RedirectResponse(url)


@app.get("/auth/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    with _lock:
        ret, _ = _oauth_states.pop(state, (SITE_ORIGINS[0] + "/", 0))
    if error or not code:
        return RedirectResponse(f"{ret}#yt_error={error or 'cancelled'}")
    flow = _google_flow(state)
    flow.fetch_token(code=code)
    creds = flow.credentials
    from googleapiclient.discovery import build
    items = build("youtube", "v3", credentials=creds).channels().list(part="snippet", mine=True) \
        .execute().get("items", [])
    if not items:
        return RedirectResponse(f"{ret}#yt_error=no_channel")
    sn = items[0]["snippet"]
    handle = (sn.get("customUrl") or "").lstrip("@").lower()
    sid = secrets.token_urlsafe(24)
    with _lock:
        _yt_sessions[sid] = {"creds": json.loads(creds.to_json()), "handle": handle,
                             "title": sn.get("title", ""), "created": time.time()}
    return RedirectResponse(f"{ret}#" + urlencode({"yt": sid, "channel": handle, "channel_title": sn.get("title", "")}))


# ---------------------------------------------------------------- jobs
class JobRequest(BaseModel):
    player_url: str
    player_name: str = Field(min_length=1, max_length=80)
    season: str = Field(default="", max_length=40)
    sport: str = Field(default="softball", pattern="^(softball|baseball)$")
    reels: list = Field(default_factory=lambda: ["smart"])          # any of: smart, top, full
    top_n: int = Field(default=10, ge=3, le=30)
    caption: str = Field(default="always", pattern="^(always|brief)$")
    privacy: str = Field(default="unlisted", pattern="^(unlisted|private|public)$")
    titles: dict = Field(default_factory=dict)
    yt_session: str
    gc_email: str = Field(min_length=3, max_length=200)
    gc_password: str = Field(min_length=1, max_length=200)


class CodeRequest(BaseModel):
    code: str = Field(min_length=3, max_length=12)


@app.get("/api/health")
def health():
    return {"ok": True, "queued": _queue.qsize(),
            "youtube_ready": bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    _expire()
    try:
        ids = sh_config.parse_player_url(req.player_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    reels = [r for r in req.reels if r in ("smart", "top", "full")]
    if not reels:
        raise HTTPException(400, "Pick at least one reel.")
    with _lock:
        yt = _yt_sessions.get(req.yt_session)
    if not yt:
        raise HTTPException(400, "Connect your YouTube channel first (or again - the connection expired).")
    if _queue.qsize() >= MAX_QUEUE:
        raise HTTPException(429, "The queue is full right now - please try again later.")
    job_id = secrets.token_urlsafe(18)
    titles = {k: str(v)[:100] for k, v in (req.titles or {}).items() if k in ("full", "smart", "top") and v}
    job = {"id": job_id, "created": time.time(), "updated": time.time(), "status": "queued",
           "stage": "Waiting for its turn", "needs_code": False, "log": [], "results": [],
           "player_name": req.player_name, "team_id": ids["team_id"], "channel": yt["handle"],
           "settings": {"player_url": req.player_url.strip(), "player_name": req.player_name.strip(),
                        "season": req.season.strip(), "sport": req.sport, "youtube_channel": "@" + yt["handle"],
                        "privacy": req.privacy, "caption": req.caption, "titles": titles,
                        "add_to_team_playlist": False, "headless": True,
                        "delete_after_upload": False},              # the whole job folder is removed at the end
           "reels": [r for r in ("full", "smart", "top") if r in reels],   # Top N reuses the Smart clips
           "top_n": req.top_n}
    save_job(job)
    with _lock:
        _job_secrets[job_id] = {"gc_email": req.gc_email, "gc_password": req.gc_password, "yt_creds": yt["creds"]}
    _queue.put(job_id)
    return {"id": job_id, "position": _queue.qsize()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = load_job(job_id)
    job.pop("settings", None)
    if job["status"] == "queued":
        job["position"] = list(_queue.queue).index(job_id) + 1 if job_id in list(_queue.queue) else 1
    return job


@app.post("/api/jobs/{job_id}/code")
def send_code(job_id: str, body: CodeRequest):
    job = load_job(job_id)
    if not job.get("needs_code"):
        raise HTTPException(409, "This job isn't waiting for a code.")
    if not re.fullmatch(r"[A-Za-z0-9]{3,12}", body.code.strip()):
        raise HTTPException(400, "That code doesn't look right.")
    (DATA / "work" / job_id / "gc_code.txt").write_text(body.code.strip(), encoding="utf-8")
    update_job(job_id, needs_code=False, stage="Signing in to GameChanger", log_add="Code received")
    return {"ok": True}


# ---------------------------------------------------------------- worker
REEL_ARGS = {"full": ["run", "--again"], "smart": ["run", "--quick", "--again"]}
STAGES = [(r"reading the schedule", "Reading the team schedule"),
          (r"Signed in to GameChanger", "Signed in to GameChanger"),
          (r"Playing \d+ clip", "Finding the player's clips"),
          (r"Downloading and preparing", "Downloading and cutting clips"),
          (r"Joining \d+ clips", "Joining the reel"),
          (r"Uploading ", "Uploading to YouTube")]
LINK_RE = re.compile(r"https://youtu\.be/[A-Za-z0-9_-]{6,}")


def engine_cmd():
    custom = os.environ.get("ENGINE_CMD")
    return custom.split() if custom else [sys.executable, "-u", str(ENGINE / "highlights.py")]


def run_reel(job, reel, work, env):
    args = list(REEL_ARGS.get(reel) or ["run", "--quick", "--again", "--top", str(job["top_n"])])
    args += ["--caption", job["settings"]["caption"]]
    proc = subprocess.Popen(engine_cmd() + args, cwd=str(work), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    deadline = time.time() + JOB_HOURS * 3600
    links = []

    def watch_code():
        while proc.poll() is None:
            if (work / "gc_code_needed").exists() and not load_job(job["id"]).get("needs_code"):
                update_job(job["id"], needs_code=True, status="running",
                           stage="Enter the sign-in code GameChanger emailed you")
            time.sleep(2)

    threading.Thread(target=watch_code, daemon=True).start()
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        stage = next((s for pat, s in STAGES if re.search(pat, line)), None)
        links += LINK_RE.findall(line)
        update_job(job["id"], log_add=line[:300], **({"stage": stage} if stage else {}))
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("took too long and was stopped")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("stopped with an error (see the log)")
    return links


def stop_browser(work):
    """The engine leaves its Chrome running between steps; close the one that belongs to this job."""
    if os.name != "nt":
        subprocess.run(["pkill", "-f", f"--user-data-dir={work}"], capture_output=True)


def process(job_id):
    with _lock:
        sec = _job_secrets.pop(job_id, None)
    job = load_job(job_id)
    if not sec:
        update_job(job_id, status="failed", stage="The server restarted before this job ran - please start again")
        return
    work = DATA / "work" / job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        (work / "config.json").write_text(json.dumps(job["settings"]), encoding="utf-8")
        (work / "youtube_token.json").write_text(json.dumps(sec["yt_creds"]), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "GC_"))}
        env.update(SMART_HIGHLIGHTS_CONFIG=str(work / "config.json"), SMART_HIGHLIGHTS_WORKDIR=str(work),
                   SMART_HIGHLIGHTS_HEADLESS="1", PYTHONUNBUFFERED="1",
                   GC_EMAIL=sec["gc_email"], GC_PASSWORD=sec["gc_password"])
        update_job(job_id, status="running", stage="Starting")
        results = []
        for reel in job["reels"]:
            update_job(job_id, log_add=f"=== {reel} reel ===")
            for link in run_reel(job, reel, work, env):
                results.append({"reel": reel, "url": link})
                update_job(job_id, results=results)
        final = {"status": "done", "stage": "Done"}
    except Exception as e:
        final = {"status": "failed", "stage": f"Failed: {e}"}
    finally:
        sec.clear()
        stop_browser(work)
        shutil.rmtree(work, ignore_errors=True)         # videos, browser profile and YouTube sign-in
    update_job(job_id, needs_code=False, **final)


def cleanup_old():
    for p in JOBS_DIR.glob("*.json"):
        if time.time() - p.stat().st_mtime > KEEP_DAYS * 86400:
            p.unlink(missing_ok=True)


def worker():
    while True:
        job_id = _queue.get()
        try:
            process(job_id)
        except Exception as e:                      # one job's failure must never stop the jobs behind it
            print(f"Job {job_id} ended with an error: {e}", flush=True)
        finally:
            cleanup_old()
            _queue.task_done()


@app.on_event("startup")
def start_worker():
    # jobs that were waiting or running when the server stopped can't continue (their passwords are gone)
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("status") in ("queued", "running"):
            job.update(status="failed", stage="The server restarted - please start again", needs_code=False)
            save_job(job)
    shutil.rmtree(DATA / "work", ignore_errors=True)
    threading.Thread(target=worker, daemon=True).start()
