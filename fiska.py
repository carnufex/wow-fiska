"""One-key fishing helper.

The in-game addon does everything on a single key (default F10): it applies a
new lure when the old one has run out, casts, and reels in the bobber. This
script presses that key and decides *when* to press it.

There is no game event for a bite. Sound turned out to be useless for it
(ambience is as loud as the splash, and other players' splashes are heard
too), so the bite is SEEN instead:

  * vision  - right after each cast the script finds OUR bobber: what newly
              appeared (red feather weighted) and then sits still. A bite is
              a burst of motion in that small box - other players' bobbers
              are elsewhere and do not count.
  * addon   - the FiskaSignal addon paints two squares in the top-left
              corner: "channeling Fishing", "loot window opened" and
              "casting" (lure/bait). That tells us a cast really started,
              when a lure is being applied, when the bobber is gone, and
              whether a reel-in actually caught something.
  * audio   - optional extra check (--confirm both) or fallback.

If the bobber is not found, the script reels in and casts again.

Usage:
    python fiska.py --calibrate              # audio meter + addon status
    python fiska.py                          # fish (Pause key = start/pause)
    python fiska.py --debug-dir debug        # save bobber-search images
    python fiska.py --help
"""

import argparse
import csv
import ctypes
import json
import ctypes.wintypes as wt
import math
import os
import queue
import random
import sys
import threading
import time
from collections import deque

import numpy as np

# Physical pixels everywhere (window rects and screen capture must agree).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    ctypes.windll.user32.SetProcessDPIAware()

import mss  # noqa: E402  (after DPI awareness)
import mss.tools  # noqa: E402
import pyaudiowpatch as pyaudio  # noqa: E402


# --------------------------------------------------------------------------
# Win32: keyboard (SendInput with scan codes) and window lookup
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
user32.MapVirtualKeyW.restype = wt.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = (wt.HWND,)
user32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.GetClientRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.ClientToScreen.argtypes = (wt.HWND, ctypes.POINTER(wt.POINT))

NAMED_KEYS = {
    "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "space": 0x20,
}
EXTENDED_VKS = {0x2D, 0x24, 0x23, 0x21, 0x22}


def vk_from_name(name):
    """'f10' -> VK_F10, 'pause' -> VK_PAUSE, 'e' -> 'E'."""
    n = name.strip().lower()
    if n in NAMED_KEYS:
        return NAMED_KEYS[n]
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x70 + int(n[1:]) - 1
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    raise ValueError(f"unknown key: {name!r}")


