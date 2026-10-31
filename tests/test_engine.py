import shutil, subprocess, sys, tempfile
from pathlib import Path
import imageio_ffmpeg
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import sh_config, smart_cut


def test_player_url():
    ids = sh_config.parse_player_url("https://web.gc.com/teams/AbC123/2024-fall-team-12u/players/ABCDEF12-3456-4abc-8def-1234567890ab?tab=clips")
    assert ids == {"team_id": "AbC123", "team_slug": "2024-fall-team-12u", "player_id": "abcdef12-3456-4abc-8def-1234567890ab"}
    assert sh_config.parse_player_url("web.gc.com/teams/AbC123/players/abcdef12-3456-4abc-8def-1234567890ab")["team_slug"] == ""
    try:
        sh_config.parse_player_url("https://example.com")
        assert False
    except ValueError:
        pass


def test_channel_and_titles():
    assert sh_config.parse_channel("https://www.youtube.com/@My.Channel-1/videos") == "my.channel-1"
    assert sh_config.parse_channel("@abc") == "abc"
    assert sh_config.parse_channel("https://www.youtube.com/channel/UC123") == ""
    assert sh_config.fill_title("Smart Highlights: {player} - {team} {season}", "Jane", "Team 12U", "Fall 2024") \
        == "Smart Highlights: Jane - Team 12U Fall 2024"
    assert sh_config.fill_title("Top {n} - {player}", "Jane", "", "", n="{n}") == "Top {n} - Jane"
    assert sh_config.fill_title("Plain title", "x", "y", "z") == "Plain title"


def test_smart_cut_finds_bat_crack():
    ff = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    d = Path(tempfile.mkdtemp()) / "clip.mp4"
    # quiet field, a pitch at ~9.5 s, a bat crack at 12 s, then a runner moving for a few seconds
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=gray:s=640x360:r=30:d=30",
                    "-f", "lavfi", "-i", "aevalsrc='0.02*(random(0)-0.5)+if(between(t,12,12.02),0.9*sin(2*PI*2500*t),0)':s=16000:d=30",
                    "-vf", "drawbox=x='300+40*sin(t*20)':y=250:w=40:h=60:c=white:t=fill:enable='between(t,9.5,12.2)',"
                           "drawbox=x='320+(t-12)*60':y=200:w=30:h=50:c=black:t=fill:enable='between(t,12,17)'",
                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(d)], check=True)
    r = smart_cut.analyze(d, ff, recorded_at=25, play_type="single", duration=30)
    assert r and abs(r["key"] - 12.0) < 0.3, r
    assert r["start"] == round(r["key"] - smart_cut.LEAD, 2) and r["end"] > r["key"] + 3, r


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f(); print(name, "ok")
