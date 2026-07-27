"""
tray.py - optional system tray icon for ahk_lite.

Needs `pystray` and `Pillow` (pip install pystray pillow). If they
aren't installed, run() raises TrayUnavailable and ahk_lite.py falls
back to running headless instead of failing outright.
"""

import os
import subprocess
import sys

try:
    import pystray
    from PIL import Image, ImageDraw
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


class TrayUnavailable(Exception):
    pass


HERE = os.path.dirname(os.path.abspath(__file__))
GUI_SCRIPT = os.path.join(HERE, "ahk_lite_gui.py")


def _make_icon_image(paused):
    color = (150, 150, 150) if paused else (0, 170, 110)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return image


def run(app):
    """Blocks until Quit is chosen, same role as keyboard.wait()."""
    if not AVAILABLE:
        raise TrayUnavailable("pystray/Pillow not installed")

    def on_reload(icon, item):
        app.reload()
        icon.icon = _make_icon_image(app.expander.paused)

    def on_toggle_pause(icon, item):
        app.toggle_pause()
        icon.icon = _make_icon_image(app.expander.paused)

    def on_open_editor(icon, item):
        subprocess.Popen([sys.executable, GUI_SCRIPT, app.config_path])

    def on_install_startup(icon, item):
        try:
            import install_startup
            path = install_startup.install()
            print(f"[ahk_lite] Startup shortcut installed: {path}")
        except Exception as exc:
            print(f"[ahk_lite] Could not install startup shortcut: {exc}")

    def on_quit(icon, item):
        icon.stop()
        app.quit()

    def is_paused(item):
        return app.expander.paused

    menu = pystray.Menu(
        pystray.MenuItem("Reload config", on_reload),
        pystray.MenuItem("Pause expansion", on_toggle_pause, checked=is_paused),
        pystray.MenuItem("Open editor", on_open_editor),
        pystray.MenuItem("Add to Windows startup", on_install_startup),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("ahk_lite", _make_icon_image(paused=False), "ahk_lite", menu)
    icon.run()
