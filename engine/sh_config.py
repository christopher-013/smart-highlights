"""
Optional settings file for the engine.

On your own PC you can keep editing the settings blocks at the top of gc_to_youtube.py and highlights.py.
The hosted service (and anyone who prefers a file) instead points SMART_HIGHLIGHTS_CONFIG at a JSON file:

{
  "player_url": "https://web.gc.com/teams/<TEAM_ID>/<slug>/players/<PLAYER_ID>",
  "player_name": "Jane Doe",
  "season": "Fall 2024",
  "sport": "softball",
  "youtube_channel": "https://www.youtube.com/@mychannel",
  "privacy": "unlisted",                      # unlisted | private | public
  "caption": "always",                        # always | brief
  "titles": {"full": "Highlights - {player} - {team} {season}",
             "smart": "Smart Highlights: {player} - {team} {season}",
             "top": "Smart Highlights: Top {n} - {player} - {team} {season}"},
  "add_to_team_playlist": false,
  "headless": false
}

SMART_HIGHLIGHTS_WORKDIR (optional) is the folder for downloads, logins and notes; default: next to the scripts.
"""
import json
import os
import re
from pathlib import Path

PLAYER_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?web\.gc\.com/teams/([A-Za-z0-9]+)(?:/([^/?#]+?))?/players/([0-9a-fA-F-]{36})")
CHANNEL_RE = re.compile(r"youtube\.com/@([A-Za-z0-9._-]+)", re.I)


def parse_player_url(url):
    """'https://web.gc.com/teams/AbC123/2024-fall-team/players/<uuid>' -> team id, slug, player id."""
    m = PLAYER_URL_RE.search((url or "").strip())
    if not m:
        raise ValueError("That doesn't look like a GameChanger player link "
                         "(https://web.gc.com/teams/.../players/...).")
    return {"team_id": m.group(1), "team_slug": m.group(2) or "", "player_id": m.group(3).lower()}


def parse_channel(url):
    """'https://www.youtube.com/@MyChannel' -> 'mychannel' ('' when no @handle is given)."""
    m = CHANNEL_RE.search(url or "")
    if m:
        return m.group(1).lower()
    u = (url or "").strip()
    return u.lstrip("@").lower() if u.startswith("@") else ""


def load():
    path = os.environ.get("SMART_HIGHLIGHTS_CONFIG")
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def workdir(default):
    w = os.environ.get("SMART_HIGHLIGHTS_WORKDIR")
    if not w:
        return default
    p = Path(w).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG = load()


def headless():
    return bool(CONFIG.get("headless")) or os.environ.get("SMART_HIGHLIGHTS_HEADLESS") == "1"


def team_settings():
    """Overrides for gc_to_youtube.py's team settings."""
    c = CONFIG
    if not c:
        return {}
    out = {}
    if c.get("player_url"):
        ids = parse_player_url(c["player_url"])
        out.update(TEAM_ID=ids["team_id"], TEAM_SLUG=ids["team_slug"])
    if "season" in c:
        out["SEASON"] = c["season"]
    if "youtube_channel" in c:
        out["EXPECTED_HANDLE"] = parse_channel(c["youtube_channel"])
        out["CHANNEL_NAME"] = "@" + out["EXPECTED_HANDLE"] if out["EXPECTED_HANDLE"] else "your"
    if c.get("sport"):
        out["TAGS"] = ["{team}", c["sport"], "highlights"]
    if c.get("privacy") in ("unlisted", "private", "public"):
        out["PRIVACY"] = c["privacy"]
        out["PLAYLIST_PRIVACY"] = c["privacy"]
    out["SHARED_BROWSER_PROFILE"] = bool(c.get("shared_browser_profile", False))
    if "delete_after_upload" in c:
        out["DELETE_AFTER_UPLOAD"] = bool(c["delete_after_upload"])
    return out


DEFAULT_TITLES = {"full": "Highlights - {player} - {team} {season}",
                  "smart": "Smart Highlights: {player} - {team} {season}",
                  "top": "Smart Highlights: Top {n} - {player} - {team} {season}"}


def player_settings():
    """Overrides for highlights.py's player settings."""
    c = CONFIG
    if not c:
        return {}
    out = {}
    if c.get("player_url"):
        out["PLAYER_ID"] = parse_player_url(c["player_url"])["player_id"]
    if c.get("player_name"):
        out["PLAYER_NAME"] = c["player_name"]
    t = {**DEFAULT_TITLES, **{k: v for k, v in (c.get("titles") or {}).items() if v}}
    if t.get("full"):
        out["VIDEO_TITLE"] = t["full"]
    if t.get("smart"):
        out["QUICK_TITLE"] = t["smart"]
    if t.get("top"):
        out["TOP_TITLE"] = t["top"]
    if "add_to_team_playlist" in c:
        out["ADD_TO_TEAM_PLAYLIST"] = bool(c["add_to_team_playlist"])
    if c.get("caption") in ("always", "brief"):
        out["CAPTION_ALWAYS"] = c["caption"] == "always"
    return out


def apply(namespace, overrides):
    for k, v in overrides.items():
        namespace[k] = v


def fill_title(template, player, team, season, n=10):
    """Titles may use {player} {team} {season} {n}; plain titles are left as they are."""
    if "{" not in template:
        return template
    return re.sub(r"\s+", " ", template.format(player=player, team=team, season=season, n=n)).strip()
