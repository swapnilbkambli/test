"""
install_startup.py - adds ahk_lite to Windows startup for the current
user only (no admin rights needed), by dropping a small VBScript
launcher into your Startup folder. It runs ahk_lite.py hidden (via
pythonw.exe, no console window) every time you log in.

Run once:
    python install_startup.py

To undo:
    python install_startup.py --remove
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AHK_LITE_PY = os.path.join(HERE, "ahk_lite.py")
LAUNCHER_NAME = "ahk_lite.vbs"


def startup_folder():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set -- this only works on Windows.")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def pythonw_path():
    exe = sys.executable
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.exists(candidate) else exe


def install():
    folder = startup_folder()
    os.makedirs(folder, exist_ok=True)
    vbs_path = os.path.join(folder, LAUNCHER_NAME)
    pyw = pythonw_path()
    script = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run """{pyw}"" ""{AHK_LITE_PY}""", 0, False\r\n'
    )
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(script)
    return vbs_path


def remove():
    vbs_path = os.path.join(startup_folder(), LAUNCHER_NAME)
    if os.path.exists(vbs_path):
        os.remove(vbs_path)
        return vbs_path
    return None


if __name__ == "__main__":
    if "--remove" in sys.argv:
        removed = remove()
        if removed:
            print(f"[ahk_lite] Removed startup launcher: {removed}")
        else:
            print("[ahk_lite] No startup launcher was installed.")
    else:
        path = install()
        print(f"[ahk_lite] Installed startup launcher: {path}")
        print("[ahk_lite] ahk_lite will now start automatically at login.")
        print("[ahk_lite] To undo: python install_startup.py --remove")
