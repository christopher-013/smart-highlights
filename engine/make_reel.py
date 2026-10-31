"""
Make a highlight reel without touching any scripts: asks a few questions, then runs highlights.py.

  Make Highlights.bat                     asks everything (double-click it)
  py make_reel.py --folder "D:\\GameChanger\\My Team Fall 2024" --reel smart --local

Each team folder keeps its answers in smart_highlights.json, so you only give them once. Reels stay on this
computer unless you choose to upload, and an upload always asks you to confirm first.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
import sh_config  # noqa: E402

SETTINGS_NAME = "smart_highlights.json"


def ask(question, default=""):
    shown = f" [{default}]" if default else ""
    return input(f"{question}{shown}: ").strip().strip('"') or default


def choose(question, options, default):
    """options: [(answer, description)]. Returns the chosen answer."""
    print(question)
    for n, (_, text) in enumerate(options, 1):
        print(f"  {n}. {text}")
    while True:
        got = ask("Your choice", str(default))
        if got.isdigit() and 1 <= int(got) <= len(options):
            return options[int(got) - 1][0]
        print(f"Please type a number from 1 to {len(options)}.")


def load_settings(folder):
    try:
        return json.loads((folder / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def team_settings(folder, args):
    """Saved answers, with anything missing asked for (or taken from the command line)."""
    cfg = load_settings(folder)
    cfg["player_url"] = args.player_url or cfg.get("player_url") or ""
    while True:
        cfg["player_url"] = cfg["player_url"] or ask("GameChanger player link (web.gc.com/teams/.../players/...)")
        try:
            sh_config.parse_player_url(cfg["player_url"])
            break
        except ValueError as e:
            if args.no_questions:
                sys.exit(str(e))
            print(e)
            cfg["player_url"] = ""
    cfg["player_name"] = args.player_name or cfg.get("player_name") or ask("Player's name, as shown in captions")
    cfg["season"] = args.season or cfg.get("season") or ask("Season", "Fall 2024")
    if args.channel is not None:
        cfg["youtube_channel"] = args.channel
    cfg.setdefault("youtube_channel", "")
    cfg.setdefault("sport", "softball")
    cfg.setdefault("privacy", "unlisted")
    cfg.setdefault("caption", "always")
    cfg.setdefault("shared_browser_profile", True)      # one GameChanger sign-in for every team folder beside it
    cfg["delete_after_upload"] = False                  # the reel stays on this computer after uploading
    (folder / SETTINGS_NAME).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def engine_args(reel, top, caption, local, limit):
    out = ["run"] + {"full": [], "smart": ["--quick"], "top": ["--quick", "--top", str(top)]}[reel]
    out += ["--caption", caption]
    if limit:
        out += ["--limit", str(limit)]
    return out + (["--local"] if local else ["--again"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", help="the team's folder (made if needed)")
    ap.add_argument("--player-url")
    ap.add_argument("--player-name")
    ap.add_argument("--season")
    ap.add_argument("--channel", help="youtube.com/@yourchannel: uploads are refused on any other channel")
    ap.add_argument("--reel", choices=["smart", "top", "full"])
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--caption", choices=["always", "brief"])
    ap.add_argument("--limit", type=int, default=0, help="a quick trial: only the first N plays")
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--local", action="store_true", help="keep the reel on this computer")
    where.add_argument("--upload", action="store_true", help="upload it Unlisted (asks you to confirm)")
    ap.add_argument("--check", action="store_true", help="only test the settings and sign-ins")
    ap.add_argument("--no-questions", action="store_true", help="stop instead of asking, for testing")
    args = ap.parse_args()
    if args.no_questions:
        def no_input(*_):
            sys.exit("Something needed an answer, and --no-questions was given.")
        globals()["input"] = no_input

    folder = Path(args.folder or ask("Team folder (for example D:\\GameChanger\\My Team Fall 2024)")).expanduser()
    if not folder.name:
        sys.exit("Please give a team folder.")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        sys.exit(f"Couldn't use the folder '{folder}'. Please check the name and try again.")
    cfg = team_settings(folder.resolve(), args)
    env = dict(os.environ, SMART_HIGHLIGHTS_CONFIG=str(folder.resolve() / SETTINGS_NAME),
               SMART_HIGHLIGHTS_WORKDIR=str(folder.resolve()), PYTHONUNBUFFERED="1")
    engine = [sys.executable, "-u", str(CODE_DIR / "highlights.py")]
    if args.check:
        sys.exit(subprocess.run(engine + ["check"], env=env, cwd=str(folder)).returncode)

    reel = args.reel or choose("Which reel?", [("smart", "Smart Highlights: just the key play from every clip"),
                                               ("top", f"Top {args.top}: the most exciting plays"),
                                               ("full", "Full clips: every clip, untrimmed")], 1)
    local = args.local or (not args.upload and choose(
        "What should happen when it's finished?",
        [(True, "Keep it on this computer"), (False, "Upload it to YouTube as Unlisted, and keep a copy here")], 1))
    if not local:
        channel = cfg.get("youtube_channel") or "the channel you sign in with"
        print(f"\nThis will upload the reel to {channel} as {cfg['privacy']}. YouTube allows about 6 uploads a day.")
        if ask("Type UPLOAD to go ahead").upper() != "UPLOAD":
            print("Nothing was changed.")
            return
    run = engine + engine_args(reel, args.top, args.caption or cfg["caption"], local, args.limit)
    sys.exit(subprocess.run(run, env=env, cwd=str(folder)).returncode)


if __name__ == "__main__":
    main()
