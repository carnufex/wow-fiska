"""Offline tests for the detection logic (no game needed): python -m unittest"""

import ctypes
import threading
import unittest

import numpy as np

import fiska


def water(rng, h=120, w=200):
    """Gray frame with flickering 'water' noise."""
    return 90 + rng.normal(0, 6, (h, w)).astype(np.float32)


class LocateBobber(unittest.TestCase):
    def test_finds_new_object_not_water_or_character(self):
        rng = np.random.default_rng(1)
        pre = [water(rng) for _ in range(3)]
        post = [water(rng) for _ in range(3)]
        for f in post:
            f[40:48, 150:158] = 230          # our bobber appears at ~(154, 44)
            f[80:120, 80:120] = 20           # character changed pose (excluded)
        exclude = np.zeros_like(pre[0], dtype=bool)
        exclude[70:, 70:130] = True
        cx, cy, score, _ = fiska.locate_bobber(pre, post, k=7, exclude=exclude)
        self.assertLessEqual(abs(cx - 154), 3)
        self.assertLessEqual(abs(cy - 44), 3)
        self.assertGreater(score, 6.0)

    def test_nothing_new_gives_low_score(self):
        rng = np.random.default_rng(2)
        pre = [water(rng) for _ in range(3)]
        post = [water(rng) for _ in range(3)]
        _, _, score, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLess(score, 6.0)

    def test_moving_npc_loses_to_still_bobber(self):
        """A walking NPC changes a lot but keeps changing; the bobber sits still."""
        rng = np.random.default_rng(6)
        pre = [water(rng) for _ in range(3)]
        post = [water(rng) for _ in range(4)]
        for i, f in enumerate(post):
            f[60:90, 20 + 4 * i:40 + 4 * i] = 15     # NPC walking right across the frame
            f[30:37, 150:157] = 230                   # our bobber
        cx, cy, _, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLessEqual(abs(cx - 153), 3)
        self.assertLessEqual(abs(cy - 33), 3)

    def test_red_feather_beats_bigger_shifted_npc(self):
        """An NPC that shifted a bit between pre and post (then stood still) gives a
        bigger gray change than the small bobber; the newly appeared red wins."""
        rng = np.random.default_rng(8)

        def scene(npc_x, bobber):
            f = np.empty((120, 200, 3), np.float32)
            f[...] = (70, 60, 140)                                   # blue-purple water
            f += rng.normal(0, 4, f.shape).astype(np.float32)
            f[50:100, npc_x:npc_x + 25] = (40, 35, 60)               # dark NPC
            if bobber:
                f[30:34, 150:155] = (120, 75, 80)                    # dull red feather
                f[34:38, 151:154] = (190, 150, 140)                  # beige cork
            return f

        pre = [scene(20, False) for _ in range(3)]
        post = [scene(40, True) for _ in range(4)]                   # NPC moved 20 px
        cx_plain, _, _, _ = fiska.locate_bobber(pre, post, k=7, red_weight=0, ring_weight=0)
        cx, cy, _, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLess(cx_plain, 100, "sanity: plain change (no red, no ring) picks the NPC")
        self.assertLessEqual(abs(cx - 152), 4)
        self.assertLessEqual(abs(cy - 33), 5)

    def test_popup_window_loses_to_bobber(self):
        """A UI window popping up is a big change with red text and sharp edges;
        the bobber is small and alone on calm water."""
        rng = np.random.default_rng(9)

        def scene(panel, bobber):
            f = np.empty((240, 400, 3), np.float32)
            f[...] = (70, 60, 140)
            f += rng.normal(0, 4, f.shape).astype(np.float32)
            if panel:
                f[120:200, 20:180] = (200, 170, 60)
                f[140:148, 30:170] = (255, 40, 40)               # red text
            if bobber:
                f[60:64, 300:305] = (120, 75, 80)
                f[64:68, 301:304] = (190, 150, 140)
            return f

        pre = [scene(False, False) for _ in range(3)]
        post = [scene(True, True) for _ in range(4)]
        cx, cy, score, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLessEqual(abs(cx - 302), 4)
        self.assertLessEqual(abs(cy - 64), 5)
        self.assertGreater(score, 6.0)

    def test_existing_bobber_of_neighbour_is_ignored(self):
        """A bobber that was already there before our cast is not 'new'."""
        rng = np.random.default_rng(3)
        pre = [water(rng) for _ in range(3)]
        post = [water(rng) for _ in range(3)]
        for f in pre + post:
            f[10:18, 20:28] = 230            # neighbour's bobber, present all along
        for f in post:
            f[60:68, 100:108] = 230          # ours
        cx, cy, _, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLessEqual(abs(cx - 104), 3)
        self.assertLessEqual(abs(cy - 64), 3)


