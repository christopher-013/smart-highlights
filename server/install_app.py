"""
One-time setup: teach Windows what the public site's Start button means.

Registers smart-highlights:// for this Windows user only, then opens the site. "Set Up Smart Highlights.bat" runs this.

  python install_app.py            set it up
  python install_app.py --remove   undo it
"""
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEME = "smart-highlights"
SITE = "https://christopher-013.github.io/smart-highlights/"
KEY = rf"Software\Classes\{SCHEME}"


def start_command(python=None):
    """Runs the app with no window. The address that opened it is left out, so a website can't pass anything to it."""
    python = Path(python or sys.executable)
    quiet = python.with_name("pythonw.exe")
    return f'"{quiet if quiet.exists() else python}" "{HERE / "local_app.py"}" --from-site'


def install():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:Smart Highlights")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY + r"\shell\open\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, start_command())


def remove():
    import winreg
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY + sub)
        except FileNotFoundError:
            pass


def main():
    if sys.platform != "win32":
        sys.exit("This setup is for Windows.")
    if "--remove" in sys.argv:
        remove()
        print("Removed. The site's Start button won't start anything on this computer now.")
        return
    install()
    print(f"All set. You won't need this again. Use Smart Highlights from {SITE}")
    webbrowser.open(SITE)


if __name__ == "__main__":
    main()
