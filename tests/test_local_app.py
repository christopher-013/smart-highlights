import json
import os
import sys
import tempfile
from pathlib import Path

D = tempfile.mkdtemp()
os.environ["SMART_HIGHLIGHTS_HOME"] = D
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from server import local_app as app

URL = "https://web.gc.com/teams/AbC123/2024-fall-example-12u/players/abcdef12-3456-4abc-8def-1234567890ab"
PAGE = """Home
Support
Example Team 12U
3-1
Fall 2024
Somewhere, CA
HOME
SCHEDULE
JD
Jane Doe, #13
Bats: Right
Video Clips
297 clips
vs. Somebody · Sep 22, 2024
"""


def client():
    return TestClient(app.app, base_url="http://127.0.0.1:8765")


def player_open(monkeypatch, text=PAGE):
    monkeypatch.setattr(app, "open_tabs", lambda: [{"id": "t", "url": URL, "type": "page"}])
    monkeypatch.setattr(app, "read_page_text", lambda url: text)
    monkeypatch.setattr(app, "_seen", {})
    monkeypatch.setattr(app, "_opened_tab", "t")
    monkeypatch.setattr(app, "_players", {})
    monkeypatch.setattr(app, "_job", None)


def wait_for_job(c):
    for _ in range(200):
        j = c.get("/api/status").json()["job"]
        if j["status"] != "running":
            return j
        app.time.sleep(0.05)
    raise AssertionError("job never finished")


def test_reads_the_player_page():
    assert app.parse_player_page(PAGE, URL) == {"name": "Jane Doe", "team": "Example Team 12U", "season": "Fall 2024", "clips": 297}
    # no jersey number, record or clip count showing: the season comes from the address
    got = app.parse_player_page("JD\nJane Doe\nBats: Right\n", URL)
    assert got == {"name": "Jane Doe", "team": "", "season": "Fall 2024", "clips": None}
    assert app.parse_player_page("", URL)["name"] == ""


def test_only_tabs_reached_after_opening_count(monkeypatch):
    other = URL.replace("abcdef12", "bbbbbbbb")
    tabs = [{"id": "old", "url": URL}, {"id": "new", "url": "https://web.gc.com/teams"}]
    monkeypatch.setattr(app, "_seen", {"old": URL, "new": "https://web.gc.com/teams"})
    monkeypatch.setattr(app, "_opened_tab", "new")
    assert app.tab_on_player(tabs) is None
    tabs[1]["url"] = other
    assert app.tab_on_player(tabs)["id"] == "new"
    monkeypatch.setattr(app, "_seen", None)
    assert app.tab_on_player(tabs) is None                       # GameChanger not opened from the app yet


def test_refuses_other_websites():
    with client() as c:
        assert c.get("/api/status", headers={"origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/open", headers={"origin": "https://evil.example"}).status_code == 403
    with TestClient(app.app, base_url="http://evil.example") as c:      # a name pointed at this computer
        assert c.get("/api/status").status_code == 403


def test_page_is_served_and_asks_for_no_sign_in():
    with client() as c:
        html = c.get("/").text
    assert 'id="open"' in html and 'id="make"' in html
    assert 'type="password"' not in html and "gc_email" not in html


def test_makes_a_reel_from_the_open_player(monkeypatch, tmp_path):
    reel = tmp_path / "Reel (first 4 plays).mp4"
    reel.write_bytes(b"x")
    fake = tmp_path / "fake.py"
    fake.write_text(f"""import sys
print("args", " ".join(sys.argv[1:]))
print("[10:00:00] 5 clips tagged to Jane Doe; 2 picked for the reel")
print("[10:00:01]    2/2 clips ready")
print("[10:00:01] Joining 2 clips into Reel.mp4...")
print("[10:00:02]    1 clip(s) were cut with less certainty - worth a look: x")
print(r"[10:00:03] Reel ready: {reel} (0.27 GB, 17 min)")
""", encoding="utf-8")
    monkeypatch.setenv("ENGINE_CMD", f"{sys.executable} -u {fake}")
    player_open(monkeypatch)
    with client() as c:
        s = c.get("/api/status").json()
        assert s["player"]["name"] == "Jane Doe" and s["job"] is None
        assert c.post("/api/reels", json={}).status_code == 200
        j = wait_for_job(c)
        assert j["status"] == "done", j
        assert app.finished_reel() == reel
        assert j["reel"] == str(reel) and j["unsure"] == 1 and j["length"] == "17 min" and (j["done"], j["total"]) == (2, 2)
        args = j["log"][0]
        assert "--reel smart --local --no-questions" in args and "--upload" not in args
        assert Path(D, "Example Team 12U Fall 2024").is_dir()
        assert json.loads(Path(D, app.LAST_PLAYER).read_text(encoding="utf-8"))["url"] == URL
        assert app.start_address() == URL


def test_failed_reel_says_why(monkeypatch, tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text('import sys\nprint("Still cannot see any games on the schedule page.")\nsys.exit(1)\n', encoding="utf-8")
    monkeypatch.setenv("ENGINE_CMD", f"{sys.executable} -u {fake}")
    player_open(monkeypatch)
    with client() as c:
        assert c.post("/api/reels", json={}).status_code == 200
        j = wait_for_job(c)
        assert j["status"] == "failed" and "Still cannot see" in j["stage"]


def test_signed_out_page_cannot_start_a_reel(monkeypatch):
    player_open(monkeypatch, "Pricing Support Sign In Join Us\n" + PAGE)
    with client() as c:
        assert c.get("/api/status").json()["player"]["signed_out"]
        r = c.post("/api/reels", json={})
        assert r.status_code == 400 and "Sign in" in r.json()["detail"]
