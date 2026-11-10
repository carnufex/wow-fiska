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

from imgui_bundle import hello_imgui, icons_fontawesome_6 as fa, imgui  # noqa: E402

user32 = ctypes.windll.user32
SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fiska_settings.json")
DEFAULTS = {"toggle": "pause", "overlay": False}

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11
GA_ROOT = 2
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


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


# Frames you can edit on the game: (settings/args name, label, colour)
EDITABLE = (("prior", "usual landing area - the bobber is looked for here first", (240, 200, 60)),
            ("search", "search area", (90, 160, 255)),
            ("exclude", "character - ignored", (150, 150, 150)))
GRAB = 10        # px: how close to an edge counts as grabbing it
MIN_FRAC = 0.02  # smallest frame, fraction of the window


def hit_test(frames, x, y, w, h):
    """Which frame part is under (x, y) px in a w x h window.
    frames: {name: (x1, y1, x2, y2) fractions}. Returns (name, edges) where
    edges is a set of l/r/t/b (a corner has two) or {move}; or None."""
    for name, _, _ in EDITABLE:
        f = frames.get(name)
        if not f:
            continue
        x1, y1, x2, y2 = f[0] * w, f[1] * h, f[2] * w, f[3] * h
        if not (x1 - GRAB <= x <= x2 + GRAB and y1 - GRAB <= y <= y2 + GRAB):
            continue
        edges = set()
        if abs(x - x1) <= GRAB:
            edges.add("l")
        elif abs(x - x2) <= GRAB:
            edges.add("r")
        if abs(y - y1) <= GRAB:
            edges.add("t")
        elif abs(y - y2) <= GRAB:
            edges.add("b")
        if edges:
            return name, edges
    for name, _, _ in EDITABLE:                      # no edge: inside one = move it
        f = frames.get(name)
        if f and f[0] * w < x < f[2] * w and f[1] * h < y < f[3] * h:
            return name, {"move"}
    return None


def drag(frame, edges, dx, dy):
    """frame (x1, y1, x2, y2) fractions moved/resized by (dx, dy) fractions,
    kept inside the window and at least MIN_FRAC big."""
    x1, y1, x2, y2 = frame
    if "move" in edges:
        dx = min(max(dx, -x1), 1 - x2)
        dy = min(max(dy, -y1), 1 - y2)
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)
    if "l" in edges:
        x1 = min(max(0.0, x1 + dx), x2 - MIN_FRAC)
    if "r" in edges:
        x2 = max(min(1.0, x2 + dx), x1 + MIN_FRAC)
    if "t" in edges:
        y1 = min(max(0.0, y1 + dy), y2 - MIN_FRAC)
    if "b" in edges:
        y2 = max(min(1.0, y2 + dy), y1 + MIN_FRAC)
    return (x1, y1, x2, y2)


