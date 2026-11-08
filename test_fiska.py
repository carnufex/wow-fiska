"""Offline tests for the detection logic (no game needed): python -m unittest"""

import ctypes
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
    def feed(self, tracker, rng, t0, t1, amp, dt=0.05):
        t = t0
        while t < t1:
            tracker.feed(t, 100 + rng.normal(0, amp, (30, 30)).astype(np.float32))
            t += dt

    def test_burst_after_calm_is_a_spike(self):
        rng = np.random.default_rng(4)
        tr = fiska.MotionTracker(ratio=2.5, min_delta=3.0)
        self.feed(tr, rng, 0.0, 2.0, amp=1.0)
        self.feed(tr, rng, 2.0, 2.3, amp=25.0)
        verdict, peak, base = tr.spike(2.0, 2.3)
        self.assertTrue(verdict, (peak, base))

    def test_no_burst_is_not_a_spike(self):
        rng = np.random.default_rng(5)
        tr = fiska.MotionTracker(ratio=2.5, min_delta=3.0)
        self.feed(tr, rng, 0.0, 2.3, amp=1.0)
        verdict, _, _ = tr.spike(2.0, 2.3)
        self.assertFalse(verdict)

    def test_not_enough_history(self):
        tr = fiska.MotionTracker(ratio=2.5, min_delta=3.0)
        verdict, _, _ = tr.spike(0.1, 0.2)
        self.assertIsNone(verdict)


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
    def state(self, b, g, r):
        s = fiska.Screen.__new__(fiska.Screen)
        s.grab_bgra = lambda *a: np.tile(np.array([b, g, r, 255], np.uint8), (3, 3, 1))
        return s.bridge((0, 0, 100, 100))

    def test_states(self):
        self.assertEqual(self.state(255, 0, 0), {"fishing": False, "loot": False})
        self.assertEqual(self.state(255, 0, 255), {"fishing": True, "loot": False})
        self.assertEqual(self.state(255, 255, 0), {"fishing": False, "loot": True})
        self.assertIsNone(self.state(40, 50, 60))      # game scene, no addon
        self.assertIsNone(self.state(255, 128, 0))     # ambiguous colour


class Keys(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fiska.vk_from_name("F10"), 0x79)
        self.assertEqual(fiska.vk_from_name("pause"), 0x13)
        self.assertEqual(fiska.vk_from_name("e"), ord("E"))
        with self.assertRaises(ValueError):
            fiska.vk_from_name("nope")

    def test_input_struct_size(self):
        self.assertEqual(ctypes.sizeof(fiska.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


if __name__ == "__main__":
    unittest.main()


class FakeGame:
    """Simulated game: bobber appears on cast, splash (audio + optional motion) later."""

    def __init__(self, own_splash):
        self.own_splash = own_splash
        self.fishing = False
        self.loot_until = 0.0
        self.cast_at = None
        self.splash_at = None
        self.audio_sent = False
        self.rng = np.random.default_rng(7)
        self.ds = 2

    # fiska.press_key
    def press(self, vk):
        now = fiska.time.monotonic()
        if not self.fishing:
            self.fishing, self.cast_at = True, now
            self.splash_at = now + 1.2
        else:
            self.fishing = False
            if self.own_splash:
                self.loot_until = now + 1.5

    # Screen API
    def bridge(self, box):
        return {"fishing": self.fishing, "loot": fiska.time.monotonic() < self.loot_until}

    def grab_gray(self, left, top, w, h):
        now = fiska.time.monotonic()
        h2, w2 = h // 2, w // 2
        f = 90 + self.rng.normal(0, 2, (h2, w2)).astype(np.float32)
        if w > 300 and self.fishing and now - self.cast_at > 0.1:   # search region
            f[100:106, 50:56] = 230                                 # our bobber, upper-left
        if w <= 300 and self.own_splash and self.splash_at and 0 <= now - self.splash_at < 0.3:
            f += self.rng.normal(0, 40, f.shape).astype(np.float32)  # splash at our bobber
        return f

    # Listener API
    def drain(self):
        now = fiska.time.monotonic()
        if self.splash_at and now >= self.splash_at and not self.audio_sent:
            self.audio_sent = True
            return [(now, -20.0)]
        return [(now, -60.0)]


class FlowTest(unittest.TestCase):
    def run_cast(self, own_splash):
        game = FakeGame(own_splash)
        args = fiska.parse_args(["--grace", "0.3", "--timeout", "3", "--loot-min", "0",
                                 "--loot-max", "0", "--reaction-min", "0", "--reaction-max", "0",
                                 "--bobber-min", "4", "--window", ""])
        f = fiska.Fisher.__new__(fiska.Fisher)
        f.args, f.key, f.toggle = args, 0x79, type("T", (), {"pressed": lambda self: False})()
        f.screen, f.listener = game, game
        f.splash = fiska.SplashDetector(args.threshold, args.margin)
        f.running, f.waiting_logged, f.cast_no = True, False, 0
        f.stats = {"caught": 0, "no_bite": 0, "no_loot": 0, "ignored": 0, "lure": 0}
        orig = (fiska.press_key, fiska.game_window, fiska.client_box)
        fiska.press_key = game.press
        fiska.game_window = lambda title: 1
        fiska.client_box = lambda hwnd: (0, 0, 1000, 1000)
        try:
            f.cast_once()
        finally:
            fiska.press_key, fiska.game_window, fiska.client_box = orig
        return f.stats

    def test_own_splash_is_caught(self):
        s = self.run_cast(own_splash=True)
        self.assertEqual(s["caught"], 1, s)
        self.assertEqual(s["ignored"], 0, s)

    def test_neighbour_splash_is_ignored(self):
        s = self.run_cast(own_splash=False)
        self.assertEqual(s["caught"], 0, s)
        self.assertEqual(s["ignored"], 1, s)
        self.assertEqual(s["no_bite"], 1, s)
