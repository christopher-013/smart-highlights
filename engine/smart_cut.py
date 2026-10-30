"""
Smart cut: find the key play inside a GameChanger clip and decide what to keep.

Layers (each falls back gracefully when it has nothing to go on):
  1. play-by-play  - GameChanger's pitch list for the at-bat (passed in as pbp): put in play vs. strike three
  2. pitch start   - keeps the wind-up and delivery before the key moment
  3. motion        - stillness before a pitch, players running after contact, the field settling at the end
  4. audio cues    - bat crack / glove pop, plus crowd cheering and the umpire's call afterwards
  5. excitement    - a score per play (play type, crowd reaction, how long the action lasts) for "Top N" reels
  6. framing       - where the action is, so the clip can zoom in gently toward it
  7. vision AI     - optional: a vision model looks at frames and confirms/corrects the cut (needs an API key)
"""
import array
import base64
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

FPS = 10                       # analysis frames per second
W, H = 96, 54                  # analysis frame size (tiny is enough to see where things move)
RATE = 16000                   # audio sample rate for analysis
HOP = 0.01                     # sound resolution (seconds)

# Camera is normally behind home plate: plate at the bottom middle, mound in the middle, first base to the right.
REGIONS = {
    "plate": (0.30, 0.40, 0.70, 1.00),      # batter, catcher, pitch arriving
    "infield": (0.10, 0.20, 0.90, 0.95),
    "first": (0.55, 0.15, 1.00, 0.90),      # toward first base (runner, throw)
}
PLAY_WEIGHT = {"home_run": 10, "inside_the_park_home_run": 10, "triple": 8, "double": 6, "single": 4,
               "double_play": 7, "triple_play": 10, "strikeout": 4, "dropped_third_strike_batter_out": 4,
               "dropped_third_strike": 3, "caught_stealing": 4}
END_LIMITS = {"single": (4, 9), "double": (8, 13), "triple": (14, 19), "home_run": (12, 24),
              "inside_the_park_home_run": (12, 24), "strikeout": (2, 4), "dropped_third_strike": (3, 6),
              "dropped_third_strike_batter_out": (3, 6), "_out": (3, 6), "double_play": (5, 10),
              "triple_play": (6, 12)}
OFFENSE = {"single", "double", "triple", "home_run", "inside_the_park_home_run", "inside_the_park_homerun"}
SEARCH_BEFORE, SEARCH_AFTER = 20, 1


# ---------------------------------------------------------------- reading the clip
def gray_frames(path, ffmpeg, t0, t1):
    """Tiny grayscale frames between t0 and t1 seconds: [(time, bytes)]."""
    raw = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(t0, 0):.2f}",
                          "-t", f"{max(t1 - t0, 0.5):.2f}", "-i", str(path), "-an",
                          "-vf", f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    n = W * H
    return [(round(max(t0, 0) + k / FPS, 2), raw[k * n:(k + 1) * n]) for k in range(len(raw) // n)]


def audio_samples(path, ffmpeg):
    raw = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(RATE), "-f", "s16le", "-"], capture_output=True).stdout
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    return a


# ---------------------------------------------------------------- layer 3: motion by region
def region_motion(frames, box):
    x0, y0, x1, y1 = int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H)
    out, prev = [], None
    for t, f in frames:
        if prev is None:
            out.append((t, 0.0))
        else:
            s = 0
            for y in range(y0, y1):
                r = y * W
                a, b = f[r + x0:r + x1], prev[r + x0:r + x1]
                s += sum(abs(p - q) for p, q in zip(a, b))
            out.append((t, s / ((x1 - x0) * (y1 - y0))))
        prev = f
    return out


def motion_centroid(frames, t0, t1):
    """Where the action is (0-1 coordinates), weighted by how much each spot changes."""
    sx = sy = tot = 0.0
    prev = None
    for t, f in frames:
        if prev is not None and t0 <= t <= t1:
            for y in range(0, H, 2):
                r = y * W
                for x in range(0, W, 2):
                    d = abs(f[r + x] - prev[r + x])
                    if d > 12:
                        sx += x * d
                        sy += y * d
                        tot += d
        prev = f
    if tot == 0:
        return 0.5, 0.6
    return sx / tot / W, sy / tot / H


