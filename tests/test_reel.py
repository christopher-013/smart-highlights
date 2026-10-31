"""The whole reel path with GameChanger and YouTube faked: real ffmpeg, real files, nothing leaves this computer."""
import json, shutil, subprocess, sys, types
from argparse import Namespace
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import gc_to_youtube as gc
import highlights as h
import make_reel


def clip(n, play="single", name="Single"):
    return {"clip_metadata_id": f"clip-{n}", "event_id": "game-1", "name": name, "duration": 8,
            "timestamp": f"2025-01-26T03:0{n}:00.000Z", "play_metadata": {"play_type": play},
            "player_metadata": {"player_id": "p1"}, "permissions": {"can_view": True},
            "sport_metadata": {"inning": n, "inning_half": "top", "play_side": "offensive"}}


class FakeSession:
    def __init__(self, p):
        self.page = None

    def capture_by_playing(self, want, wanted_label):
        return {w: {"url": "https://example.invalid/clip.m3u8", "cookies": {"a": "b"}} for w in want}

    def close(self):
        pass


class FakeRequest:
    def next_chunk(self):
        return None, {"id": "VIDEO123"}


class FakeYouTube:
    def __init__(self):
        self.bodies = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.bodies.append(body)
        return FakeRequest()


@pytest.fixture
def reel(tmp_path, monkeypatch):
    """highlights.py pointed at a temporary folder, with the network parts replaced."""
    gc.check_tools()
    source = tmp_path / "source.mp4"
    subprocess.run([gc.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=8",
                    "-f", "lavfi", "-i", "sine=f=440:r=16000:d=8", "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", "-shortest", str(source)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "games.json").write_text(json.dumps(
        [{"id": "game-1", "date": "2025-01-25", "opponent": "Firecrackers 12U 🧨🥎", "title": "vs Firecrackers"}]), encoding="utf-8")
    monkeypatch.setattr(gc, "GAMES_JSON", tmp_path / "games.json")
    monkeypatch.setattr(gc, "STATE_JSON", tmp_path / "uploaded.json")
    monkeypatch.setattr(gc, "DELETE_AFTER_UPLOAD", True)
    monkeypatch.setattr(h, "ROOT", tmp_path)
    monkeypatch.setattr(h, "WORK", work)
    monkeypatch.setattr(h, "CLIPS_JSON", work / "clips.json")
    monkeypatch.setattr(h, "VIDEO_TITLE", "Test reel")
    monkeypatch.setattr(h, "REEL", work / "Test reel.mp4")
    monkeypatch.setattr(h, "NOTE", work / "Test reel.txt")
    monkeypatch.setattr(h, "PLAYS_DIR", tmp_path / "plays")
    monkeypatch.setattr(h, "PLAYER_NAME", "Jane Doe")
    monkeypatch.setattr(h, "PLAYER_ID", "p1")
    monkeypatch.setattr(h, "QUICK", False)
    monkeypatch.setattr(h, "LIMIT", 0)
    monkeypatch.setattr(h, "TOP_N", 0)
    monkeypatch.setattr(h, "AI_CHECK", False)
    monkeypatch.setattr(h, "GCSession", FakeSession)
    monkeypatch.setattr(h, "ADD_TO_TEAM_PLAYLIST", False)
    monkeypatch.setattr(h, "download_clip", lambda pb, ck, dest: shutil.copy(source, dest))
    pw = types.ModuleType("playwright.sync_api")

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    pw.sync_playwright = lambda: Ctx()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", pw)
    monkeypatch.setattr(h, "cmd_list", lambda args, session=None: [clip(1), clip(2), clip(3)])
    return Namespace(work=work, tmp=tmp_path)


def args(**kw):
    return Namespace(**{"again": False, "local": True, **kw})


def seconds(path):
    return h.clip_seconds(path)


def test_local_run_keeps_the_reel_and_uploads_nothing(reel, monkeypatch):
    monkeypatch.setattr(gc, "youtube", lambda **k: pytest.fail("a local run must not contact YouTube"))
    h.cmd_run(args())
    assert h.REEL.exists() and 20 < seconds(h.REEL) < 28
    assert not h.NOTE.exists()
    chapters = json.loads((reel.work / "chapters.json").read_text(encoding="utf-8"))
    assert len(chapters) == 3 and chapters[0].startswith("0:00 Single - Jan 25, 2025 vs Firecrackers")


def test_upload_keeps_or_removes_files_as_asked(reel, monkeypatch):
    yt = FakeYouTube()
    monkeypatch.setattr(gc, "youtube", lambda **k: yt)
    monkeypatch.setattr(gc, "DELETE_AFTER_UPLOAD", False)                   # what --keep does
    h.cmd_run(args(local=False))
    assert yt.bodies[0]["status"]["privacyStatus"] == "unlisted" and yt.bodies[0]["snippet"]["title"] == "Test reel"
    assert "youtu.be/VIDEO123" in h.NOTE.read_text(encoding="utf-8")
    assert h.REEL.exists() and list(reel.work.glob("clips*"))
    monkeypatch.setattr(gc, "DELETE_AFTER_UPLOAD", True)
    h.cmd_upload(args(local=False, again=True))
    assert not h.REEL.exists() and not list(reel.work.glob("clips*"))


def test_keep_flag_turns_off_deleting(reel, monkeypatch):
    monkeypatch.setattr(h, "wait_for_other_runs", lambda: None)
    monkeypatch.setattr(h, "fill_titles", lambda: None)
    monkeypatch.setattr(gc, "TEAM_ID", "team")
    monkeypatch.setattr(gc, "TEAM_NAME", "Team")
    monkeypatch.setattr(h, "cmd_run", lambda a: None)
    monkeypatch.setattr(sys, "argv", ["highlights.py", "run", "--keep"])
    h.main()
    assert gc.DELETE_AFTER_UPLOAD is False


def test_no_player_set_gives_a_plain_message(monkeypatch):
    monkeypatch.setattr(h, "PLAYER_ID", "YOUR_PLAYER_ID")
    monkeypatch.setattr(sys, "argv", ["highlights.py", "list"])
    with pytest.raises(SystemExit, match="No player is set"):
        h.main()


def test_second_upload_is_refused(reel, monkeypatch):
    monkeypatch.setattr(gc, "youtube", lambda **k: FakeYouTube())
    monkeypatch.setattr(gc, "DELETE_AFTER_UPLOAD", False)
    h.cmd_run(args(local=False))
    with pytest.raises(SystemExit, match="Already uploaded"):
        h.cmd_run(args(local=False))


def test_cached_clips_follow_the_clip_not_its_position(reel):
    h.build([clip(1), clip(2)])
    before = {p.name: p.stat().st_mtime for p in next(reel.work.glob("clips*")).glob("*.mp4")}
    assert set(before) == {"clip-1.mp4", "clip-2.mp4"}
    h.build([clip(9), clip(1), clip(2)])                                    # a new clip at the front of the list
    after = {p.name: p.stat().st_mtime for p in next(reel.work.glob("clips*")).glob("*.mp4")}
    assert set(after) == {"clip-9.mp4", "clip-1.mp4", "clip-2.mp4"}
    assert after["clip-1.mp4"] == before["clip-1.mp4"]


def test_top_n_keeps_the_most_exciting(reel, monkeypatch):
    monkeypatch.setattr(h, "QUICK", True)
    monkeypatch.setattr(h, "TOP_N", 1)
    monkeypatch.setattr(h, "fetch_play_by_play", lambda page, ids: None)
    monkeypatch.setattr(h, "play_offset", lambda pb, c, dur: (6.0, "stream clock"))
    monkeypatch.setattr(h.smart_cut, "analyze", lambda raw, ff, off, pt, dur, pbp=None: {
        "key": 3.0, "start": 1.0, "end": 5.0, "how": "test", "center": [0.5, 0.5],
        "excitement": 10 if "clip-2" in str(raw) else 1})
    h.build([clip(1), clip(2), clip(3)])
    chapters = json.loads((reel.work / "chapters.json").read_text(encoding="utf-8"))
    assert len(chapters) == 1 and "Jan 25" in chapters[0] and seconds(h.REEL) < 5
    assert "check these clips.txt" in [x.name for x in reel.work.iterdir()]   # "test" isn't a sure detection


def test_limit_gets_its_own_title_and_stays_out_of_the_playlist(reel, monkeypatch):
    monkeypatch.setattr(h, "ADD_TO_TEAM_PLAYLIST", True)
    monkeypatch.setattr(gc, "add_to_playlist", lambda *a: pytest.fail("a trial must stay out of the playlist"))
    monkeypatch.setattr(gc, "youtube", lambda **k: FakeYouTube())
    h.use_limit(2)
    assert h.VIDEO_TITLE == "Test reel (first 2 plays)" and h.REEL.name == "Test reel (first 2 plays).mp4"
    h.cmd_run(args(local=False))
    assert h.NOTE.exists()


def test_incomplete_clip_list_stops_the_run():
    pages = {0: {"hits": [{"clip_metadata_id": str(i)} for i in range(36)], "next_offset": 36, "total_count": 40},
             36: {"hits": [{"clip_metadata_id": str(i)} for i in range(36, 40)], "next_offset": None, "total_count": 40}}
    assert len(h.read_all_pages(pages.__getitem__)) == 40
    pages[0]["next_offset"] = None                                          # GameChanger stops after one page
    with pytest.raises(SystemExit, match="only 36 of 40"):
        h.read_all_pages(pages.__getitem__)


def test_play_sentence_only_when_it_is_the_same_kind_of_play(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "PLAYS_DIR", tmp_path)
    monkeypatch.setattr(h, "PLAYER_ID", "p1")
    entry = lambda order, name, text: {"inning": 1, "half": "top", "order": order, "by": "p1",
                                       "name_template": {"template": name}, "final_details": [{"template": text}]}
    (tmp_path / "game-1.json").write_text(json.dumps({"team_players": {}, "plays": [
        entry(1, "Single", "someone singles"), entry(2, "Runner Out", "Jane caught stealing")]}), encoding="utf-8")
    c = lambda name: dict(clip(1), name=name)
    used = set()
    assert h.match_play(c("Double"), used) is None and not used             # contradicts: no sentence, entry stays free
    assert h.match_play(c("Caught Stealing"), used)["text"] == "Jane caught stealing"
    assert h.match_play(c("Single"), used)["text"] == "someone singles"


def test_public_page_is_not_taken_for_signed_in():
    assert gc.looks_signed_out("Pricing Support Sign In Join Us Example Team 12U 3-1 Fall 2024 HOME SCHEDULE")
    assert not gc.looks_signed_out("Example Team 12U 3-1 Fall 2024 HOME SCHEDULE TEAM STATS")
    assert not gc.looks_signed_out("Team news " + "x" * 300 + " Sign In Join Us")


def test_captions_drop_what_the_font_cannot_draw():
    assert h.plain_text("Jane - Single\nvs Firecrackers 12U 🧨🧨🥎 · Top 1") == "Jane - Single\nvs Firecrackers 12U · Top 1"


def test_make_reel_builds_the_right_commands():
    assert make_reel.engine_args("smart", 10, "always", True, 0) == ["run", "--quick", "--caption", "always", "--local"]
    assert make_reel.engine_args("top", 5, "brief", False, 0) == ["run", "--quick", "--top", "5", "--caption", "brief", "--again"]
    assert make_reel.engine_args("full", 10, "always", True, 3) == ["run", "--caption", "always", "--limit", "3", "--local"]


def test_make_reel_saves_settings_and_refuses_upload_without_confirmation(tmp_path, monkeypatch, capsys):
    url = "https://web.gc.com/teams/AbC123/2024-fall-x/players/abcdef12-3456-4abc-8def-1234567890ab"
    monkeypatch.setattr(sys, "argv", ["make_reel.py", "--folder", str(tmp_path), "--player-url", url, "--player-name", "Jane",
                                      "--season", "Fall 2024", "--reel", "smart", "--upload"])
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run without confirmation"))
    make_reel.main()
    assert "Nothing was changed" in capsys.readouterr().out
    saved = json.loads((tmp_path / "smart_highlights.json").read_text(encoding="utf-8"))
    assert saved["player_name"] == "Jane" and saved["privacy"] == "unlisted" and saved["delete_after_upload"] is False