def _key_event(vk, up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC),
                        dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def press_key(vk):
    """Key down, short human-ish hold, key up."""
    for up in (False, True):
        inp = _key_event(vk, up)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if not up:
            time.sleep(random.uniform(0.04, 0.09))


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def game_window(title_part):
    """Foreground window handle if its title matches, else None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    if title_part and title_part.lower() not in window_title(hwnd).lower():
        return None
    return hwnd


def client_box(hwnd):
    """Client area of a window in screen coordinates (left, top, w, h)."""
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


class Toggle:
    """Edge-detects a global toggle key with GetAsyncKeyState."""

    def __init__(self, vk):
        self.vk = vk
        self.was_down = False

    def pressed(self):
        down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
        hit = down and not self.was_down
        self.was_down = down
        return hit


# --------------------------------------------------------------------------
# Screen: capture, addon status pixel, bobber search, motion
# --------------------------------------------------------------------------

class Screen:
    def __init__(self, downsample=2):
        self.sct = (getattr(mss, "MSS", None) or mss.mss)()
        self.ds = downsample

    def grab_bgra(self, left, top, width, height):
        shot = self.sct.grab({"left": int(left), "top": int(top),
                              "width": max(1, int(width)), "height": max(1, int(height))})
        return np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)

    def grab_rgb(self, left, top, width, height):
        """Downsampled float RGB frame (same grid as grab_gray)."""
        img = self.grab_bgra(left, top, width, height)[:: self.ds, :: self.ds, [2, 1, 0]]
        return img.astype(np.float32)

    def grab_gray(self, left, top, width, height):
        img = self.grab_bgra(left, top, width, height)[:: self.ds, :: self.ds].astype(np.float32)
        return 0.114 * img[..., 0] + 0.587 * img[..., 1] + 0.299 * img[..., 2]

    def bridge(self, box):
        """Read the FiskaSignal squares. None if the addon is not visible.

        Square 1: blue = marker (addon loaded), red = channeling Fishing,
                  green = loot window opened within the last ~1.5 s.
        Square 2: blue = marker, red = casting something (lure, bait).
        Each square is 6x6 UI units; at UI scale 1 the screen is 768 units high.
        """
        left, top, _, h = box
        unit = max(1.0, h / 768.0)
        strip = self.grab_bgra(left, top + int(3 * unit) - 1, int(12 * unit) + 2, 3)

        def px(cx):
            x = min(strip.shape[1] - 2, max(1, int(round(cx))))
            return [int(v) for v in np.median(strip[:, x - 1:x + 2, :3].reshape(-1, 3), axis=0)]

        b, g, r = px(3 * unit)
        if b < 200 or (60 < r < 200) or (60 < g < 200):
            return None
        b2, _, r2 = px(9 * unit)
        return {"fishing": r >= 200, "loot": g >= 200, "casting": b2 >= 200 and r2 >= 200,
                "v11": b2 >= 200}


class FrameRing(threading.Thread):
    """Background capture of the search region (dry run): when you cast by hand,
    the frames from just before your key press are already here."""

    def __init__(self, ds, every=0.1, keep=12):
        super().__init__(daemon=True)
        self.ds, self.every = ds, every
        self.region = None
        self.frames = deque(maxlen=keep)
        self.lock = threading.Lock()

    def run(self):
        screen = Screen(self.ds)
        while True:
            region = self.region
            if region:
                frame = screen.grab_rgb(*region)
                with self.lock:
                    self.frames.append((time.monotonic(), region, frame))
            time.sleep(self.every)

    def before(self, t, region, n=3):
        with self.lock:
            got = [f for ft, r, f in self.frames if ft < t and r == region]
        return got[-n:] if len(got) >= n else None


def frac_box(box, frac):
    """(left, top, w, h) of a fractional sub-rectangle (x1, y1, x2, y2)."""
    left, top, w, h = box
    x1, y1, x2, y2 = frac
    return (left + int(x1 * w), top + int(y1 * h),
            max(1, int((x2 - x1) * w)), max(1, int((y2 - y1) * h)))


def box_mean(a, k):
    """Mean over every k x k window (valid positions only), via an integral image."""
    ii = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = a.cumsum(0).cumsum(1)
    s = ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]
    return s / (k * k)


def centered_box_mean(a, k):
    """Same-size map: mean of the k x k window centred on each pixel (zero padded)."""
    lo = k // 2
    return box_mean(np.pad(a, ((lo, k - 1 - lo), (lo, k - 1 - lo))), k)


def to_gray(frames):
    """(n, h, w, 3) RGB or (n, h, w) gray -> (n, h, w) gray."""
    if frames.ndim == 3:
        return frames
    return 0.299 * frames[..., 0] + 0.587 * frames[..., 1] + 0.114 * frames[..., 2]


def redness(frames):
    """How red each pixel is: R above the larger of G and B (0 for gray input)."""
    if frames.ndim == 3:
        return np.zeros_like(frames)
    return np.clip(frames[..., 0] - np.maximum(frames[..., 1], frames[..., 2]), 0, None)


def _stable_change(pre, post, positive_only=False):
    """Change between the means, minus what flickers within either set."""
    noise = np.maximum(pre.max(0) - pre.min(0), post.max(0) - post.min(0))
    diff = post.mean(0) - pre.mean(0)
    change = np.clip(diff, 0, None) if positive_only else np.abs(diff)
    return np.clip(change - noise, 0, None)


def locate_bobber(pre, post, k, exclude=None, red_weight=3.0, ring_weight=3.0, allowed=None):
    """Find what appeared after the cast.

    pre/post: lists of RGB (or gray) frames from just before the key press and
    just after the bobber landed. Pixels that flicker within either set
    (water, particles, a walking NPC, a flapping pet) are discounted by that
    variability: the bobber is new AND sits still. With colour frames, red
    that newly appeared (the bobber's feather) weighs red_weight times more,
    which keeps NPCs and pets that merely shifted from winning.
    exclude: boolean mask of pixels to ignore (character, cast bar, chat).
    allowed: optional boolean mask of where the bobber may be (the learned
    landing zone); scoring still sees the whole frame, only the pick is limited.

    The bobber is also small and alone on calm water: each spot scores its
    k x k mean minus ring_weight x the mean of the ring around it (the 3k x 3k
    area minus the centre). A UI window or message that pops up changes its
    surroundings too, so it scores below zero instead of winning. Weight 3 kept
    18/18 recorded bobbers and beat a pasted-in UI panel on all of them.

    Returns (cx, cy, score, score_map) in frame coordinates.
    """
    pre = np.stack(pre).astype(np.float32)
    post = np.stack(post).astype(np.float32)
    score = _stable_change(to_gray(pre), to_gray(post))
    if pre.ndim == 4 and red_weight:
        score += red_weight * _stable_change(redness(pre), redness(post), positive_only=True)
    if exclude is not None:
        score[exclude] = 0
    centre = centered_box_mean(score, k)
    ring = (9 * centered_box_mean(score, 3 * k) - centre) / 8
    smap = centre - ring_weight * ring
    # Near the frame edge the ring reaches outside it, reads as calm, and an
    # edge object (a party frame's animated portrait) wins. Keep clear of it.
    edge = (3 * k) // 2
    smap[:edge, :] = smap[-edge:, :] = -np.inf
    smap[:, :edge] = smap[:, -edge:] = -np.inf
    if allowed is not None:
        smap[~allowed] = -np.inf
    y, x = np.unravel_index(int(np.argmax(smap)), smap.shape)
    return x, y, float(smap[y, x]), smap


def save_debug_png(path, img, marks):
    """Gray (h, w) or RGB (h, w, 3) frame -> PNG with a red square per (cx, cy, half)."""
    if img.ndim == 2:
        rgb = np.repeat(np.clip(img, 0, 255).astype(np.uint8)[..., None], 3, axis=2)
    else:
        rgb = np.ascontiguousarray(img[..., :3]).astype(np.uint8)
    for cx, cy, half in marks:
        y1, y2 = max(0, cy - half), min(rgb.shape[0] - 1, cy + half)
        x1, x2 = max(0, cx - half), min(rgb.shape[1] - 1, cx + half)
        for yy in (y1, y2):
            rgb[yy, x1:x2 + 1] = (255, 0, 0)
        for xx in (x1, x2):
            rgb[y1:y2 + 1, xx] = (255, 0, 0)
    mss.tools.to_png(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), output=path)


class MotionTracker:
    """Frame-to-frame change inside the bobber box.

    A bite = the median change over the last `window` seconds reaches
    max(base * ratio, base + delta), where base is the median over the
    `base_s` seconds before that (minus a `gap`).

    Fitted on 46 recorded bites (2 dry runs + one 12-minute live run):
    - ratio 2.2 / delta 0.8: calm water peaks at 0.82 of the threshold, and a
      weak bite on a far-away bobber (median 1.6-1.7 on calm 0.67) is caught;
    - warmup 2.5 s: right after landing the bobber still settles, and the
      baseline is too short (earliest real bite in 96 catches: 3.2 s);
    - cap 5.5, checked only at the trigger: 112 real bites peaked at <= 4.51
      in the trigger window (single samples DURING a bite go higher, so the
      cap must not be applied after the trigger). Bigger trigger bursts were
      something passing through the box - a pet, a nameplate, the rod's lure
      glow - and those reel-ins came up empty.
    """

    def __init__(self, ratio, delta, window=0.1, base_s=3.0, gap=0.3, warmup=2.5,
                 cap=5.5, peak_window=0.15):
        self.ratio, self.delta, self.window = ratio, delta, window
        self.base_s, self.gap, self.warmup = base_s, gap, warmup
        self.cap, self.peak_window = cap, peak_window
        self.samples = deque()
        self.prev = None
        self.t0 = None

    def feed(self, t, frame):
        if self.t0 is None:
            self.t0 = t
        if self.prev is not None and self.prev.shape == frame.shape:
            self.samples.append((t, float(np.mean(np.abs(frame - self.prev)))))
            while self.samples and t - self.samples[0][0] > self.base_s + self.gap + 1.0:
                self.samples.popleft()
        self.prev = frame

    def calm(self):
        """Median motion so far: water ripples ~0.45-1.2, a static spot (wood,
        UI) ~0.0-0.2, a spot where someone walks ~4."""
        return float(np.median([m for _, m in self.samples])) if self.samples else 0.0

    def check(self, now):
        """(verdict, level, threshold, base, peak) or None while there is too
        little history. verdict: 'bite', 'burst' (over the cap) or None."""
        if self.t0 is None or now - self.t0 < self.warmup:
            return None
        recent = [m for t, m in self.samples if t > now - self.window]
        before = [m for t, m in self.samples
                  if now - self.gap - self.base_s < t <= now - self.gap]
        if len(before) < 20 or len(recent) < 3:
            return None
        base = float(np.median(before))
        level = float(np.median(recent))
        threshold = max(base * self.ratio, base + self.delta)
        peak = max(m for t, m in self.samples if t > now - self.peak_window)
        if level < threshold:
            verdict = None
        elif self.cap and peak > self.cap:
            verdict = "burst"
        else:
            verdict = "bite"
        return verdict, level, threshold, base, peak


# --------------------------------------------------------------------------
# Audio (WASAPI loopback of the default output device)
# --------------------------------------------------------------------------

SILENCE_DB = -100.0


def to_db(rms):
    return 20 * math.log10(rms / 32768.0) if rms > 0 else SILENCE_DB


class Listener:
    """Pushes (time, dBFS) for every ~20 ms audio block onto a queue."""

    def __init__(self, device_index=None, block_ms=20):
        self.pa = pyaudio.PyAudio()
        if device_index is None:
            dev = self.pa.get_default_wasapi_loopback()
        else:
            dev = self.pa.get_device_info_by_index(device_index)
        self.device = dev
        self.rate = int(dev["defaultSampleRate"])
        self.channels = max(1, int(dev["maxInputChannels"]))
        self.levels = queue.Queue()
        frames = max(64, int(self.rate * block_ms / 1000))
        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
            input=True, input_device_index=dev["index"],
            frames_per_buffer=frames, stream_callback=self._callback)

    def _callback(self, in_data, frame_count, time_info, status):
        samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        self.levels.put((time.monotonic(), to_db(rms)))
        return (None, pyaudio.paContinue)

    def drain(self):
        """All queued (time, dB) blocks. WASAPI loopback delivers nothing
        while the output is silent, so an empty list just means silence."""
        out = []
        while True:
            try:
                out.append(self.levels.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()


def list_devices():
    pa = pyaudio.PyAudio()
    try:
        default = pa.get_default_wasapi_loopback()
        print("Loopback devices (use --device <index>):")
        for dev in pa.get_loopback_device_info_generator():
            mark = "  <- default" if dev["index"] == default["index"] else ""
            print(f"  {dev['index']:3d}  {dev['name']}{mark}")
    finally:
        pa.terminate()


class SplashDetector:
    """A splash = a block louder than an absolute floor AND clearly louder
    than the recent background (rolling median), so steady ambience or
    music does not trigger it."""

    def __init__(self, threshold_db, margin_db, baseline_blocks=100, warmup_blocks=10):
        self.threshold_db = threshold_db
        self.margin_db = margin_db
        self.warmup = warmup_blocks
        self.history = deque(maxlen=baseline_blocks)

    def reset(self):
        self.history.clear()

    def baseline(self):
        return float(np.median(self.history)) if self.history else SILENCE_DB

    def feed(self, db):
        base = self.baseline()
        warm = len(self.history) >= self.warmup
        hit = warm and db >= self.threshold_db and db >= base + self.margin_db
        self.history.append(db)  # the median shrugs off short splashes
        return hit, base


# --------------------------------------------------------------------------
# Fishing loop
# --------------------------------------------------------------------------

LOG_FILE = None


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def parse_range(text):
    """'20,30' -> (20.0, 30.0); '25' -> (25.0, 25.0); '0' -> (0.0, 0.0) = off."""
    vals = tuple(float(v) for v in text.split(","))
    if len(vals) == 1:
        vals = vals * 2
    if len(vals) != 2 or vals[0] < 0 or vals[0] > vals[1]:
        raise argparse.ArgumentTypeError("expected MIN,MAX minutes (or one number)")
    return vals


class Breaks:
    """Natural pauses: fish for a random 20-30 min, rest for a random 5-10 min.

    Only time actually spent casting counts as work, and a pause the user took
    (Pause key, game out of focus) that was at least a minimum break long
    counts as the break. Breaks start between casts, never mid-cast."""

    def __init__(self, work, rest, rand=random.uniform):
        self.work, self.rest, self.rand = work, rest, rand
        self.worked = 0.0
        self.next_work = self._pick(work)

    def _pick(self, rng):
        return self.rand(rng[0], rng[1]) * 60

    @property
    def enabled(self):
        return self.work[1] > 0 and self.rest[1] > 0

    def add_work(self, seconds):
        self.worked += seconds

    def user_paused(self, seconds):
        """The user stopped us for `seconds`; a long enough stop is a break."""
        if self.enabled and seconds >= self.rest[0] * 60:
            self.reset()

    def due(self):
        return self.enabled and self.worked >= self.next_work

    def take(self):
        """Length in seconds of the break that is due now."""
        return self._pick(self.rest)

    def reset(self):
        self.worked = 0.0
        self.next_work = self._pick(self.work)


def parse_frac(text):
    vals = tuple(float(v) for v in text.split(","))
    if len(vals) != 4 or not all(0 <= v <= 1 for v in vals) or vals[0] >= vals[2] or vals[1] >= vals[3]:
        raise argparse.ArgumentTypeError("expected x1,y1,x2,y2 as fractions 0..1")
    return vals


class Fisher:
    def __init__(self, args):
        self.args = args
        self.key = vk_from_name(args.key)
        self.toggle = Toggle(vk_from_name(args.toggle))
        self.screen = Screen()
        self.listener = Listener(args.device)
        self.splash = SplashDetector(args.threshold, args.margin)
        self.running = not args.start_paused
        self.waiting_logged = False
        self.stats = {"caught": 0, "no_bite": 0, "no_loot": 0, "ignored": 0, "lure": 0,
                      "not_water": 0, "no_bobber": 0,
                      "dry_hit": 0, "dry_missed": 0, "dry_false": 0}
        self.cast_no = 0
        self.trace_audio, self.trace_motion, self.bites = [], [], []
        self.rec_frames = None
        self.clips = []
        self.ring = None
        self.landings = deque(maxlen=20)   # screen centres of bobbers that caught a fish
        self.window_size = None            # the game window the landings belong to
        self.cur = {}                      # metrics of the cast in progress -> casts.csv
        self.breaks = Breaks(args.work_minutes, args.break_minutes)
        for d in (args.debug_dir, args.record):
            if d:
                os.makedirs(d, exist_ok=True)

    # -- control -----------------------------------------------------------
    def ready(self):
        """Toggle key + focus check. Returns the game hwnd when we may act."""
        if self.toggle.pressed():
            self.running = not self.running
            log("RUNNING" if self.running else "PAUSED")
        if not self.running:
            return None
        hwnd = game_window(self.args.window)
        if hwnd is None and not self.waiting_logged:
            log(f"waiting for focus on '{self.args.window}'")
            self.waiting_logged = True
        if hwnd is not None:
            self.waiting_logged = False
        return hwnd

    def wait_ready(self):
        while True:
            hwnd = self.ready()
            if hwnd:
                return hwnd
            time.sleep(0.1)

    def take_break(self, deadline=None):
        """Rest between casts. Pause key during the break = PAUSED (ends it).
        Returns early at `deadline` (the --max-minutes end)."""
        length = self.breaks.take()
        until = time.monotonic() + length
        back = time.strftime("%H:%M", time.localtime(time.time() + length))
        log(f"break: {length / 60:.1f} min after {self.breaks.worked / 60:.1f} min of fishing "
            f"(back at {back}) - {self.summary()}")
        while time.monotonic() < until and not (deadline and time.monotonic() > deadline):
            if self.toggle.pressed():
                self.running = False
                log("PAUSED (break ended early)")
                break
            self.listener.drain()      # don't let ten minutes of audio pile up
            time.sleep(0.1)
        else:
            log("break over")
        self.breaks.reset()

    def summary(self):
        s = self.stats
        if self.args.dry_run:
            return f"detected {s['dry_hit']}, missed {s['dry_missed']}, false alarms {s['dry_false']}"
        return (f"caught {s['caught']}, no bite {s['no_bite']}, empty reel-ins "
                f"{s['no_loot']}, recast (no bobber / not water) {s['no_bobber']}/{s['not_water']}, "
                f"lures {s['lure']}")

    # -- vision ------------------------------------------------------------
    def frames(self, region, n=3, gap=0.07):
        out = []
        for i in range(n):
            out.append(self.screen.grab_rgb(*region))
            if i < n - 1:
                time.sleep(gap)
        return out

    def load_landings(self, box):
        """Landing zone from earlier sessions (--state), if the game window is the
        same size: the zone then guards the first casts too. A session's first
        empty reel-in was a pet on the dock, picked before 5 catches existed."""
        self.window_size = [int(box[2]), int(box[3])]
        if not self.args.state or not os.path.exists(self.args.state):
            return
        try:
            with open(self.args.state, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError) as err:
            log(f"could not read {self.args.state} ({err}) - learning the landing zone from scratch")
            return
        if state.get("window") != self.window_size:
            log(f"landing zone in {self.args.state} is for a {state.get('window')} window - not used")
            return
        self.landings.extend(tuple(p) for p in state.get("landings", []))
        log(f"landing zone from earlier sessions: {len(self.landings)} catches "
            f"(falls back to searching everywhere if nothing is there)")

    def add_landing(self, x, y):
        self.landings.append((float(x), float(y)))
        if self.args.state and self.window_size:
            try:
                with open(self.args.state, "w", encoding="utf-8") as fh:
                    json.dump({"window": self.window_size, "landings": list(self.landings)}, fh)
            except OSError as err:
                log(f"could not save {self.args.state}: {err}")

    def landing_zone(self, region, shape):
        """Where our bobbers have been landing (from casts that caught a fish),
        as an allowed-mask in search-frame coordinates; None until 5 catches.
        Other players walking on the dock or standing still after moving look
        'new and still' too, and outscored the bobber 8 times in one session."""
        if len(self.landings) < 5:
            return None
        pts = np.array(self.landings, dtype=float)
        med = np.median(pts, axis=0)
        dev = np.abs(pts - med).max(axis=0)
        floor = self.args.zone_min * region[3] / (self.args.search[3] - self.args.search[1])
        half_x, half_y = max(1.5 * dev[0], floor), max(1.5 * dev[1], floor)
        ds = self.screen.ds
        h, w = shape
        xs = (region[0] + np.arange(w) * ds)[None, :]
        ys = (region[1] + np.arange(h) * ds)[:, None]
        return (np.abs(xs - med[0]) <= half_x) & (np.abs(ys - med[1]) <= half_y)

    def find_bobber(self, region, pre, post):
        """Returns the bobber box in screen coords, or None."""
        a = self.args
        h, w = pre[0].shape[:2]
        k = max(5, int(a.bobber_size * h / (a.search[3] - a.search[1])))
        exclude = np.zeros((h, w), dtype=bool)
        sx1, sy1, sx2, sy2 = a.search
        ex1, ey1, ex2, ey2 = a.exclude
        # exclude box -> search-region frame coordinates
        fx = lambda v: int(round((v - sx1) / (sx2 - sx1) * w))
        fy = lambda v: int(round((v - sy1) / (sy2 - sy1) * h))
        exclude[max(0, fy(ey1)):max(0, fy(ey2)), max(0, fx(ex1)):max(0, fx(ex2))] = True

        zone = self.landing_zone(region, (h, w))
        where = "everywhere"
        if zone is not None:
            cx, cy, score, _ = locate_bobber(pre, post, k, exclude, a.red_weight, allowed=zone)
            where = "landing zone"
            if score < a.bobber_min:
                log(f"nothing in the usual landing zone (best {score:.1f}) - searching everywhere")
                zone = None
        if zone is None:
            cx, cy, score, _ = locate_bobber(pre, post, k, exclude, a.red_weight)
        self.rec_frames = (pre, post)
        found = score >= a.bobber_min
        self.cur.update(score=round(score, 1), search=where if found else "none")
        for d in (a.debug_dir, a.record):
            if d:
                path = os.path.join(d, f"cast{self.cast_no:04d}_bobber.png")
                save_debug_png(path, post[-1], [(cx, cy, k)])
        if not found:
            log(f"bobber not found (best score {score:.1f} < {a.bobber_min})")
            return None
        ds = self.screen.ds
        half = int(k * a.motion_box / 2) * ds
        left = region[0] + cx * ds - half
        top = region[1] + cy * ds - half
        self.cur.update(bobber_x=left + half, bobber_y=top + half)
        log(f"bobber at ({left + half}, {top + half}) score {score:.1f} [{where}]")
        return (left, top, 2 * half, 2 * half)

    def snapshot(self, region, name, mark=None):
        """Colour picture of the search area at an event, for --record."""
        if not self.args.record or region is None:
            return
        marks = []
        if mark:
            ds = self.screen.ds
            l, t, w, h = mark
            marks = [((l + w // 2 - region[0]) // ds, (t + h // 2 - region[1]) // ds, w // (2 * ds))]
        save_debug_png(os.path.join(self.args.record, f"cast{self.cast_no:04d}_{name}.png"),
                       self.screen.grab_rgb(*region), marks)

    # -- one cast ----------------------------------------------------------
    def wait_bridge(self, box, cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.screen.bridge(box)
            if state is not None and cond(state):
                return True
            time.sleep(0.03)
        return False

    def listen(self, box, has_bridge, bobber, cast_at, region=None):
        """Returns 'bite', 'timeout', 'ended', 'interrupted' or 'not_water'.

        With a bobber box (default --confirm visual) a bite is a burst of motion
        in that box that HOLDS for --confirm-time. --confirm both also needs a
        splash within 0.8 s; --confirm audio (or no bobber with --no-bobber
        audio) uses sound only.
        """
        a = self.args
        motion = (MotionTracker(a.motion_ratio, a.motion_min, a.motion_window,
                                warmup=a.motion_warmup, cap=a.motion_cap) if bobber else None)
        audio_only = motion is None
        last_block = time.monotonic()
        last_splash = -1e9
        last_note = 0.0
        last_big = -1e9                # last sample over the cap: something passed through
        burst_logged = -1e9
        water_checked = False
        pending = None                 # (t, threshold, level, base, peak): bite waiting to hold
        clip = deque(maxlen=150)       # ~1.8 s of bobber-box frames, for --record

        def keep_clip(kind, t):
            if self.args.record and len(self.clips) < 4:
                self.clips.append((kind, t, [ct for ct, _ in clip], [f for _, f in clip]))

        def bite(t, msg):
            """Real run: stop here. Dry run: note it and keep listening."""
            if self.bites and t - self.bites[-1] < 1.5:
                return False  # same burst
            log(("[dry] would reel in: " if a.dry_run else "") + msg)
            self.bites.append(t)
            keep_clip("bite", t)
            if len(self.bites) == 1:
                self.snapshot(region, "bite", bobber)
            return not a.dry_run

        while time.monotonic() - cast_at < a.timeout:
            if not self.ready():
                return "interrupted"
            now = time.monotonic()
            if has_bridge:
                state = self.screen.bridge(box)
                if state is not None and not state["fishing"]:
                    return "ended"

            blocks = self.listener.drain()
            if blocks:
                last_block = blocks[-1][0]
            elif now - last_block >= 0.02:
                # Loopback delivers nothing while the output is silent:
                # count one silent block per 20 ms of nothing.
                blocks = [(now, SILENCE_DB)]
                last_block = now
            self.trace_audio.extend(blocks)
            for t, db in blocks:
                hit, base = self.splash.feed(db)
                if hit:
                    last_splash = t
                    if audio_only and bite(t, f"bite  splash {db:.1f} dB (background {base:.1f} dB)"):
                        return "bite"

            if audio_only:
                time.sleep(0.01)
                continue

            frame = self.screen.grab_gray(*bobber)
            motion.feed(now, frame)
            if motion.samples:
                self.trace_motion.append(motion.samples[-1])
                if a.motion_cap and motion.samples[-1][1] > a.motion_cap:
                    last_big = now
            if self.args.record:
                clip.append((now, np.clip(frame, 0, 255).astype(np.uint8)))
            result = motion.check(now)
            if result is None:
                continue

            if not water_checked:
                # First full look at the box: water ripples, or something else?
                water_checked = True
                calm = motion.calm()
                self.cur["calm"] = round(calm, 2)
                if not a.min_calm <= calm <= a.max_calm:
                    log(f"bobber box does not look like water (calm {calm:.2f}, water is "
                        f"{a.min_calm}-{a.max_calm}) - wrong spot")
                    self.snapshot(region, "notwater", bobber)
                    return "not_water"

            verdict, level, threshold, base, peak = result
            if verdict == "burst" and not pending:
                if now - burst_logged > 1.5:
                    log(f"something passed through the bobber box (peak {peak:.1f} > cap "
                        f"{a.motion_cap}) - not a bite, ignored")
                    keep_clip("burst", now)
                    if self.cur.get("bursts", 0) == 0:
                        self.snapshot(region, "burst", bobber)
                    self.cur["bursts"] = self.cur.get("bursts", 0) + 1
                burst_logged = now
                pending = None
                continue

            if pending:
                t0, thr0, level0, base0, peak0 = pending
                if now - t0 < a.confirm_time:
                    continue
                held = [m for t, m in motion.samples if t0 < t <= now]
                hold = float(np.median(held)) if held else 0.0
                pending = None
                self.cur["hold_x_thr"] = round(hold / thr0, 2) if thr0 else ""
                if (a.motion_cap and hold > a.motion_cap) or hold > a.confirm_max * thr0:
                    # Kept growing after the trigger: something passing through.
                    # Real bites hold at 0.72-1.35x threshold; intruders 2.5-5x.
                    log(f"something passed through the bobber box (held {hold:.1f}, threshold "
                        f"{thr0:.1f}) - not a bite, ignored")
                    keep_clip("burst", now)
                    self.cur["bursts"] = self.cur.get("bursts", 0) + 1
                    last_big = now
                    continue
                if hold < a.confirm_hold * thr0:
                    log(f"blip at the bobber ({level0:.1f}) did not hold (median {hold:.1f} < "
                        f"{a.confirm_hold:.1f} x threshold {thr0:.1f}) - not a bite")
                    self.cur["blips"] = self.cur.get("blips", 0) + 1
                    continue
                if a.confirm == "both" and now - last_splash > 0.8 + a.confirm_time:
                    if now - last_note > 1.0:
                        log(f"bobber moved ({level0:.1f}) but no splash heard - ignored")
                        last_note = now
                    continue
                self.cur.update(level=round(level0, 2), threshold=round(thr0, 2), peak=round(peak0, 2))
                if bite(t0, f"bite  bobber motion {level0:.1f} (threshold {thr0:.1f}, calm "
                            f"{base0:.1f}), held {hold:.1f}"):
                    return "bite"
                continue

            if verdict == "bite" and now - last_big >= a.burst_quiet:
                pending = (now, threshold, level, base, peak)
        return "timeout"

    CSV_FIELDS = ("time", "cast", "outcome", "bobber_x", "bobber_y", "score", "search", "calm",
                  "level", "threshold", "peak", "hold_x_thr", "bursts", "blips", "duration_s")

    def cast_once(self):
        """One cast, with its outcome written to casts.csv (--record)."""
        self.cur = {"time": time.strftime("%Y-%m-%d %H:%M:%S")}
        start = time.monotonic()
        outcome = self._cast_once()
        if self.args.record and outcome:
            self.cur.update(cast=self.cast_no, outcome=outcome,
                            duration_s=round(time.monotonic() - start, 1))
            path = os.path.join(self.args.record, "casts.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=self.CSV_FIELDS, extrasaction="ignore")
                if new:
                    writer.writeheader()
                writer.writerow(self.cur)
        return outcome

    def _cast_once(self):
        a = self.args
        hwnd = self.wait_ready()
        box = client_box(hwnd)
        has_bridge = self.screen.bridge(box) is not None
        region = frac_box(box, a.search)
        use_vision = a.confirm != "audio"

        if a.dry_run:
            # You cast; we only watch. Frames from just before your press come
            # from the background ring.
            if not has_bridge:
                log("--dry-run needs the FiskaSignal addon (it tells us when you cast)")
                time.sleep(2.0)
                return None
            if use_vision:
                if self.ring is None:
                    self.ring = FrameRing(self.screen.ds)
                    self.ring.start()
                self.ring.region = region
            while True:
                if not self.ready():
                    return None
                state = self.screen.bridge(box)
                if state and state["fishing"]:
                    break
                time.sleep(0.03)
            cast_at = time.monotonic()
            pre = self.ring.before(cast_at - 0.05, region) if use_vision else None
            self.cast_no += 1
            log("you cast (addon confirms Fishing)")
        else:
            pre = self.frames(region) if use_vision else None
            self.cast_no += 1
            press_key(self.key)
            cast_at = time.monotonic()
            if has_bridge:
                started = self.wait_bridge(box, lambda s: s["fishing"] or s["casting"], a.cast_wait)
                state = self.screen.bridge(box) or {}
                if state.get("casting") and not state.get("fishing"):
                    self.stats["lure"] += 1
                    log("applying lure/bait - waiting for the cast to finish")
                    self.wait_bridge(box, lambda s: not s["casting"], a.lure_timeout)
                    time.sleep(random.uniform(0.3, 0.7))
                    return "lure"
                if not started:
                    log("key pressed but nothing started - trying again")
                    time.sleep(random.uniform(0.6, 1.2))
                    return "nothing_started"
                log("cast (addon confirms Fishing)")
            else:
                log("cast")

        self.trace_audio, self.trace_motion, self.bites = [], [], []
        self.rec_frames = None
        self.clips = []
        time.sleep(max(0.0, a.grace - (time.monotonic() - cast_at)))
        bobber = None
        if use_vision and pre is not None:
            bobber = self.find_bobber(region, pre, self.frames(region, n=4, gap=0.1))
        elif use_vision:
            log("no frames from before the cast")
        if use_vision and bobber is None and a.no_bobber == "recast":
            self.stats["no_bobber"] += 1
            return self.give_up(box, has_bridge, "bobber not found", "no_bobber")
        self.listener.drain()
        self.splash.reset()

        result = self.listen(box, has_bridge, bobber, cast_at, region)
        if result == "interrupted":
            log("interrupted - recasting when resumed")
            return "interrupted"
        if result == "not_water":
            self.stats["not_water"] += 1
            self.save_record(cast_at, time.monotonic(), False, bobber)
            return self.give_up(box, has_bridge, "wrong spot", "not_water")
        if result == "ended":
            end_t = time.monotonic()
            loot = self.wait_bridge(box, lambda s: s["loot"], 2.0)
            if a.dry_run:
                self.report_dry(end_t, loot)
            else:
                self.stats["no_bite"] += 1
                log(f"Fishing channel ended without a bite{' (but loot?!)' if loot else ''}  "
                    f"({self.summary()})")
            self.save_record(cast_at, end_t, loot, bobber)
            self.snapshot(region, "ended", bobber)
            time.sleep(0.0 if a.dry_run else random.uniform(0.3, 0.8))
            return "no_bite"
        if result == "timeout":
            self.stats["no_bite"] += 1
            log(f"no bite within {a.timeout:.0f}s  ({self.summary()})")
            self.save_record(cast_at, time.monotonic(), False, bobber)
            state = self.screen.bridge(box) if has_bridge else None
            if state and state["fishing"] and not a.dry_run:
                press_key(self.key)  # still channeling: reel in so the next press casts
            time.sleep(random.uniform(0.5, 1.2))
            return "timeout"

        time.sleep(random.uniform(a.reaction_min, a.reaction_max))
        if not self.ready():
            return "interrupted"
        press_key(self.key)
        end_t = time.monotonic()
        if has_bridge:
            loot = self.wait_bridge(box, lambda s: s["loot"], 3.0)
            if loot:
                self.stats["caught"] += 1
                self.add_landing(bobber[0] + bobber[2] / 2, bobber[1] + bobber[3] / 2)
                log(f"caught!  ({self.summary()})")
            else:
                self.stats["no_loot"] += 1
                log(f"reeled in but no loot window (false bite?)  ({self.summary()})")
            self.save_record(cast_at, end_t, loot, bobber)
        else:
            loot = None
            self.stats["caught"] += 1
            log(f"reeled in  ({self.summary()})")
        time.sleep(random.uniform(a.loot_min, a.loot_max))
        return "caught" if loot or loot is None else "empty"

    def give_up(self, box, has_bridge, why, outcome):
        """Reel in and cast again (real run) / wait for your reel-in (dry run)."""
        if self.args.dry_run:
            log(f"{why} - a real run would reel in and recast")
            self.wait_bridge(box, lambda s: not s["fishing"], self.args.timeout)
            return outcome
        log(f"{why} - reeling in and recasting  ({self.summary()})")
        state = self.screen.bridge(box) if has_bridge else None
        if state is None or state["fishing"]:
            press_key(self.key)
        time.sleep(random.uniform(1.0, 1.8))
        return outcome

    def report_dry(self, end_t, loot):
        """You reeled in (dry run). Compare with what the detector said, and show
        the loudest sound and biggest bobber motion just before your reel-in."""
        lo, hi = end_t - 2.5, end_t - 0.1
        audio = [(t, db) for t, db in self.trace_audio if lo <= t <= hi]
        motion = [(t, m) for t, m in self.trace_motion if lo <= t <= hi]
        calm = [m for t, m in self.trace_motion if t < lo]
        bg = [d for t, d in self.trace_audio if t < lo]
        detail = []
        if audio:
            t, db = max(audio, key=lambda x: x[1])
            detail.append(f"loudest {db:.1f} dB at -{end_t - t:.1f}s"
                          + (f" (typical {np.median(bg):.1f})" if bg else ""))
        if motion:
            t, m = max(motion, key=lambda x: x[1])
            detail.append(f"motion peak {m:.2f} at -{end_t - t:.1f}s"
                          + (f" (calm {np.median(calm):.2f})" if calm else ""))
        hits = [t for t in self.bites if lo <= t <= hi]
        false = len(self.bites) - len(hits)
        self.stats["dry_false"] += false
        if not loot:
            verdict = "no loot (reeled in without a fish)"
        elif hits:
            self.stats["dry_hit"] += 1
            if self.cur.get("bobber_x"):
                self.add_landing(self.cur["bobber_x"], self.cur["bobber_y"])
            verdict = f"DETECTED ({end_t - hits[-1]:.1f}s before you)"
        else:
            self.stats["dry_missed"] += 1
            verdict = "MISSED"
        log(f"you reeled in, {verdict}; {', '.join(detail)}; false alarms this cast {false}"
            f"  ({self.summary()})")

    def save_record(self, cast_at, end_t, loot, bobber):
        if not self.args.record:
            return
        path = os.path.join(self.args.record, f"cast{self.cast_no:04d}.npz")
        pre, post = self.rec_frames or ([np.zeros((1, 1, 3))], [np.zeros((1, 1, 3))])
        np.savez_compressed(
            path, cast_at=cast_at, end_t=end_t, loot=loot,
            pre=np.clip(np.stack(pre), 0, 255).astype(np.uint8),
            post=np.clip(np.stack(post), 0, 255).astype(np.uint8),
            bobber=np.array(bobber if bobber else (-1, -1, 0, 0)),
            audio=np.array(self.trace_audio or [(0.0, SILENCE_DB)]),
            motion=np.array(self.trace_motion or [(0.0, 0.0)]),
            bites=np.array(self.bites, dtype=float),
            **self.clip_arrays())

    def clip_arrays(self):
        """Bobber-box frames around each bite/burst: clipN_kind, clipN_t, clipN_times, clipN_frames."""
        out = {}
        for i, (kind, t, times, frames) in enumerate(self.clips):
            if frames and all(f.shape == frames[0].shape for f in frames):
                out[f"clip{i}_kind"] = np.array(kind)
                out[f"clip{i}_t"] = np.array(t)
                out[f"clip{i}_times"] = np.array(times)
                out[f"clip{i}_frames"] = np.stack(frames)
        return out

    def run(self):
        global LOG_FILE
        a = self.args
        if a.record:
            LOG_FILE = open(os.path.join(a.record, time.strftime("fiska-%Y%m%d-%H%M%S.log")),
                            "a", encoding="utf-8")
            log(f"recording to {os.path.abspath(a.record)}: casts.csv, per-cast .npz and .png, this log")
        log(f"Audio: {self.listener.device['name']}")
        log(f"Key {a.key.upper()}, toggle {a.toggle.upper()}, confirm={a.confirm}, "
            f"threshold {a.threshold} dB, margin {a.margin} dB, window '{a.window}'")
        log("RUNNING" if self.running else f"PAUSED - press {a.toggle.upper()} to start")
        started = time.monotonic()
        deadline = started + a.max_minutes * 60 if a.max_minutes else None
        if self.breaks.enabled and not a.dry_run:
            log(f"breaks: {a.break_minutes[0]:g}-{a.break_minutes[1]:g} min after every "
                f"{a.work_minutes[0]:g}-{a.work_minutes[1]:g} min of fishing "
                f"(first after {self.breaks.next_work / 60:.1f} min)")
        bridge_logged = None
        try:
            while not (deadline and time.monotonic() > deadline):
                if self.breaks.due() and not a.dry_run:
                    self.take_break(deadline)
                    continue
                idle_from = time.monotonic()
                hwnd = self.wait_ready()
                self.breaks.user_paused(time.monotonic() - idle_from)
                if self.window_size is None:
                    self.load_landings(client_box(hwnd))
                has_bridge = self.screen.bridge(client_box(hwnd)) is not None
                if has_bridge != bridge_logged:
                    log("FiskaSignal addon: " + ("detected" if has_bridge else
                        "NOT detected - using timeouts, no loot confirmation"))
                    state = self.screen.bridge(client_box(hwnd)) if has_bridge else None
                    if state is not None and not state["v11"]:
                        log("FiskaSignal is the OLD version (no 'casting' square): lure/bait casts "
                            "are not waited out - /reload in game to load 1.1")
                    bridge_logged = has_bridge
                cast_from = time.monotonic()
                self.cast_once()
                self.breaks.add_work(time.monotonic() - cast_from)
            else:
                log(f"max runtime of {a.max_minutes} min reached")
        except KeyboardInterrupt:
            pass
        finally:
            self.listener.close()
            log(f"stopped after {(time.monotonic() - started) / 60:.1f} min - {self.summary()}")


def calibrate(args):
    listener = Listener(args.device)
    screen = Screen()
    log(f"Listening on: {listener.device['name']}  ({listener.rate} Hz, {listener.channels} ch)")
    log("Cast manually in game and note the peak when the splash comes. Ctrl+C to stop.")
    window = deque(maxlen=50)  # ~1 s of blocks
    try:
        while True:
            for _, db in listener.drain() or [(0, SILENCE_DB)]:
                window.append(db)
            peak = max(window)
            hwnd = game_window(args.window)
            if hwnd is None:
                addon = "no game window in focus"
            else:
                st = screen.bridge(client_box(hwnd))
                addon = "addon missing" if st is None else (
                    ("FISHING" if st["fishing"] else "idle") + (" +LOOT" if st["loot"] else ""))
            bar = "#" * int(max(0, min(40, (peak + 70) * 40 / 70)))
            mark = "*" if peak >= args.threshold else " "
            sys.stdout.write(f"\r peak(1s) {peak:6.1f} dB {mark}|{bar:<40}|  {addon:<26}")
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
    finally:
        listener.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="One-key fishing: press, wait for OUR splash, press.")
    p.add_argument("--calibrate", action="store_true", help="live audio meter + addon status")
    p.add_argument("--list-devices", action="store_true", help="list loopback audio devices")
    p.add_argument("--device", type=int, help="loopback device index (default: default output)")
    p.add_argument("--key", default="f10", help="addon key (default: f10)")
    p.add_argument("--toggle", default="pause", help="start/pause key (default: pause)")
    p.add_argument("--start-paused", action="store_true", help="wait for the toggle key first")
    p.add_argument("--window", default="World of Warcraft",
                   help="only act while the foreground window title contains this")
    p.add_argument("--max-minutes", type=float, default=0, help="stop after N minutes (0 = never)")
    p.add_argument("--work-minutes", type=parse_range, default=(20.0, 30.0),
                   help="fish a random MIN,MAX minutes between breaks (default 20,30; 0 = no breaks)")
    p.add_argument("--break-minutes", type=parse_range, default=(5.0, 10.0),
                   help="break length, random MIN,MAX minutes (default 5,10; 0 = no breaks)")
    p.add_argument("--dry-run", action="store_true",
                   help="never press keys: you fish, the script logs when it WOULD reel in")

    g = p.add_argument_group("bite detection")
    g.add_argument("--confirm", choices=("visual", "both", "audio"), default="visual",
                   help="visual = burst of motion at OUR bobber (default); both = plus a splash; "
                        "audio = sound only")
    g.add_argument("--no-bobber", choices=("recast", "audio"), default="recast",
                   help="when the bobber is not found: reel in and recast (default) or use sound")
    g.add_argument("--threshold", type=float, default=-50.0, help="splash level floor, dBFS (default -50)")
    g.add_argument("--margin", type=float, default=12.0, help="dB above background (default 12)")
    g.add_argument("--motion-ratio", type=float, default=2.2, help="bite = motion this many times calm (default 2.2)")
    g.add_argument("--motion-min", type=float, default=0.8, help="...and at least this much above calm (default 0.8)")
    g.add_argument("--motion-cap", type=float, default=5.5,
                   help="a trigger peaking above this is something passing through the box, not a bite "
                        "(default 5.5; 112 real bites peaked at <= 4.51; 0 = off)")
    g.add_argument("--motion-warmup", type=float, default=2.5,
                   help="no bites in the first seconds of watching, while the bobber settles (default 2.5; "
                        "earliest real bite in 96 catches: 3.2 s)")
    g.add_argument("--confirm-time", type=float, default=0.2,
                   help="a bite must hold this long (default 0.2 s)")
    g.add_argument("--confirm-hold", type=float, default=0.6,
                   help="...with median motion >= this x threshold (default 0.6; real bites: >= 0.72, calm ~0.45)")
    g.add_argument("--confirm-max", type=float, default=2.0,
                   help="...and at most this x threshold: more means it kept growing, i.e. something "
                        "passing through (default 2.0; real bites <= 1.35)")
    g.add_argument("--burst-quiet", type=float, default=1.0,
                   help="no bites until this long after the last over-cap sample (default 1.0 s)")
    g.add_argument("--min-calm", type=float, default=0.3,
                   help="the bobber box must look like water: calm motion at least this (default 0.3)")
    g.add_argument("--max-calm", type=float, default=2.5,
                   help="...and at most this (default 2.5; someone walking there is ~4)")
    g.add_argument("--state", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fiska_state.json"),
                   help="where the learned landing zone is kept between sessions ('' = do not keep)")
    g.add_argument("--zone-min", type=float, default=0.08,
                   help="learned landing zone: at least +/- this fraction of the window height (default 0.08)")
    g.add_argument("--motion-window", type=float, default=0.1, help="seconds of motion the bite median spans (default 0.1)")

    g = p.add_argument_group("bobber search (fractions of the game window)")
    g.add_argument("--search", type=parse_frac, default=(0.05, 0.10, 0.85, 0.68),
                   help="where bobbers can land, x1,y1,x2,y2 (default 0.05,0.10,0.85,0.68)")
    g.add_argument("--exclude", type=parse_frac, default=(0.46, 0.52, 0.56, 1.0),
                   help="ignore this box: your character + cast bar (default 0.46,0.52,0.56,1.0)")
    g.add_argument("--bobber-size", type=float, default=0.02, help="bobber size vs window height (default 0.02)")
    g.add_argument("--red-weight", type=float, default=3.0,
                   help="weight of newly appeared red (the feather) in the search (default 3)")
    g.add_argument("--bobber-min", type=float, default=6.0, help="min search score to trust a match (default 6)")
    g.add_argument("--motion-box", type=float, default=3.0, help="watched box size in bobber sizes (default 3)")
    g.add_argument("--debug-dir", help="save one PNG per cast showing where the bobber was found")
    g.add_argument("--record", help="save audio/motion traces per cast (.npz) for tuning")

    g = p.add_argument_group("timing (seconds)")
    g.add_argument("--cast-wait", type=float, default=2.0, help="max wait for the Fishing channel to start")
    g.add_argument("--lure-timeout", type=float, default=20.0,
                   help="max wait for a lure/bait cast to finish (default 20)")
    g.add_argument("--grace", type=float, default=2.0, help="ignore audio this long after the cast")
    g.add_argument("--timeout", type=float, default=32.0, help="give up on a cast after this long")
    g.add_argument("--reaction-min", type=float, default=0.10, help="after the bite has held (default 0.10)")
    g.add_argument("--reaction-max", type=float, default=0.50)
    g.add_argument("--loot-min", type=float, default=1.5)
    g.add_argument("--loot-max", type=float, default=2.8)
    return p.parse_args(argv)


ERROR_ALREADY_EXISTS = 183


def single_instance(name="Local\\fiska-one-key-fishing"):
    """Hold a named mutex for the life of the process. Two fishers pressing the
    same key out of step reel in each other's casts ~2 s after every throw.
    Returns the handle, or None when another instance already holds it."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
    kernel32.CreateMutexW.restype = wt.HANDLE
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def main():
    args = parse_args()
    if args.list_devices:
        list_devices()
    elif args.calibrate:
        calibrate(args)
    else:
        lock = single_instance()
        if lock is None:
            log("another fiska.py is already running - stop it first (two of them reel in "
                "each other's casts)")
            sys.exit(1)
        Fisher(args).run()


if __name__ == "__main__":
    main()