class Motion(unittest.TestCase):
    def feed(self, tracker, rng, t0, t1, amp, dt=0.0125):
        t = t0
        while t < t1:
            tracker.feed(t, 100 + rng.normal(0, amp, (30, 30)).astype(np.float32))
            t += dt
        return t - dt

    def test_burst_after_calm_is_a_bite(self):
        rng = np.random.default_rng(4)
        tr = fiska.MotionTracker(ratio=2.5, delta=1.0)
        self.feed(tr, rng, 0.0, 4.0, amp=0.5)
        t = self.feed(tr, rng, 4.0, 4.15, amp=3.0)
        hit, level, threshold, base = tr.check(t)
        self.assertTrue(hit, (level, threshold, base))

    def test_calm_water_and_single_flicker_are_not_a_bite(self):
        rng = np.random.default_rng(5)
        tr = fiska.MotionTracker(ratio=2.5, delta=1.0)
        t = self.feed(tr, rng, 0.0, 4.0, amp=0.5)
        tr.feed(t + 0.0125, 100 + rng.normal(0, 8, (30, 30)).astype(np.float32))  # one-frame flicker
        t = self.feed(tr, rng, t + 0.025, t + 0.06, amp=0.5)
        hit, *_ = tr.check(t)
        self.assertFalse(hit)

    def test_not_enough_history(self):
        tr = fiska.MotionTracker(ratio=2.5, delta=1.0)
        self.assertIsNone(tr.check(0.2))


class Splash(unittest.TestCase):
    def test_transient_over_background(self):
        d = fiska.SplashDetector(threshold_db=-35, margin_db=10)
        for _ in range(50):
            self.assertFalse(d.feed(-55.0)[0])
        self.assertTrue(d.feed(-25.0)[0])

    def test_no_hit_before_warmup(self):
        d = fiska.SplashDetector(threshold_db=-35, margin_db=10)
        self.assertFalse(d.feed(-20.0)[0])

    def test_loud_steady_music_does_not_trigger(self):
        d = fiska.SplashDetector(threshold_db=-35, margin_db=10)
        hits = [d.feed(-28.0 + (i % 3))[0] for i in range(100)]
        self.assertFalse(any(hits[10:]))


class Bridge(unittest.TestCase):
    def state(self, sq1, sq2=(255, 0, 0), h=768):
        """sq = (b, g, r) of each 6x6-unit square; h = game window height."""
        s = fiska.Screen.__new__(fiska.Screen)
        unit = max(1.0, h / 768.0)

        def grab(left, top, w, hh):
            strip = np.zeros((hh, w, 4), np.uint8)
            for x in range(w):
                strip[:, x, :3] = sq1 if x < 6 * unit else sq2
            return strip

        s.grab_bgra = grab
        return s.bridge((0, 0, 1000, h))

    def test_states(self):
        idle = {"fishing": False, "loot": False, "casting": False}
        self.assertEqual(self.state((255, 0, 0)), idle)
        self.assertEqual(self.state((255, 0, 255)), dict(idle, fishing=True))
        self.assertEqual(self.state((255, 255, 0)), dict(idle, loot=True))
        self.assertEqual(self.state((255, 0, 0), (255, 0, 255)), dict(idle, casting=True))
        self.assertEqual(self.state((255, 0, 255), h=1440), dict(idle, fishing=True))
        self.assertEqual(self.state((255, 0, 0), (255, 0, 255), h=1440), dict(idle, casting=True))
        self.assertIsNone(self.state((40, 50, 60)))       # game scene, no addon
        self.assertIsNone(self.state((255, 128, 0)))      # ambiguous colour

    def test_old_addon_without_second_square(self):
        self.assertFalse(self.state((255, 0, 255), (60, 70, 80))["casting"])