class Overlay(threading.Thread):
    """Transparent, click-through, capture-excluded window over the game client
    area, redrawn from fisher.overlay_shapes() a few times a second.

    Edit mode: the window takes the mouse (dimmed so you see it does) and the
    frames can be dragged by their edges and corners, or moved from inside."""

    KEY = "#010101"   # the transparent colour
    EDIT_BG = "#000010"

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.grabbed = None      # (name, edges, start x, start y, frame at start)
        self.box = None

    def set_clickthrough(self, hwnd, on):
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT) if on else (style & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def run(self):
        import tkinter as tk
        before = user32.GetForegroundWindow()
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.KEY)
        root.attributes("-transparentcolor", self.KEY)
        canvas = tk.Canvas(root, bg=self.KEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<ButtonPress-1>", self.on_press)
        canvas.bind("<B1-Motion>", self.on_drag)
        canvas.bind("<ButtonRelease-1>", self.on_release)
        canvas.bind("<Motion>", lambda e: self.on_hover(canvas, e))
        root.withdraw()
        root.update()
        hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
        self.hwnd, self.canvas = hwnd, canvas            # for tests and debugging
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT |
                              WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        # Tk activates its window when it starts; give the focus straight back
        # (the fisher only presses keys while the game is the foreground window).
        if user32.GetForegroundWindow() == hwnd and before:
            user32.SetForegroundWindow(before)
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            fiska.log("overlay: could not exclude it from screen capture (Windows 10 2004+ "
                      "needed) - it is hidden while fishing")
            self.app.overlay_capturable = True
        shown = None
        editing = False
        while not self.app.closing:
            edit = self.app.editing
            want = (self.app.settings["overlay"] or edit) and (
                edit or not self.app.overlay_capturable or not self.app.fishing_now())
            game = find_game_window(self.app.args.window) if want else None
            if edit != editing:
                editing = edit
                self.set_clickthrough(hwnd, not edit)
                canvas.configure(bg=self.EDIT_BG if edit else self.KEY)
                root.attributes("-alpha", 0.75 if edit else 1.0)
                self.grabbed = None
            if game:
                box = fiska.client_box(game)
                if shown != box:
                    root.geometry(f"{box[2]}x{box[3]}+{box[0]}+{box[1]}")
                    # Never take focus from the game: the fisher only acts while
                    # the game is the foreground window. deiconify() activated it.
                    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    shown = box
                self.box = box
                self.draw(canvas, box, edit)
                self.drawn = len(canvas.find_all())      # for tests and debugging
            elif shown is not None:
                user32.ShowWindow(hwnd, SW_HIDE)
                shown = None
            root.update()
            time.sleep(0.03 if edit else 0.15)
        root.destroy()
        del canvas, root

    # -- editing -------------------------------------------------------------
    def frames(self):
        a = self.app.args
        return {"prior": a.prior, "search": a.search, "exclude": a.exclude}

    def on_press(self, e):
        if not self.app.editing or not self.box:
            return
        hit = hit_test(self.frames(), e.x, e.y, self.box[2], self.box[3])
        if hit:
            name, edges = hit
            self.grabbed = (name, edges, e.x, e.y, self.frames()[name])

    def on_drag(self, e):
        if not self.grabbed or not self.box:
            return
        name, edges, x0, y0, start = self.grabbed
        frame = drag(start, edges, (e.x - x0) / self.box[2], (e.y - y0) / self.box[3])
        self.app.set_frame(name, frame, final=False)

    def on_release(self, e):
        if self.grabbed:
            name = self.grabbed[0]
            self.grabbed = None
            self.app.set_frame(name, self.frames()[name], final=True)

    def on_hover(self, canvas, e):
        if not self.app.editing or not self.box or self.grabbed:
            return
        hit = hit_test(self.frames(), e.x, e.y, self.box[2], self.box[3])
        edges = hit[1] if hit else set()
        cursor = ("fleur" if "move" in edges else
                  "size_nw_se" if edges in ({"l", "t"}, {"r", "b"}) else
                  "size_ne_sw" if edges in ({"r", "t"}, {"l", "b"}) else
                  "sb_h_double_arrow" if edges & {"l", "r"} else
                  "sb_v_double_arrow" if edges & {"t", "b"} else "arrow")
        canvas.configure(cursor=cursor)

    # -- drawing -------------------------------------------------------------
    def draw(self, canvas, box, edit):
        canvas.delete("all")
        if edit:
            w, h = box[2], box[3]
            frames = self.frames()
            for name, label, rgb in EDITABLE:
                f = frames.get(name)
                if not f:
                    continue
                colour = "#%02x%02x%02x" % rgb
                x1, y1, x2, y2 = f[0] * w, f[1] * h, f[2] * w, f[3] * h
                canvas.create_rectangle(x1, y1, x2, y2, outline=colour, width=3)
                for hx, hy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
                    canvas.create_rectangle(hx - 6, hy - 6, hx + 6, hy + 6, fill=colour, outline="")
                canvas.create_text(x1 + 8, y1 + 6, text=label, anchor="nw", fill=colour,
                                   font=("Segoe UI", 12, "bold"))
            canvas.create_text(w // 2, 24, fill="white", font=("Segoe UI", 14, "bold"),
                               text="Drag the edges or corners, or move a frame from inside. "
                                    "Untick 'Edit the frames' in Fiska when done.")
            return
        fisher = self.app.fisher
        shapes = fisher.overlay_shapes(box) if fisher else []
        for label, (x, y, w, h), rgb in shapes:
            colour = "#%02x%02x%02x" % rgb
            x, y = x - box[0], y - box[1]
            width = 3 if label == "watching" else 2
            canvas.create_rectangle(x, y, x + w, y + h, outline=colour, width=width)
            canvas.create_text(x + 6, y + 4, text=label, anchor="nw", fill=colour,
                               font=("Segoe UI", 11, "bold"))


# -- look --------------------------------------------------------------------
def rgba(hex_colour, alpha=1.0):
    h = hex_colour.lstrip("#")
    return imgui.ImVec4(int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha)


C_BG = rgba("#14161b")
C_CARD = rgba("#1d2027")
C_CARD_HI = rgba("#262a33")
C_TEXT = rgba("#e6e8ee")
C_MUTED = rgba("#8a91a0")
C_GREEN = rgba("#3ecf8e")
C_RED = rgba("#ef5b5b")
C_AMBER = rgba("#f2b544")
C_YELLOW = rgba("#f0c84a")
C_BLUE = rgba("#5aa2ff")
C_VIOLET = rgba("#a78bfa")


def setup_style():
    style = imgui.get_style()
    style.window_padding = imgui.ImVec2(18, 16)
    style.frame_padding = imgui.ImVec2(10, 6)
    style.item_spacing = imgui.ImVec2(8, 8)
    style.item_inner_spacing = imgui.ImVec2(8, 6)
    style.window_rounding = 0
    style.child_rounding = 10
    style.frame_rounding = 8
    style.popup_rounding = 10
    style.grab_rounding = 8
    style.tab_rounding = 8
    style.scrollbar_rounding = 8
    style.scrollbar_size = 10
    style.window_border_size = 0
    style.child_border_size = 0
    style.frame_border_size = 0
    colours = {
        imgui.Col_.window_bg: C_BG, imgui.Col_.child_bg: C_CARD, imgui.Col_.popup_bg: C_CARD,
        imgui.Col_.text: C_TEXT, imgui.Col_.text_disabled: C_MUTED,
        imgui.Col_.frame_bg: C_CARD_HI, imgui.Col_.frame_bg_hovered: rgba("#2f3440"),
        imgui.Col_.frame_bg_active: rgba("#363c4a"),
        imgui.Col_.button: C_CARD_HI, imgui.Col_.button_hovered: rgba("#323845"),
        imgui.Col_.button_active: rgba("#3a4150"),
        imgui.Col_.tab: C_BG, imgui.Col_.tab_hovered: C_CARD_HI, imgui.Col_.tab_selected: C_CARD,
        imgui.Col_.separator: rgba("#2a2e37"), imgui.Col_.border: rgba("#2a2e37"),
        imgui.Col_.check_mark: C_GREEN, imgui.Col_.slider_grab: C_BLUE,
        imgui.Col_.header: C_CARD_HI, imgui.Col_.header_hovered: rgba("#323845"),
        imgui.Col_.scrollbar_bg: C_BG, imgui.Col_.scrollbar_grab: rgba("#353a46"),
        imgui.Col_.modal_window_dim_bg: rgba("#000000", 0.55),
    }
    for col, value in colours.items():
        style.set_color_(col, value)


def muted(text):
    """Secondary text, wrapped at the window edge."""
    wrapped(text, C_MUTED)


def wrapped(text, colour=None):
    if colour is not None:
        imgui.push_style_color(imgui.Col_.text, colour)
    imgui.text_wrapped(text)
    if colour is not None:
        imgui.pop_style_color()


def chip(text, colour, right=False):
    """A rounded status pill, on the same line, right-aligned if asked."""
    size = imgui.calc_text_size(text)
    pad_x, pad_y = 12, 5
    w, h = size.x + 2 * pad_x, size.y + 2 * pad_y
    if right:
        imgui.same_line(imgui.get_window_width() - imgui.get_style().window_padding.x - w)
    p = imgui.get_cursor_screen_pos()
    dl = imgui.get_window_draw_list()
    bg = imgui.ImVec4(colour.x, colour.y, colour.z, 0.16)
    dl.add_rect_filled(p, imgui.ImVec2(p.x + w, p.y + h), imgui.get_color_u32(bg), h / 2)
    dl.add_text(imgui.ImVec2(p.x + pad_x, p.y + pad_y), imgui.get_color_u32(colour), text)
    imgui.dummy(imgui.ImVec2(w, h))


def primary_button(text, colour, width, font):
    dark = imgui.ImVec4(0.06, 0.07, 0.09, 1)
    imgui.push_style_color(imgui.Col_.button, colour)
    imgui.push_style_color(imgui.Col_.button_hovered,
                           imgui.ImVec4(min(1, colour.x * 1.12), min(1, colour.y * 1.12),
                                        min(1, colour.z * 1.12), 1))
    imgui.push_style_color(imgui.Col_.button_active,
                           imgui.ImVec4(colour.x * 0.85, colour.y * 0.85, colour.z * 0.85, 1))
    imgui.push_style_color(imgui.Col_.text, dark)
    imgui.push_style_var(imgui.StyleVar_.frame_rounding, 12.0)
    imgui.push_font(font, 18)
    clicked = imgui.button(text, imgui.ImVec2(width, 48))
    imgui.pop_font()
    imgui.pop_style_var()
    imgui.pop_style_color(4)
    return clicked


def toggle(label, value):
    """An on/off switch. Returns (changed, value)."""
    h = imgui.get_frame_height() * 0.85
    w = h * 1.8
    p = imgui.get_cursor_screen_pos()
    clicked = imgui.invisible_button(f"##{label}", imgui.ImVec2(w, h))
    if clicked:
        value = not value
    dl = imgui.get_window_draw_list()
    track = C_GREEN if value else rgba("#3a4050")
    if imgui.is_item_hovered():
        track = imgui.ImVec4(min(1, track.x * 1.1), min(1, track.y * 1.1), min(1, track.z * 1.1), 1)
    dl.add_rect_filled(p, imgui.ImVec2(p.x + w, p.y + h), imgui.get_color_u32(track), h / 2)
    r = h / 2 - 3
    cx = p.x + (w - r - 3 if value else r + 3)
    dl.add_circle_filled(imgui.ImVec2(cx, p.y + h / 2), r, imgui.get_color_u32(rgba("#f4f5f7")))
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text(label)
    return clicked, value


def swatch(colour):
    p = imgui.get_cursor_screen_pos()
    s = imgui.get_text_line_height()
    imgui.get_window_draw_list().add_rect_filled(
        imgui.ImVec2(p.x, p.y + 3), imgui.ImVec2(p.x + s - 6, p.y + s - 3),
        imgui.get_color_u32(colour), 3)
    imgui.dummy(imgui.ImVec2(s - 4, s))


def card_text(text, colour):
    """A tinted note with a coloured edge."""
    avail = imgui.get_content_region_avail().x
    wrap = avail - 28
    size = imgui.calc_text_size(text, False, wrap)
    p = imgui.get_cursor_screen_pos()
    h = size.y + 20
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(p, imgui.ImVec2(p.x + avail, p.y + h),
                       imgui.get_color_u32(imgui.ImVec4(colour.x, colour.y, colour.z, 0.10)), 10)
    dl.add_rect_filled(p, imgui.ImVec2(p.x + 3, p.y + h), imgui.get_color_u32(colour), 2)
    imgui.set_cursor_screen_pos(imgui.ImVec2(p.x + 16, p.y + 10))
    imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + wrap)
    imgui.text_unformatted(text)
    imgui.pop_text_wrap_pos()
    imgui.set_cursor_screen_pos(imgui.ImVec2(p.x, p.y + h + 8))
    imgui.dummy(imgui.ImVec2(0, 0))


