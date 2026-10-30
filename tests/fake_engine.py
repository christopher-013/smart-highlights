"""Stands in for engine/highlights.py in tests: behaves like a run without touching GameChanger or YouTube."""
import json, os, sys, time
from pathlib import Path
work = Path(os.environ["SMART_HIGHLIGHTS_WORKDIR"])
cfg = json.loads(Path(os.environ["SMART_HIGHLIGHTS_CONFIG"]).read_text())
assert os.environ.get("GC_EMAIL") and os.environ.get("GC_PASSWORD"), "login not passed"
assert (work / "youtube_token.json").exists(), "youtube token not written"
assert not any(k.startswith("GOOGLE_") for k in os.environ), "server secrets leaked to engine"
print("First run for this team: reading the schedule from GameChanger...", flush=True)
if "--quick" in sys.argv and not (work / "signed_in").exists():
    (work / "gc_code_needed").write_text("x")
    for _ in range(100):
        if (work / "gc_code.txt").exists():
            break
        time.sleep(0.1)
    assert (work / "gc_code.txt").read_text() == "123456"
    (work / "gc_code_needed").unlink()
    (work / "signed_in").write_text("x")
print("Signed in to GameChanger", flush=True)
print("Playing 12 clip(s) on the player page", flush=True)
print("Downloading and preparing 12 clips (4 at a time)...", flush=True)
print("Joining 12 clips into reel.mp4...", flush=True)
print(f"   uploaded: https://youtu.be/TEST{'top' if '--top' in sys.argv else 'smart'}01 caption={sys.argv[-1]} player={cfg['player_name']}", flush=True)