def _median(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0.0


def _at(series, t, before=0.0, after=0.0, fn=max):
    v = [m for tt, m in series if t - before <= tt <= t + after]
    return fn(v) if v else 0.0


# ---------------------------------------------------------------- layer 4: sound
def sharp_sounds(a, min_contrast=6.0):
    """[(time, strength)] of sudden cracks/pops."""
    step = int(RATE * HOP)
    env, prev = [], (a[0] if a else 0)
    for i in range(0, len(a) - step, step):
        e = 0
        for x in a[i:i + step]:
            d = x - prev
            e += d * d
            prev = x
        env.append(e / step)
    out, win = [], int(1.0 / HOP)
    for i in range(1, len(env) - 1):
        if env[i] < env[i - 1] or env[i] < env[i + 1]:
            continue
        lo, hi = max(0, i - win), min(len(env), i + win)
        bg = sorted(env[lo:hi])[(hi - lo) // 2] or 1.0
        c = env[i] / bg
        if c >= min_contrast:
            out.append((round(i * HOP, 2), round(c, 1)))
    return out


def loudness(a, hop=0.1):
    """[(time, rms)] every 0.1 s: crowd cheering and voices raise it for a while."""
    step = int(RATE * hop)
    out = []
    for i in range(0, len(a) - step, step):
        seg = a[i:i + step]
        out.append((round(i / RATE, 2), (sum(x * x for x in seg) / step) ** 0.5))
    return out


def reaction_after(loud, t, start=0.4, end=4.0):
    """How much louder it gets after the play (cheer / umpire call) vs. the clip's normal level."""
    base = _median([v for _, v in loud]) or 1.0
    after = [v for tt, v in loud if t + start <= tt <= t + end]
    if not after:
        return 1.0, None
    peak = max(after)
    # when the reaction dies down: first time after the peak that it is back near normal for 0.5 s
    peak_t = next(tt for tt, v in loud if t + start <= tt <= t + end and v == peak)
    calm, settle = 0, None
    for tt, v in loud:
        if tt <= peak_t:
            continue
        calm = calm + 1 if v <= base * 1.4 else 0
        if calm >= 5:
            settle = tt - 0.3
            break
    return peak / base, settle


# ---------------------------------------------------------------- main analysis
# What the real clips showed (8 samples, 4 different cameras):
#  * GameChanger's "recorded at" time is when the scorekeeper tapped the play, 1 to 34 seconds AFTER it
#    happened, so the whole clip up to that moment is searched.
#  * Bat-on-ball is by far the loudest crack in a clip (hundreds to thousands of times the background).
#    Fouls earlier in the at-bat are cracks too, so the LAST big crack is the ball put in play.
#  * A strike-three pitch comes after a still moment (everyone set) and pops the catcher's glove; after the
#    third out everyone walks off, which is noisy but not still beforehand.
CRACK_MIN = 300          # a crack this loud (vs. background) is a bat hitting the ball
CRACK_SHARE = 0.3        # the last crack at least this share of the loudest one is the one put in play
POP_MIN = 10             # glove pop / umpire call for a strikeout
STILL = 1.0              # "everyone set" before a pitch: motion at most this times the clip's normal level
LEAD = 3.0               # seconds kept before the key moment (the pitcher's wind-up and delivery)
TAIL = 1.0               # seconds kept after the play settles


def _mean(series, t0, t1):
    v = [m for t, m in series if t0 <= t <= t1]
    return sum(v) / len(v) if v else 0.0


def is_strikeout(play_type, pbp=None):
    pt = (play_type or "").lower()
    if pbp and pbp.get("pitches"):
        last = pbp["pitches"][-1].lower()
        if "in play" in last:
            return False
        if "strike 3" in last or "foul tip" in last:
            return True
    return "strikeout" in pt or "dropped_third" in pt


def analyze(path, ffmpeg, recorded_at, play_type, duration, pbp=None):
    """Decide the key moment, start, end, excitement and zoom centre for one clip."""
    pt = (play_type or "").lower()
    hi = min(duration, recorded_at + 15)
    frames = gray_frames(path, ffmpeg, 0, hi)
    field = region_motion(frames, (0.0, 0.35, 1.0, 1.0))        # everything below the sky line
    base = _median([m for _, m in field]) or 0.1
    a = audio_samples(path, ffmpeg)
    sounds = [x for x in sharp_sounds(a, 4.0) if x[0] <= recorded_at + 1]
    loud = loudness(a)
    how = []
    key = None

    if is_strikeout(pt, pbp):
        # the last glove pop that comes right after a still moment
        pops = [(t, c) for t, c in sounds if c >= POP_MIN and _mean(field, t - 3, t - 0.5) / base <= STILL]
        if pops:
            key = pops[-1][0]
            how.append("strike-three pitch (still, then glove pop)")
    else:
        mx = max((c for _, c in sounds), default=0)
        if mx >= CRACK_MIN:
            key = [t for t, c in sounds if c >= CRACK_SHARE * mx][-1]
            how.append("bat crack")
        elif _mean(field, 0, 3) / base >= 1.5:  # no crack: the clip opens mid-play (hit just before it starts)
            key = 0.5
            how.append("clip starts mid-play")
        else:
            best = None
            for t, c in sounds:
                if c < 10 or _mean(field, t - 3, t - 0.5) / base > STILL * 1.2:
                    continue
                run = _mean(field, t + 0.5, t + 3.5) / base
                if best is None or run > best[1]:
                    best = (t, run)
            if best and best[1] >= 1.5:
                key = best[0]
                how.append("sound + players running")
    if key is None:
        return None

    start = max(0.0, key - LEAD)

    # end: when the field calms down (runner reached base, out made, batter walks off), within limits
    mn, mx_ = END_LIMITS.get(pt) or (END_LIMITS["strikeout"] if is_strikeout(pt, pbp) else END_LIMITS["_out"])
    end, calm = key + mx_, 0
    for t, m in field:
        if t < key + mn:
            continue
        if t > key + mx_:
            break
        calm = calm + 1 if m <= base * 1.3 else 0
        if calm >= FPS:                                    # quiet for a second
            end = t - 1.0
            how.append("action settled")
            break
    react, settle = reaction_after(loud, key)
    if settle and key + mn <= settle <= key + mx_ and settle > end:
        end = settle                                       # let the cheer / umpire call finish
        how.append("crowd/umpire")
    end = min(end + TAIL, duration)

    action_len = end - key
    excitement = round(PLAY_WEIGHT.get(pt, 3) * 2 + min(react, 6) * 2 + min(action_len, 12) * 0.5, 1)
    if pbp and pbp.get("scored"):
        excitement += 3
    cx, cy = motion_centroid(frames, start, end)
    return {"key": round(key, 2), "start": round(start, 2), "end": round(end, 2), "excitement": excitement,
            "reaction": round(react, 2), "center": [round(cx, 3), round(cy, 3)], "how": ", ".join(how)}


# ---------------------------------------------------------------- layer 6: gentle zoom
def zoom_filter(center, zoom=1.2):
    """ffmpeg crop that frames the action (applied before scaling to the reel size). The centre is pulled
    halfway back toward the middle of the picture so a wrong guess never crops the play out."""
    cx, cy = 0.5 + (center[0] - 0.5) * 0.5, 0.55 + (center[1] - 0.55) * 0.5
    cw, ch = f"iw/{zoom}", f"ih/{zoom}"
    x = f"max(0,min(iw-{cw},iw*{cx:.3f}-{cw}/2))"
    y = f"max(0,min(ih-{ch},ih*{cy:.3f}-{ch}/2))"
    return f"crop={cw}:{ch}:'{x}':'{y}'"


# ---------------------------------------------------------------- layer 7: vision AI check (optional)
AI_MODEL = "claude-sonnet-5-5"


def ai_key(folder):
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    f = Path(folder) / "anthropic_key.txt"
    if not k and f.exists():
        k = f.read_text(encoding="utf-8").strip()
    return k


def ai_refine(path, ffmpeg, play_name, guess, folder, sheet_path, font=""):
    """Show a vision model a grid of frames (2 per second, labeled) around the guessed cut and ask it to
    confirm the moment of the key play and where the highlight should start and end."""
    key = ai_key(folder)
    if not key:
        return None
    t0 = max(0.0, guess["start"] - 3)
    t1 = guess["end"] + 3
    vf = (f"fps=2,scale=320:-2,drawtext={font}text='%{{pts\\:flt\\:{t0:.1f}}}s':fontsize=22:fontcolor=yellow:"
          f"box=1:boxcolor=black@0.6:x=4:y=4,tile=6x5")
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t0:.2f}",
                        "-t", f"{min(t1 - t0, 15):.2f}", "-i", str(path), "-vf", vf, "-frames:v", "1",
                        str(sheet_path)], capture_output=True)
    if r.returncode != 0:
        vf = "fps=2,scale=320:-2,tile=6x5"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t0:.2f}", "-t",
                        f"{min(t1 - t0, 15):.2f}", "-i", str(path), "-vf", vf, "-frames:v", "1", str(sheet_path)])
    if not Path(sheet_path).exists():
        return None
    img = base64.b64encode(Path(sheet_path).read_bytes()).decode()
    prompt = (f"These are video frames from a youth softball/baseball game, 2 frames per second, left to right "
              f"then top to bottom. The first frame is at {t0:.1f} seconds into the clip; each next frame is "
              f"0.5 s later. The scorekeeper recorded this play as: '{play_name}'.\n"
              f"Find the key play (the moment of bat-ball contact for a hit or batted out, the strike-three "
              f"pitch for a strikeout, the catch/tag for an out). Choose where a short highlight should start "
              f"(just before the pitch is thrown) and end (when the runner reaches base or is out, or the "
              f"umpire signals strike three). Answer ONLY with JSON: "
              f'{{"key": seconds, "start": seconds, "end": seconds, "confident": true/false, "note": "short"}}')
    body = {"model": AI_MODEL, "max_tokens": 300, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
        {"type": "text", "text": prompt}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode())
        text = "".join(b.get("text", "") for b in out.get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        ans = json.loads(m.group(0)) if m else None
    except Exception:
        return None
    if not ans or not ans.get("confident"):
        return None
    try:
        s, e, k = float(ans["start"]), float(ans["end"]), float(ans["key"])
    except Exception:
        return None
    if not (t0 - 1 <= s < k < e <= t1 + 1 and 2 <= e - s <= 30):
        return None                                        # ignore answers that don't make sense
    return {"start": round(s, 2), "end": round(e, 2), "key": round(k, 2), "note": ans.get("note", "")}
