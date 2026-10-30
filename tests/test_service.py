import os, sys, time, json
from pathlib import Path
import tempfile
D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D, ENGINE_CMD=f"{sys.executable} -u {Path(__file__).parent / 'fake_engine.py'}",
                  GOOGLE_CLIENT_SECRET="should-not-leak")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from server import app as svc

def test_end_to_end():
    with TestClient(svc.app) as c:
        assert c.get("/api/health").json()["ok"]
        body = {"player_url": "https://web.gc.com/teams/AbC123xyz/2024-fall-x/players/abcdef12-3456-4abc-8def-1234567890ab",
                "player_name": "Jane Doe", "season": "Fall 2024", "reels": ["top", "smart"], "caption": "always",
                "yt_session": "nope", "gc_email": "a@b.c", "gc_password": "pw"}
        assert c.post("/api/jobs", json=body).status_code == 400            # YouTube not connected
        svc._yt_sessions["S" * 24] = {"creds": {"token": "t"}, "handle": "mychannel", "title": "My", "created": time.time()}
        body["yt_session"] = "S" * 24
        bad = dict(body, player_url="https://example.com/x")
        assert c.post("/api/jobs", json=bad).status_code == 400
        r = c.post("/api/jobs", json=body); assert r.status_code == 200, r.text
        jid = r.json()["id"]
        for _ in range(100):
            j = c.get(f"/api/jobs/{jid}").json()
            if j["needs_code"]:
                break
            time.sleep(0.1)
        assert j["needs_code"], j
        assert c.post(f"/api/jobs/{jid}/code", json={"code": "123456"}).json()["ok"]
        for _ in range(100):
            j = c.get(f"/api/jobs/{jid}").json()
            if j["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert j["status"] == "done", j
        assert [x["reel"] for x in j["results"]] == ["smart", "top"], j["results"]
        rec = Path(D, "jobs", f"{jid}.json").read_text()
        assert "pw" not in json.loads(rec)["settings"].values() and "a@b.c" not in rec
        assert not Path(D, "work", jid).exists()                            # everything deleted
        assert "settings" not in j
        print("\n".join(j["log"]))

if __name__ == "__main__":
    test_end_to_end(); print("OK")