def meter(label, fraction, colour, detail):
    imgui.text(label)
    imgui.same_line(170)
    imgui.push_style_color(imgui.Col_.plot_histogram, colour)
    imgui.push_style_color(imgui.Col_.frame_bg, C_CARD_HI)
    imgui.progress_bar(max(0.0, min(1.0, fraction)), imgui.ImVec2(-1, 0),
                       f"{100 * fraction:.1f}%  ({detail})")
    imgui.pop_style_color(2)



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
        self.editing = False
        self.lines = deque(maxlen=600)
        self.scroll = False
        self.important_only = False
        self.select_tab = None             # open this tab on the next frame
        self.disk = (0, 0)
        self.font_ui = self.font_bold = self.font_mono = None      # set when the window opens
        fiska.LOG_LISTENERS.append(self.on_log)
        self.start_fisher()
        Overlay(self).start()
        threading.Thread(target=self.disk_loop, daemon=True).start()

    def make_args(self):
        args = fiska.parse_args(self.argv + ["--start-paused"])
        if "--toggle" not in self.argv:
            args.toggle = self.settings["toggle"]
        for name in ("search", "prior", "exclude"):
            if self.settings.get(name) and f"--{name}" not in self.argv:
                setattr(args, name, tuple(self.settings[name]))
        # A usual landing area you placed yourself is not switched off after misses.
        args.prior_locked = bool(self.settings.get("prior")) or "--prior" in self.argv
        breaks = self.settings.get("breaks")
        if breaks and "--work-minutes" not in self.argv and "--break-minutes" not in self.argv:
            args.work_minutes, args.break_minutes = tuple(breaks["work"]), tuple(breaks["rest"])
        if "learn" in self.settings and "--learn" not in self.argv:
            args.learn = "on" if self.settings["learn"] else "off"
        return args

    def set_frame(self, name, frame, final):
        """A frame dragged on the overlay: used by the fisher at once (it reads
        args each cast), saved when the drag ends."""
        frame = tuple(round(v, 4) for v in frame)
        setattr(self.args, name, frame)
        if not final:
            return
        self.settings[name] = list(frame)
        save_settings(self.settings)
        fiska.log(f"{name} frame set to {', '.join(f'{v:.3f}' for v in frame)}")
        if name == "prior":
            self.args.prior_locked = True
            if self.fisher:
                self.fisher.prior_off = False
                self.forget_zone("you placed the usual landing area")

    def forget_zone(self, why):
        f = self.fisher
        if f and f.landings:
            f.landings.clear()
            f.save_landings()
            fiska.log(f"learned landing zone forgotten ({why}) - it is learned again from "
                      f"the next 5 catches")

    def reset_frames(self):
        defaults = fiska.parse_args([])
        for name in ("search", "prior", "exclude"):
            self.settings.pop(name, None)
            setattr(self.args, name, getattr(defaults, name))
        save_settings(self.settings)
        self.args.prior_locked = False
        if self.fisher:
            self.fisher.prior_off = False
        self.forget_zone("frames reset")
        fiska.log("frames reset to the defaults")

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
    def state(self):
        """(label, colour, icon) of what the fisher is doing right now."""
        f = self.fisher
        alive = self.thread is not None and self.thread.is_alive()
        if not alive or not f:
            return "Stopped", C_MUTED, fa.ICON_FA_CIRCLE_STOP
        if f.learning and f.learning.is_alive():
            return "Learning", C_VIOLET, fa.ICON_FA_BRAIN
        if f.status.startswith("break"):
            return f.status.capitalize(), C_BLUE, fa.ICON_FA_MUG_HOT
        if not f.running:
            return "Paused", C_AMBER, fa.ICON_FA_PAUSE
        if f.status == "fishing":
            return "Fishing", C_GREEN, fa.ICON_FA_FISH
        return f.status.capitalize(), C_MUTED, fa.ICON_FA_HOURGLASS_HALF

    def gui(self):
        f = self.fisher
        alive = self.thread is not None and self.thread.is_alive()
        if not alive and f and f.status not in ("stopped", "learning"):
            f.status = "stopped"
        self.header(f, alive)
        imgui.dummy(imgui.ImVec2(0, 6))
        self.tiles(f)
        self.cast_strip(f)
        imgui.dummy(imgui.ImVec2(0, 8))
        if imgui.begin_tab_bar("tabs"):
            tabs = ((f"{fa.ICON_FA_LIST}  Activity", self.tab_activity),
                    (f"{fa.ICON_FA_CROP_SIMPLE}  Camera & frames", self.tab_frames),
                    (f"{fa.ICON_FA_BRAIN}  Bite model", self.tab_model),
                    (f"{fa.ICON_FA_GEAR}  Settings", self.tab_settings))
            for n, (title, body) in enumerate(tabs):
                flags = imgui.TabItemFlags_.set_selected.value if self.select_tab == n else 0
                selected, _ = imgui.begin_tab_item(title, None, flags)
                if selected:
                    imgui.dummy(imgui.ImVec2(0, 4))
                    body(f)
                    imgui.end_tab_item()
            imgui.end_tab_bar()
        self.select_tab = None
        if self.waiting_for_key:
            self.poll_hotkey()

    def header(self, f, alive):
        imgui.push_font(self.font_bold, 22)
        imgui.text(f"{fa.ICON_FA_FISH_FINS}  Fiska")
        imgui.pop_font()
        label, colour, icon = self.state()
        chip(f"{icon}  {label}", colour, right=True)
        imgui.dummy(imgui.ImVec2(0, 4))
        width = imgui.get_content_region_avail().x
        if alive:
            running = f.running
            text = f"{fa.ICON_FA_PAUSE}   Pause" if running else f"{fa.ICON_FA_PLAY}   Start fishing"
            if primary_button(text, C_AMBER if running else C_GREEN, width, self.font_bold):
                f.running = not f.running
                fiska.log("RUNNING" if f.running else "PAUSED")
        else:
            if primary_button(f"{fa.ICON_FA_ROTATE_RIGHT}   Start a new run", C_GREEN, width,
                              self.font_bold):
                self.start_fisher()
                self.fisher.running = True
                fiska.log("RUNNING")
        hint = f"or press {self.args.toggle.upper()} in the game"
        if f and f.started and alive:
            mins = (time.monotonic() - f.started) / 60
            hint = f"{mins:.0f} min this run  ·  " + hint
            if f.breaks.enabled and f.running:
                left = max(0.0, f.breaks.next_work - f.breaks.worked) / 60
                hint += f"  ·  break in ~{left:.0f} min"
        muted(hint)

    def tiles(self, f):
        s = f.stats if f else {"caught": 0, "no_bite": 0, "no_loot": 0}
        real = s["caught"] + s["no_bite"] + s["no_loot"]
        rate = f"{100 * s['caught'] / real:.0f}%" if real else "–"
        cells = ((str(s["caught"]), "Caught", C_GREEN),
                 (str(s["no_bite"]), "Missed bites", C_RED),
                 (str(s["no_loot"]), "Empty reel-ins", C_AMBER),
                 (rate, "Success", C_TEXT))
        avail = imgui.get_content_region_avail().x
        gap = 8
        w = (avail - gap * 3) / 4
        h = 64
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        for i, (value, label, colour) in enumerate(cells):
            x = p.x + i * (w + gap)
            dl.add_rect_filled(imgui.ImVec2(x, p.y), imgui.ImVec2(x + w, p.y + h),
                               imgui.get_color_u32(C_CARD), 10)
            dl.add_rect_filled(imgui.ImVec2(x, p.y + 10), imgui.ImVec2(x + 3, p.y + h - 10),
                               imgui.get_color_u32(colour), 2)
            imgui.set_cursor_screen_pos(imgui.ImVec2(x + 14, p.y + 7))
            imgui.push_font(self.font_bold, 26)
            imgui.text_colored(colour, value)
            imgui.pop_font()
            imgui.set_cursor_screen_pos(imgui.ImVec2(x + 14, p.y + 40))
            imgui.text_colored(C_MUTED, label)
        imgui.set_cursor_screen_pos(imgui.ImVec2(p.x, p.y + h + 8))
        if f:
            muted(f"Lures {s['lure']}  ·  recast (no bobber / not water) "
                  f"{s['no_bobber']} / {s['not_water']}")

    def cast_strip(self, f):
        """The last casts as coloured blocks, newest on the right."""
        history = list(f.history) if f else []
        colours = {"caught": C_GREEN, "empty": C_AMBER, "no_bite": C_RED, "timeout": C_RED,
                   "not_water": C_MUTED, "no_bobber": C_MUTED}
        n = 30
        avail = imgui.get_content_region_avail().x
        gap = 3
        w = (avail - gap * (n - 1)) / n
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        recent = history[-n:]
        for i in range(n):
            j = i - (n - len(recent))
            colour = colours.get(recent[j], C_MUTED) if j >= 0 else C_CARD
            x = p.x + i * (w + gap)
            dl.add_rect_filled(imgui.ImVec2(x, p.y), imgui.ImVec2(x + w, p.y + 10),
                               imgui.get_color_u32(colour), 3)
        imgui.dummy(imgui.ImVec2(avail, 12))
        if imgui.is_item_hovered():
            imgui.set_tooltip("Last 30 casts: green caught, red missed bite, amber empty "
                              "reel-in, gray recast")

    # -- tabs ----------------------------------------------------------------
    def tab_activity(self, f):
        _, self.important_only = toggle("Important only", self.important_only)
        imgui.same_line()
        muted(f"  {len(self.lines)} lines")
        imgui.push_style_color(imgui.Col_.child_bg, C_CARD)
        imgui.begin_child("log", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders.value)
        imgui.push_font(self.font_mono, 14)
        for line in list(self.lines):
            colour = None
            if "caught!" in line:
                colour = C_GREEN
            elif "MISSED" in line or "no loot" in line or "failed" in line:
                colour = C_RED
            elif "not a bite" in line or "passed through" in line or "did not move" in line:
                colour = C_MUTED
            elif line[9:].startswith("bite") or "learning:" in line or "new bite model" in line:
                colour = C_BLUE
            if self.important_only and colour in (None, C_MUTED):
                continue
            wrapped(line, colour)
        imgui.pop_font()
        if self.scroll:
            imgui.set_scroll_here_y(1.0)
            self.scroll = False
        imgui.end_child()
        imgui.pop_style_color()

    def tab_frames(self, f):
        changed, value = toggle("Show the frames on the game", self.settings["overlay"])
        if changed:
            self.settings["overlay"] = value
            save_settings(self.settings)
        changed, value = toggle("Edit the frames", self.editing)
        if changed:
            self.editing = value
            fiska.log("editing the frames: drag them on the game" if value else "frames saved")
        if self.editing:
            card_text(f"{fa.ICON_FA_HAND_POINTER}  The game is dimmed and takes no clicks while you "
                      "edit. Drag an edge or a corner to resize a frame, drag inside it to move it. "
                      "Untick Edit when you are done.", C_AMBER)
        imgui.dummy(imgui.ImVec2(0, 4))
        imgui.separator_text("What the frames mean")
        for colour, name, text in (
                (C_YELLOW, "Usual landing area",
                 "looked in first. Put it around where your bobber lands, clear of the rod."),
                (C_GREEN, "Learned landing zone", "replaces it after 5 catches."),
                (C_BLUE, "Search area", "the fallback when nothing is found in the yellow box."),
                (C_MUTED, "Character", "ignored, plus a margin. Keep it to the body."),
                (C_RED, "Watching", "the bobber being watched right now.")):
            swatch(colour)
            imgui.same_line()
            imgui.text_colored(colour, name)
            imgui.same_line()
            muted(text)
        imgui.dummy(imgui.ImVec2(0, 6))
        if imgui.button(f"{fa.ICON_FA_ROTATE_LEFT}  Reset frames"):
            self.reset_frames()
        imgui.same_line()
        if imgui.button(f"{fa.ICON_FA_ERASER}  Forget the landing zone"):
            self.forget_zone("asked for")
        n = len(f.landings) if f else 0
        imgui.same_line()
        muted(f"  zone from {n} catches" if n >= 5 else f"  {n}/5 catches to learn the zone")

    def tab_model(self, f):
        model = f.bite_model if f else None
        if model:
            i, c = model.info, model.check
            real, false = i.get("real") or 0, i.get("false") or 0
            card_text(f"{fa.ICON_FA_CIRCLE_CHECK}  Learned from {real} real and {false} false bites "
                      f"in {i.get('sessions')} sessions  ·  updated {i.get('date')}", C_GREEN)
            if c and real and false:
                lost, through = c.get("lost", c.get("model_lost", 0)), c.get("through", c.get("model_through", 0))
                imgui.text("Judged session by session (each by a model that never saw it):")
                meter("Real bites kept", 1 - lost / real, C_GREEN, f"{real - lost} of {real}")
                meter("False bites stopped", 1 - through / false, C_BLUE, f"{false - through} of {false}")
            muted(f"Bites under {model.threshold:.2f} are ignored. Few false bites so far: the more "
                  f"you record, the better it gets.")
        else:
            card_text(f"{fa.ICON_FA_TRIANGLE_EXCLAMATION}  No model yet - the hand-made bite rules "
                      f"are used.", C_AMBER)
        imgui.dummy(imgui.ImVec2(0, 6))
        imgui.separator_text("Keep learning")
        if f:
            on = f.args.learn == "on"
            changed, on = toggle("Learn during breaks and when a run stops", on)
            if changed:
                f.args.learn = "on" if on else "off"
                self.settings["learn"] = on
                save_settings(self.settings)
            muted("A new model is used only if it is not worse on the sessions the current one "
                  "has never seen. Every model is kept in models\\.")
            busy = f.learning is not None and f.learning.is_alive()
            imgui.begin_disabled(busy or f.running)
            if imgui.button(f"{fa.ICON_FA_BRAIN}  Learn now"):
                f.learn("asked for")
            imgui.end_disabled()
            if f.running and not busy:
                imgui.same_line()
                muted("pause first")
            if f.learned:
                imgui.same_line()
                muted(f"  last: {f.learned}")
        imgui.dummy(imgui.ImVec2(0, 6))
        imgui.separator_text("Recordings")
        size, sessions = self.disk
        imgui.text(f"{size / 1e9:.1f} GB in {sessions} sessions" if sessions else "measuring...")
        muted("The search images are 94% of it; the bite model only needs the clips.")
        if imgui.button(f"{fa.ICON_FA_BROOM}  Free space..."):
            imgui.open_popup("Free space")
        opened, _ = imgui.begin_popup_modal("Free space", None, imgui.WindowFlags_.always_auto_resize.value)
        if opened:
            imgui.text("Delete the search images of all but the 10 newest sessions?")
            muted("Bite clips and traces stay, so the model keeps learning from them.\n"
                  "The images cannot be brought back.")
            imgui.dummy(imgui.ImVec2(0, 4))
            if imgui.button("Delete images"):
                threading.Thread(target=self.slim, daemon=True).start()
                imgui.close_current_popup()
            imgui.same_line()
            if imgui.button("Cancel"):
                imgui.close_current_popup()
            imgui.end_popup()

    def tab_settings(self, f):
        imgui.separator_text("Pause key")
        imgui.text(f"{fa.ICON_FA_KEYBOARD}  {self.args.toggle.upper()}")
        imgui.same_line()
        if self.waiting_for_key:
            imgui.text_colored(C_AMBER, "  press a key...")
            imgui.same_line()
            if imgui.small_button("cancel"):
                self.waiting_for_key = False
        elif imgui.small_button("change"):
            self.waiting_for_key = True
            self.keys_down = {vk for vk in HOTKEY_CANDIDATES
                              if user32.GetAsyncKeyState(vk) & 0x8000}
        muted(f"Starts and pauses fishing from inside the game. The fishing key is "
              f"{self.args.key.upper()}.")
        imgui.dummy(imgui.ImVec2(0, 6))
        imgui.separator_text("Breaks")
        if f:
            b = f.breaks
            on = b.enabled
            changed, on = toggle("Take breaks", on)
            if changed:
                b.work, b.rest = ((20.0, 30.0), (5.0, 10.0)) if on else ((0.0, 0.0), (0.0, 0.0))
                b.reset()
                self.save_breaks(b)
            if b.enabled:
                imgui.set_next_item_width(260)
                ch1, w0, w1 = imgui.drag_float_range2("fish (min)", b.work[0], b.work[1], 0.5, 5, 180,
                                                      "%.0f", "%.0f")
                imgui.set_next_item_width(260)
                ch2, r0, r1 = imgui.drag_float_range2("rest (min)", b.rest[0], b.rest[1], 0.5, 1, 60,
                                                      "%.0f", "%.0f")
                if ch1 or ch2:
                    b.work, b.rest = (w0, max(w0, w1)), (r0, max(r0, r1))
                    self.save_breaks(b)
                muted("Random lengths in these ranges, always between casts.")
        imgui.dummy(imgui.ImVec2(0, 6))
        imgui.separator_text("Recording")
        muted(f"{os.path.abspath(f.args.record)}" if f and f.args.record else "off (--no-record)")

    # -- actions -------------------------------------------------------------
    def save_breaks(self, b):
        self.settings["breaks"] = {"work": list(b.work), "rest": list(b.rest)}
        save_settings(self.settings)
        if b.enabled:
            b.next_work = min(b.next_work, b.work[1] * 60)

    def slim(self):
        import train_bite
        train_bite.slim(self.fisher.rec_root if self.fisher else None, 10, say=fiska.log)
        self.measure_disk()

    def measure_disk(self):
        root = (self.fisher.rec_root if self.fisher else None) or "rec"
        total, sessions = 0, 0
        for dirpath, _, files in os.walk(root):
            if "casts.csv" in files:
                sessions += 1
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        self.disk = (total, sessions)

    def disk_loop(self):
        while not self.closing:
            self.measure_disk()
            for _ in range(120):
                if self.closing:
                    return
                time.sleep(0.5)

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
    run_window(app)


