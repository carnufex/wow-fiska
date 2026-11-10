"""Fiska control console: start/pause, the pause hotkey, and an overlay that
shows the search areas on top of the game so you can place the camera.

    .venv\\Scripts\\python fiska_gui.py            (records to rec\\ like --record rec)
    .venv\\Scripts\\python fiska_gui.py --no-record
    Any fiska.py option works too, e.g. --work-minutes 40,60.

The overlay is click-through and excluded from screen capture
(SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE), so the fisher never sees it.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
from collections import deque

import fiska  # sets per-monitor DPI awareness before any window exists

from imgui_bundle import hello_imgui, imgui  # noqa: E402

user32 = ctypes.windll.user32
SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fiska_settings.json")
DEFAULTS = {"toggle": "pause", "overlay": False}

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11
GA_ROOT = 2


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return {**DEFAULTS, **json.load(fh)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(settings):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError as err:
        fiska.log(f"could not save {SETTINGS}: {err}")


def find_game_window(title_part):
    """The game's top-level window whether or not it has focus (None if minimised)."""
    found = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def check(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and \
                title_part.lower() in fiska.window_title(hwnd).lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(proc(check), 0)
    return found[0] if found else None


# Keys the pause hotkey may be set to (not the mouse, not modifiers).
HOTKEY_CANDIDATES = [vk for vk in range(0x08, 0xFF)
                     if fiska.name_from_vk(vk) and vk not in (0x10, 0x11, 0x12)]


class Overlay(threading.Thread):
    """Transparent, click-through, capture-excluded window over the game client
    area, redrawn from fisher.overlay_shapes() a few times a second."""

    KEY = "#010101"   # the transparent colour

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.KEY)
        root.attributes("-transparentcolor", self.KEY)
        canvas = tk.Canvas(root, bg=self.KEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        root.withdraw()
        root.update()
        hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT |
                              WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            fiska.log("overlay: could not exclude it from screen capture (Windows 10 2004+ "
                      "needed) - it is hidden while fishing")
            self.app.overlay_capturable = True
        shown = None
        while not self.app.closing:
            want = self.app.settings["overlay"] and (not self.app.overlay_capturable or
                                                     not self.app.fishing_now())
            game = find_game_window(self.app.args.window) if want else None
            if game:
                box = fiska.client_box(game)
                if shown != box:
                    root.geometry(f"{box[2]}x{box[3]}+{box[0]}+{box[1]}")
                    root.deiconify()
                    shown = box
                self.draw(canvas, box)
            elif shown is not None:
                root.withdraw()
                shown = None
            root.update()
            time.sleep(0.15)
        root.destroy()

    def draw(self, canvas, box):
        canvas.delete("all")
        fisher = self.app.fisher
        shapes = fisher.overlay_shapes(box) if fisher else []
        for i, (label, (x, y, w, h), rgb) in enumerate(shapes):
            colour = "#%02x%02x%02x" % rgb
            x, y = x - box[0], y - box[1]
            width = 3 if label == "watching" else 2
            canvas.create_rectangle(x, y, x + w, y + h, outline=colour, width=width)
            canvas.create_text(x + 6, y + 4 + (i % 2) * 0, text=label, anchor="nw", fill=colour,
                               font=("Segoe UI", 11, "bold"))


class App:
    def __init__(self, argv):
        self.settings = load_settings()
        argv = list(argv)
        record = "--no-record" not in argv
        argv = [x for x in argv if x != "--no-record"]
        if record and "--record" not in argv:
            argv += ["--record", "rec"]
        self.argv = argv
        self.args = self.make_args()
        self.fisher = None
        self.thread = None
        self.closing = False
        self.overlay_capturable = False
        self.waiting_for_key = False
        self.lines = deque(maxlen=400)
        self.scroll = False
        fiska.LOG_LISTENERS.append(self.on_log)
        self.start_fisher()
        Overlay(self).start()

    def make_args(self):
        args = fiska.parse_args(self.argv + ["--start-paused"])
        if "--toggle" not in self.argv:
            args.toggle = self.settings["toggle"]
        return args

    def on_log(self, line):
        self.lines.append(line)
        self.scroll = True

    def start_fisher(self):
        self.args = self.make_args()
        self.fisher = fiska.Fisher(self.args)
        self.thread = threading.Thread(target=self.fisher.run, daemon=True)
        self.thread.start()

    def fishing_now(self):
        f = self.fisher
        return bool(f and f.running and f.status == "fishing")

    # -- gui -----------------------------------------------------------------
    def gui(self):
        f = self.fisher
        alive = self.thread is not None and self.thread.is_alive()
        if not alive and f and f.status != "stopped":
            f.status = "stopped"

        imgui.text("Status:")
        imgui.same_line()
        colour = (imgui.ImVec4(0.4, 0.9, 0.4, 1) if f and f.running and alive
                  else imgui.ImVec4(0.95, 0.75, 0.3, 1))
        state = ("RUNNING" if f and f.running else "PAUSED") if alive else "STOPPED"
        imgui.text_colored(colour, f"{state} - {f.status if f else ''}")

        if alive:
            if imgui.button("Pause" if f.running else "Start", imgui.ImVec2(120, 0)):
                f.running = not f.running
                fiska.log("RUNNING" if f.running else "PAUSED")
        else:
            if imgui.button("Start a new run", imgui.ImVec2(160, 0)):
                self.start_fisher()
                self.fisher.running = True
                fiska.log("RUNNING")
        imgui.same_line()
        imgui.text_disabled(f"(or press {self.args.toggle.upper()} in game)")

        imgui.separator()
        imgui.text(f"Pause key: {self.args.toggle.upper()}")
        imgui.same_line()
        if self.waiting_for_key:
            imgui.text_colored(imgui.ImVec4(1, 0.8, 0.2, 1), "press a key...")
            imgui.same_line()
            if imgui.small_button("cancel"):
                self.waiting_for_key = False
            self.poll_hotkey()
        elif imgui.small_button("change"):
            self.waiting_for_key = True
            self.keys_down = {vk for vk in HOTKEY_CANDIDATES
                              if user32.GetAsyncKeyState(vk) & 0x8000}

        changed, value = imgui.checkbox("Show the search areas on the game", self.settings["overlay"])
        if changed:
            self.settings["overlay"] = value
            save_settings(self.settings)
        if self.settings["overlay"]:
            for label, rgb in (("search area - the bobber is looked for here", (90, 160, 255)),
                               ("usual landing area / learned landing zone (yellow / green)",
                                (240, 200, 60)),
                               ("character - ignored", (150, 150, 150)),
                               ("watching - the bobber box", (255, 60, 60))):
                imgui.text_colored(imgui.ImVec4(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1),
                                   "   " + label)
            imgui.text_wrapped("Place the camera so the bobber lands inside the yellow box, "
                               "away from sun glitter, other players and NPCs.")
            if self.overlay_capturable:
                imgui.text_colored(imgui.ImVec4(1, 0.5, 0.3, 1),
                                   "   (hidden while fishing: this Windows can't hide it from capture)")

        imgui.separator()
        if f:
            s = f.stats
            real = s["caught"] + s["no_bite"] + s["no_loot"]
            rate = f"{100 * s['caught'] / real:.0f} %" if real else "-"
            imgui.text(f"Caught {s['caught']}   missed bites {s['no_bite']}   "
                       f"empty reel-ins {s['no_loot']}   ({rate})")
            imgui.text(f"Lures {s['lure']}   recast (no bobber / not water) "
                       f"{s['no_bobber']}/{s['not_water']}")
            if f.breaks.enabled and f.running:
                left = max(0.0, f.breaks.next_work - f.breaks.worked) / 60
                imgui.text_disabled(f"next break in ~{left:.0f} min of fishing")
            if self.args.record:
                imgui.text_disabled(f"recording to {os.path.abspath(self.args.record)}")

        imgui.separator()
        imgui.begin_child("log", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders.value)
        for line in list(self.lines):
            if "MISSED" in line or "no loot" in line:
                imgui.text_colored(imgui.ImVec4(1, 0.55, 0.45, 1), line)
            elif "caught!" in line:
                imgui.text_colored(imgui.ImVec4(0.5, 0.95, 0.5, 1), line)
            else:
                imgui.text_unformatted(line)
        if self.scroll:
            imgui.set_scroll_here_y(1.0)
            self.scroll = False
        imgui.end_child()

    def poll_hotkey(self):
        down = {vk for vk in HOTKEY_CANDIDATES if user32.GetAsyncKeyState(vk) & 0x8000}
        new = down - self.keys_down
        self.keys_down = down
        if not new:
            return
        vk = min(new)
        name = fiska.name_from_vk(vk)
        if vk == fiska.vk_from_name(self.args.key):
            fiska.log(f"{name.upper()} is the fishing key ({self.args.key.upper()}) - pick another")
            return
        self.waiting_for_key = False
        self.settings["toggle"] = name
        save_settings(self.settings)
        self.args.toggle = name
        if self.fisher:
            self.fisher.toggle = fiska.Toggle(vk)
            self.fisher.toggle.was_down = True       # don't count this press
        fiska.log(f"pause key is now {name.upper()}")

    def close(self):
        self.closing = True
        if self.fisher:
            self.fisher.quit = True
        if self.thread:
            self.thread.join(timeout=5)


def main():
    lock = fiska.single_instance()
    if lock is None:
        print("another fiska is already running - stop it first (two of them reel in "
              "each other's casts)")
        sys.exit(1)
    app = App(sys.argv[1:])
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "Fiska"
    params.app_window_params.window_geometry.size = [560, 640]
    params.fps_idling.fps_idle = 8
    params.callbacks.show_gui = app.gui
    params.callbacks.before_exit = app.close
    params.imgui_window_params.default_imgui_window_type = \
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window
    hello_imgui.run(params)


if __name__ == "__main__":
    main()
