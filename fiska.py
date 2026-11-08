"""One-key fishing helper.

The in-game addon does everything on a single key (default F10): it applies a
new lure when the old one has run out, casts, and reels in the bobber. This
script presses that key and decides *when* to press it.

There is no game event for a bite, so the splash is the only cue. To avoid
reacting to other players' splashes, a bite is accepted only when signals
agree:

  * audio   - a loud transient on the game's audio output (WASAPI loopback),
  * vision  - the script finds OUR bobber on screen right after the cast
              (what appeared where nothing changed before) and requires a
              burst of motion in that small box at the same moment,
  * addon   - the FiskaSignal addon paints a status pixel in the top-left
              corner: "channeling Fishing" and "loot window opened". That
              tells us a cast really started (vs. a lure being applied), when
              the bobber is gone, and whether a reel-in actually caught
              something.

Every signal is optional: without the addon the script falls back to
timeouts, and if the bobber cannot be found it falls back to audio only.

Usage:
    python fiska.py --calibrate              # audio meter + addon status
    python fiska.py                          # fish (Pause key = start/pause)
    python fiska.py --debug-dir debug        # save bobber-search images
    python fiska.py --help
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import random
import sys
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

    def grab_gray(self, left, top, width, height):
        img = self.grab_bgra(left, top, width, height)[:: self.ds, :: self.ds].astype(np.float32)
        return 0.114 * img[..., 0] + 0.587 * img[..., 1] + 0.299 * img[..., 2]

    def bridge(self, box):
        """Read the FiskaSignal pixel. None if the addon is not visible.

        Blue = marker (addon loaded), red = channeling Fishing,
        green = loot window opened within the last ~1.5 s.
        """
        left, top, _, _ = box
        px = np.median(self.grab_bgra(left + 1, top + 1, 3, 3)[..., :3].reshape(-1, 3), axis=0)
        b, g, r = (int(v) for v in px)
        if b < 200 or (60 < r < 200) or (60 < g < 200):
            return None
        return {"fishing": r >= 200, "loot": g >= 200}


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


def locate_bobber(pre, post, k, exclude=None):
    """Find what appeared after the cast.

    pre/post: lists of gray frames from just before the key press and just
    after the bobber landed. Pixels that already flickered before the cast
    (water, particles) are discounted by their own pre-cast variability.
    exclude: boolean mask of pixels to ignore (character, cast bar, chat).

    Returns (cx, cy, score, score_map) in frame coordinates.
    """
    pre = np.stack(pre)
    post = np.stack(post)
    noise = pre.max(0) - pre.min(0)
    change = np.abs(post.mean(0) - pre.mean(0))
    score = np.clip(change - noise, 0, None)
    if exclude is not None:
        score[exclude] = 0
    smap = box_mean(score, k)
    y, x = np.unravel_index(int(np.argmax(smap)), smap.shape)
    return x + k // 2, y + k // 2, float(smap[y, x]), smap


def save_debug_png(path, gray, marks):
    """Gray frame -> PNG with a red square per (cx, cy, half_size)."""
    rgb = np.repeat(np.clip(gray, 0, 255).astype(np.uint8)[..., None], 3, axis=2)
    for cx, cy, half in marks:
        y1, y2 = max(0, cy - half), min(rgb.shape[0] - 1, cy + half)
        x1, x2 = max(0, cx - half), min(rgb.shape[1] - 1, cx + half)
        for yy in (y1, y2):
            rgb[yy, x1:x2 + 1] = (255, 0, 0)
        for xx in (x1, x2):
            rgb[y1:y2 + 1, xx] = (255, 0, 0)
    mss.tools.to_png(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), output=path)


class MotionTracker:
    """Frame-to-frame change inside the bobber box, with a rolling history."""

    def __init__(self, ratio, min_delta, keep_s=4.0):
        self.ratio = ratio
        self.min_delta = min_delta
        self.keep_s = keep_s
        self.samples = deque()
        self.prev = None

    def feed(self, t, frame):
        if self.prev is not None and self.prev.shape == frame.shape:
            self.samples.append((t, float(np.mean(np.abs(frame - self.prev)))))
            while self.samples and t - self.samples[0][0] > self.keep_s:
                self.samples.popleft()
        self.prev = frame

    def spike(self, t0, t1, guard=0.5):
        """Was there a motion burst in [t0, t1] compared with the calm before?

        Returns (verdict, peak, baseline); verdict None = not enough history.
        """
        before = [m for t, m in self.samples if t < t0 - guard]
        during = [m for t, m in self.samples if t0 - guard <= t <= t1]
        if len(before) < 5 or not during:
            return None, 0.0, 0.0
        base = float(np.median(before))
        peak = max(during)
        return peak >= max(base * self.ratio, base + self.min_delta), peak, base


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

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


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
        self.stats = {"caught": 0, "no_bite": 0, "no_loot": 0, "ignored": 0, "lure": 0}
        self.cast_no = 0
        if args.debug_dir:
            os.makedirs(args.debug_dir, exist_ok=True)

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

    def summary(self):
        s = self.stats
        return (f"caught {s['caught']}, no bite {s['no_bite']}, empty reel-ins "
                f"{s['no_loot']}, ignored splashes {s['ignored']}, lures {s['lure']}")

    # -- vision ------------------------------------------------------------
    def frames(self, region, n=3, gap=0.07):
        out = []
        for i in range(n):
            out.append(self.screen.grab_gray(*region))
            if i < n - 1:
                time.sleep(gap)
        return out

    def find_bobber(self, region, pre, post):
        """Returns the bobber box in screen coords, or None."""
        h, w = pre[0].shape
        k = max(5, int(self.args.bobber_size * h / (self.args.search[3] - self.args.search[1])))
        exclude = np.zeros((h, w), dtype=bool)
        sx1, sy1, sx2, sy2 = self.args.search
        ex1, ey1, ex2, ey2 = self.args.exclude
        # exclude box -> search-region frame coordinates
        fx = lambda v: int(round((v - sx1) / (sx2 - sx1) * w))
        fy = lambda v: int(round((v - sy1) / (sy2 - sy1) * h))
        exclude[max(0, fy(ey1)):max(0, fy(ey2)), max(0, fx(ex1)):max(0, fx(ex2))] = True

        cx, cy, score, _ = locate_bobber(pre, post, k, exclude)
        found = score >= self.args.bobber_min
        if self.args.debug_dir:
            path = os.path.join(self.args.debug_dir, f"cast{self.cast_no:04d}_{'found' if found else 'miss'}.png")
            save_debug_png(path, post[-1], [(cx, cy, k)])
        if not found:
            log(f"bobber not found (best score {score:.1f} < {self.args.bobber_min}) - audio only this cast")
            return None
        ds = self.screen.ds
        half = int(k * self.args.motion_box / 2) * ds
        left = region[0] + cx * ds - half
        top = region[1] + cy * ds - half
        log(f"bobber at ({left + half}, {top + half}) score {score:.1f}")
        return (left, top, 2 * half, 2 * half)

    # -- one cast ----------------------------------------------------------
    def wait_bridge(self, box, cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.screen.bridge(box)
            if state is not None and cond(state):
                return True
            time.sleep(0.03)
        return False

    def listen(self, box, has_bridge, bobber, cast_at):
        """Returns 'bite', 'timeout', 'ended' or 'interrupted'."""
        a = self.args
        motion = MotionTracker(a.motion_ratio, a.motion_min) if bobber else None
        pending = None  # (t, db, base) of an unconfirmed splash
        last_block = time.monotonic()
        while time.monotonic() - cast_at < a.timeout:
            if not self.ready():
                return "interrupted"
            now = time.monotonic()
            if has_bridge:
                state = self.screen.bridge(box)
                if state is not None and not state["fishing"]:
                    return "ended"
            if motion:
                motion.feed(now, self.screen.grab_gray(*bobber))
            blocks = self.listener.drain()
            if blocks:
                last_block = blocks[-1][0]
            elif now - last_block >= 0.02:
                # Loopback delivers nothing while the output is silent:
                # count one silent block per 20 ms of nothing.
                blocks = [(now, SILENCE_DB)]
                last_block = now
            if not blocks and not motion:
                time.sleep(0.01)
            for t, db in blocks:
                hit, base = self.splash.feed(db)
                if hit and pending is None:
                    pending = (t, db, base)

            if a.confirm == "visual" and motion:
                verdict, peak, base = motion.spike(now - 0.15, now)
                if verdict:
                    log(f"bite (motion {peak:.1f} vs {base:.1f})")
                    return "bite"
                continue

            if pending is None:
                continue
            t0, db, base = pending
            if a.confirm == "audio" or motion is None:
                log(f"bite  splash {db:.1f} dB (background {base:.1f} dB)")
                return "bite"
            if now - t0 < a.confirm_window:
                continue  # keep collecting motion around the splash
            verdict, peak, mbase = motion.spike(t0, now)
            if verdict is None:
                log(f"bite  splash {db:.1f} dB (too early for a motion baseline, trusting audio)")
                return "bite"
            if verdict:
                log(f"bite  splash {db:.1f} dB + motion {peak:.1f} (calm {mbase:.1f})")
                return "bite"
            self.stats["ignored"] += 1
            log(f"splash {db:.1f} dB ignored - our bobber did not move "
                f"(motion {peak:.1f}, calm {mbase:.1f})")
            pending = None
        return "timeout"

    def cast_once(self):
        a = self.args
        hwnd = self.wait_ready()
        box = client_box(hwnd)
        has_bridge = self.screen.bridge(box) is not None
        region = frac_box(box, a.search)
        use_vision = a.confirm != "audio"

        if a.dry_run:
            # You cast; we only watch. Keep the last few frames from before the cast.
            if not has_bridge:
                log("--dry-run needs the FiskaSignal addon (it tells us when you cast)")
                time.sleep(2.0)
                return
            recent = deque(maxlen=3)
            while True:
                if not self.ready():
                    return
                state = self.screen.bridge(box)
                if state and state["fishing"]:
                    break
                if use_vision:
                    recent.append(self.screen.grab_gray(*region))
                time.sleep(0.08)
            pre = list(recent) if len(recent) == 3 else None
            use_vision = use_vision and pre is not None
            self.cast_no += 1
            cast_at = time.monotonic()
            log("you cast (addon confirms Fishing)")
        else:
            pre = self.frames(region) if use_vision else None
            self.cast_no += 1
            press_key(self.key)
            cast_at = time.monotonic()

        if a.dry_run:
            pass
        elif has_bridge:
            if not self.wait_bridge(box, lambda s: s["fishing"], a.cast_wait):
                self.stats["lure"] += 1
                log("key pressed but no Fishing channel started (lure applied?) - pressing again")
                time.sleep(a.lure_wait + random.uniform(0.0, 0.6))
                return
            log("cast (addon confirms Fishing)")
        else:
            log("cast")

        time.sleep(max(0.0, a.grace - (time.monotonic() - cast_at)))
        bobber = self.find_bobber(region, pre, self.frames(region)) if use_vision else None
        self.listener.drain()
        self.splash.reset()

        result = self.listen(box, has_bridge, bobber, cast_at)
        if result == "interrupted":
            log("interrupted - recasting when resumed")
            return
        if result == "ended":
            self.stats["no_bite"] += 1
            log(f"Fishing channel ended without a bite  ({self.summary()})")
            time.sleep(random.uniform(0.3, 0.8))
            return
        if result == "timeout":
            self.stats["no_bite"] += 1
            log(f"no bite within {a.timeout:.0f}s  ({self.summary()})")
            state = self.screen.bridge(box) if has_bridge else None
            if state and state["fishing"] and not a.dry_run:
                press_key(self.key)  # still channeling: reel in so the next press casts
            time.sleep(random.uniform(0.5, 1.2))
            return

        if a.dry_run:
            log(">>> BITE - would press the key now. Reel in yourself; watching for loot.")
            if self.wait_bridge(box, lambda s: s["loot"] or not s["fishing"], 5.0):
                state = self.screen.bridge(box) or {}
                log("loot window seen - it was a real bite" if state.get("loot") or
                    self.wait_bridge(box, lambda s: s["loot"], 1.5) else "no loot seen")
            self.wait_bridge(box, lambda s: not s["fishing"], a.timeout)
            return

        time.sleep(random.uniform(a.reaction_min, a.reaction_max))
        if not self.ready():
            return
        press_key(self.key)
        if has_bridge:
            if self.wait_bridge(box, lambda s: s["loot"], 3.0):
                self.stats["caught"] += 1
                log(f"caught!  ({self.summary()})")
            else:
                self.stats["no_loot"] += 1
                log(f"reeled in but no loot window (false bite?)  ({self.summary()})")
        else:
            self.stats["caught"] += 1
            log(f"reeled in  ({self.summary()})")
        time.sleep(random.uniform(a.loot_min, a.loot_max))

    def run(self):
        a = self.args
        log(f"Audio: {self.listener.device['name']}")
        log(f"Key {a.key.upper()}, toggle {a.toggle.upper()}, confirm={a.confirm}, "
            f"threshold {a.threshold} dB, margin {a.margin} dB, window '{a.window}'")
        log("RUNNING" if self.running else f"PAUSED - press {a.toggle.upper()} to start")
        started = time.monotonic()
        bridge_logged = None
        try:
            while not (a.max_minutes and time.monotonic() - started > a.max_minutes * 60):
                hwnd = self.wait_ready()
                has_bridge = self.screen.bridge(client_box(hwnd)) is not None
                if has_bridge != bridge_logged:
                    log("FiskaSignal addon: " + ("detected" if has_bridge else
                        "NOT detected - using timeouts, no loot confirmation"))
                    bridge_logged = has_bridge
                self.cast_once()
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
    p.add_argument("--dry-run", action="store_true",
                   help="never press keys: you fish, the script logs when it WOULD reel in")

    g = p.add_argument_group("bite detection")
    g.add_argument("--confirm", choices=("both", "audio", "visual"), default="both",
                   help="both = splash must coincide with motion at our bobber (default)")
    g.add_argument("--threshold", type=float, default=-50.0, help="splash level floor, dBFS (default -50)")
    g.add_argument("--margin", type=float, default=12.0, help="dB above background (default 12)")
    g.add_argument("--confirm-window", type=float, default=0.35,
                   help="seconds after a splash to look for bobber motion (default 0.35)")
    g.add_argument("--motion-ratio", type=float, default=2.5, help="motion burst vs calm (default 2.5x)")
    g.add_argument("--motion-min", type=float, default=3.0, help="motion burst min increase (default 3.0)")

    g = p.add_argument_group("bobber search (fractions of the game window)")
    g.add_argument("--search", type=parse_frac, default=(0.05, 0.12, 0.85, 0.72),
                   help="where bobbers can land, x1,y1,x2,y2 (default 0.05,0.12,0.85,0.72)")
    g.add_argument("--exclude", type=parse_frac, default=(0.44, 0.47, 0.56, 1.0),
                   help="ignore this box: your character + cast bar (default 0.44,0.47,0.56,1.0)")
    g.add_argument("--bobber-size", type=float, default=0.02, help="bobber size vs window height (default 0.02)")
    g.add_argument("--bobber-min", type=float, default=6.0, help="min search score to trust a match (default 6)")
    g.add_argument("--motion-box", type=float, default=3.0, help="watched box size in bobber sizes (default 3)")
    g.add_argument("--debug-dir", help="save one PNG per cast showing where the bobber was found")

    g = p.add_argument_group("timing (seconds)")
    g.add_argument("--cast-wait", type=float, default=2.0, help="max wait for the Fishing channel to start")
    g.add_argument("--lure-wait", type=float, default=3.5,
                   help="wait after a press that did not start Fishing (lure cast time)")
    g.add_argument("--grace", type=float, default=2.0, help="ignore audio this long after the cast")
    g.add_argument("--timeout", type=float, default=32.0, help="give up on a cast after this long")
    g.add_argument("--reaction-min", type=float, default=0.25)
    g.add_argument("--reaction-max", type=float, default=0.70)
    g.add_argument("--loot-min", type=float, default=1.5)
    g.add_argument("--loot-max", type=float, default=2.8)
    return p.parse_args(argv)


def main():
    args = parse_args()
    if args.list_devices:
        list_devices()
    elif args.calibrate:
        calibrate(args)
    else:
        Fisher(args).run()


if __name__ == "__main__":
    main()