class Keys(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fiska.vk_from_name("F10"), 0x79)
        self.assertEqual(fiska.vk_from_name("pause"), 0x13)
        self.assertEqual(fiska.vk_from_name("e"), ord("E"))
        with self.assertRaises(ValueError):
            fiska.vk_from_name("nope")

    def test_input_struct_size(self):
        self.assertEqual(ctypes.sizeof(fiska.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


class FakeGame:
    """Simulated game: bobber appears on cast and bursts with motion on a bite.

    own_splash: the bite happens at OUR bobber (else only a neighbour's splash
    is heard). lure: the first key press applies a lure (a 1 s cast).
    dry=True: the 'player' casts by themself and reels in 0.6 s after the bite.
    """

    def __init__(self, own_splash, dry=False, lure=False):
        self.own_splash, self.dry, self.lure = own_splash, dry, lure
        self.fishing = False
        self.casting_until = 0.0
        self.loot_until = 0.0
        self.cast_at = None
        self.splash_at = None
        self.audio_sent = False
        self.idle_polls = 0
        self.presses = 0
        self.rng = np.random.default_rng(7)
        self.lock = threading.Lock()
        self.ds = 2

    def _cast(self, now):
        self.fishing, self.cast_at = True, now
        self.splash_at = now + 2.5

    def _reel(self, now):
        self.fishing = False
        if self.own_splash:
            self.loot_until = now + 1.5

    # fiska.press_key
    def press(self, vk):
        now = fiska.time.monotonic()
        self.presses += 1
        if self.lure and self.presses == 1:
            self.casting_until = now + 1.0
            return
        if self.fishing:
            self._reel(now)
        else:
            self._cast(now)

    # Screen API
    def bridge(self, box):
        now = fiska.time.monotonic()
        if self.dry:
            if self.cast_at is None:
                self.idle_polls += 1
                if self.idle_polls > 20:
                    self._cast(now)
            elif self.fishing and now > self.splash_at + 0.6:
                self._reel(now)
        return {"fishing": self.fishing, "loot": now < self.loot_until,
                "casting": now < self.casting_until}

    def grab_gray(self, left, top, w, h):
        now = fiska.time.monotonic()
        with self.lock:
            f = 90 + self.rng.normal(0, 1, (h // 2, w // 2)).astype(np.float32)
            if w <= 300 and self.own_splash and self.splash_at and 0 <= now - self.splash_at < 0.5:
                f += self.rng.normal(0, 20, f.shape).astype(np.float32)   # bite at our bobber
        if w > 300 and self.fishing and now - self.cast_at > 0.1:       # search region
            f[100:106, 50:56] = 230                                     # our bobber, upper-left
        fiska.time.sleep(0.005)
        return f

    def grab_rgb(self, left, top, w, h):
        return np.repeat(self.grab_gray(left, top, w, h)[..., None], 3, axis=2)

    # Listener API
    def drain(self):
        now = fiska.time.monotonic()
        if self.splash_at and now >= self.splash_at and not self.audio_sent:
            self.audio_sent = True
            return [(now, -20.0)]
        return [(now, -60.0)]


class FlowTest(unittest.TestCase):
    def run_casts(self, game, extra=(), casts=1):
        argv = ["--grace", "0.3", "--timeout", "5", "--loot-min", "0", "--loot-max", "0",
                "--reaction-min", "0", "--reaction-max", "0", "--bobber-min", "4", "--window", ""]
        args = fiska.parse_args(argv + list(extra))
        orig = (fiska.press_key, fiska.game_window, fiska.client_box, fiska.Screen, fiska.Listener)
        fiska.press_key = game.press
        fiska.game_window = lambda title: 1
        fiska.client_box = lambda hwnd: (0, 0, 1000, 1000)
        fiska.Screen = lambda *a, **k: game
        fiska.Listener = lambda device=None: game
        try:
            f = fiska.Fisher(args)
            for _ in range(casts):
                f.cast_once()
        finally:
            (fiska.press_key, fiska.game_window, fiska.client_box,
             fiska.Screen, fiska.Listener) = orig
        return f.stats

    def test_own_bite_is_caught(self):
        s = self.run_casts(FakeGame(own_splash=True))
        self.assertEqual((s["caught"], s["no_bite"]), (1, 0), s)

    def test_neighbour_splash_is_not_a_bite(self):
        s = self.run_casts(FakeGame(own_splash=False))
        self.assertEqual((s["caught"], s["no_bite"]), (0, 1), s)

    def test_lure_cast_is_waited_out(self):
        game = FakeGame(own_splash=True, lure=True)
        s = self.run_casts(game, casts=2)
        self.assertEqual((s["lure"], s["caught"]), (1, 1), s)
        self.assertEqual(game.presses, 3)   # lure, cast, reel in - nothing pressed during the lure

    def test_dry_run_scores_your_own_catch(self):
        s = self.run_casts(FakeGame(own_splash=True, dry=True), extra=["--dry-run"])
        self.assertEqual((s["dry_hit"], s["dry_missed"], s["dry_false"]), (1, 0, 0), s)


if __name__ == "__main__":
    unittest.main()