def load_fonts(app):
    """Segoe UI (with Font Awesome icons merged in), Segoe UI Bold, Cascadia Mono;
    the bundled Roboto / Inconsolata where Windows lacks them."""
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

    def load(name, fallback, size, icons=True):
        path = os.path.join(fonts, name)
        if os.path.exists(path):
            params = hello_imgui.FontLoadingParams()
            params.inside_assets = False
            font = hello_imgui.load_font(path, size, params)
        else:
            font = hello_imgui.load_font(fallback, size)
        if icons:
            merge = hello_imgui.FontLoadingParams()
            merge.merge_to_last_font = True
            hello_imgui.load_font("fonts/Font_Awesome_6_Free-Solid-900.otf", size, merge)
        return font

    app.font_ui = load("segoeui.ttf", "fonts/Roboto/Roboto-Regular.ttf", 16)
    app.font_bold = load("segoeuib.ttf", "fonts/Roboto/Roboto-Bold.ttf", 16)
    app.font_mono = load("CascadiaMono.ttf", "fonts/Inconsolata-Medium.ttf", 14, icons=False)


def runner_params(app, title="Fiska"):
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = title
    params.app_window_params.window_geometry.size = [600, 820]
    params.fps_idling.fps_idle = 10
    params.imgui_window_params.default_imgui_window_type =         hello_imgui.DefaultImGuiWindowType.provide_full_screen_window
    params.imgui_window_params.background_color = C_BG
    params.callbacks.load_additional_fonts = lambda: load_fonts(app)
    params.callbacks.setup_imgui_style = setup_style
    params.callbacks.show_gui = app.gui
    params.callbacks.before_exit = app.close
    return params


def run_window(app):
    hello_imgui.run(runner_params(app))


if __name__ == "__main__":
    main()
