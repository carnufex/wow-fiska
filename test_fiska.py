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

    def test_animated_portrait_at_frame_edge_is_ignored(self):
        """A party frame's portrait cut by the frame edge looks small and calm
        (its ring is partly outside); it must not beat the bobber."""
        rng = np.random.default_rng(10)
        pre = [water(rng, 240, 400) for _ in range(3)]
        post = [water(rng, 240, 400) for _ in range(4)]
        for f in post:
            f[232:240, 300:316] = 250            # portrait poking in at the bottom edge
            f[80:86, 150:156] = 200              # our bobber, dimmer
        cx, cy, _, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertLessEqual(abs(cx - 153), 3)
        self.assertLessEqual(abs(cy - 83), 3)

    def test_landing_zone_beats_bigger_change_outside_it(self):
        """A player who walked onto the dock and stopped is 'new and still' and
        bigger than the bobber; outside the learned landing zone it is ignored."""
        rng = np.random.default_rng(13)
        pre = [water(rng, 240, 400) for _ in range(3)]
        post = [water(rng, 240, 400) for _ in range(4)]
        for f in post:
            f[150:157, 300:307] = 255           # player standing on the dock: compact, strong change
            f[60:66, 100:106] = 200             # our bobber
        cx, _, _, _ = fiska.locate_bobber(pre, post, k=7)
        self.assertGreater(cx, 250, "sanity: without a zone the player wins")
        zone = np.zeros((240, 400), bool)
        zone[30:100, 60:150] = True
        cx, cy, _, _ = fiska.locate_bobber(pre, post, k=7, allowed=zone)
        self.assertLessEqual(abs(cx - 103), 3)
        self.assertLessEqual(abs(cy - 63), 3)

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
        t = self.feed(tr, rng, 4.0, 4.15, amp=2.0)
        verdict, level, threshold, base, peak = tr.check(t)
        self.assertEqual(verdict, "bite", (level, threshold, base, peak))

    def test_burst_over_cap_is_not_a_bite(self):
        """Something passing through the box (a pet, a nameplate) moves far
        more than a bobber ever does."""
        rng = np.random.default_rng(11)
        tr = fiska.MotionTracker(ratio=2.2, delta=0.8, cap=4.5)
        self.feed(tr, rng, 0.0, 4.0, amp=0.5)
        t = self.feed(tr, rng, 4.0, 4.15, amp=8.0)
        self.assertEqual(tr.check(t)[0], "burst")

    def test_no_bite_during_warmup(self):
        rng = np.random.default_rng(12)
        tr = fiska.MotionTracker(ratio=2.2, delta=0.8, warmup=1.5, base_s=0.5, gap=0.1)
        self.feed(tr, rng, 0.0, 1.0, amp=0.5)
        t = self.feed(tr, rng, 1.0, 1.15, amp=2.0)    # settling bobber, 1.15 s in
        self.assertIsNone(tr.check(t))

    def test_calm_water_and_single_flicker_are_not_a_bite(self):
        rng = np.random.default_rng(5)
        tr = fiska.MotionTracker(ratio=2.5, delta=1.0)
        t = self.feed(tr, rng, 0.0, 4.0, amp=0.5)
        tr.feed(t + 0.0125, 100 + rng.normal(0, 8, (30, 30)).astype(np.float32))  # one-frame flicker
        t = self.feed(tr, rng, t + 0.025, t + 0.06, amp=0.5)
        self.assertIsNone(tr.check(t)[0])

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
        idle = {"fishing": False, "loot": False, "casting": False, "v11": True}
        self.assertEqual(self.state((255, 0, 0)), idle)
        self.assertEqual(self.state((255, 0, 255)), dict(idle, fishing=True))
        self.assertEqual(self.state((255, 255, 0)), dict(idle, loot=True))
        self.assertEqual(self.state((255, 0, 0), (255, 0, 255)), dict(idle, casting=True))
        self.assertEqual(self.state((255, 0, 255), h=1440), dict(idle, fishing=True))
        self.assertEqual(self.state((255, 0, 0), (255, 0, 255), h=1440), dict(idle, casting=True))
        self.assertIsNone(self.state((40, 50, 60)))       # game scene, no addon
        self.assertIsNone(self.state((255, 128, 0)))      # ambiguous colour

    def test_old_addon_without_second_square(self):
        state = self.state((255, 0, 255), (60, 70, 80))
        self.assertFalse(state["casting"])
        self.assertFalse(state["v11"])


class Keys(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fiska.vk_from_name("F10"), 0x79)
        self.assertEqual(fiska.vk_from_name("pause"), 0x13)
        self.assertEqual(fiska.vk_from_name("e"), ord("E"))
        with self.assertRaises(ValueError):
            fiska.vk_from_name("nope")

    def test_landing_zone_survives_a_restart_for_the_same_window(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            args = fiska.parse_args(["--state", path])
            first = fiska.Fisher.__new__(fiska.Fisher)
            first.args, first.landings, first.window_size = args, fiska.deque(maxlen=20), None
            first.load_landings((0, 0, 2560, 1440))
            for i in range(6):
                first.add_landing(1250 + i, 500)
            again = fiska.Fisher.__new__(fiska.Fisher)
            again.args, again.landings, again.window_size = args, fiska.deque(maxlen=20), None
            again.load_landings((0, 0, 2560, 1440))
            self.assertEqual(len(again.landings), 6)
            other = fiska.Fisher.__new__(fiska.Fisher)
            other.args, other.landings, other.window_size = args, fiska.deque(maxlen=20), None
            other.load_landings((0, 0, 1920, 1080))          # different window: not used
            self.assertEqual(len(other.landings), 0)

    def test_second_instance_is_refused(self):
        name = "Local\\fiska-test-%d" % id(self)
        first = fiska.single_instance(name)
        self.assertIsNotNone(first)
        self.assertIsNone(fiska.single_instance(name))

    def test_input_struct_size(self):
        self.assertEqual(ctypes.sizeof(fiska.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def bare_fisher(argv=()):
    """A Fisher without screen/audio, for the search and zone logic."""
    f = fiska.Fisher.__new__(fiska.Fisher)
    f.args = fiska.parse_args(["--state", ""] + list(argv))
    f.landings, f.window_size, f.cur, f.cast_no = fiska.deque(maxlen=20), None, {}, 0
    f.prior_off = False
    f.screen = type("S", (), {"ds": 2})()
    return f


class NewSpot(unittest.TestCase):
    """A new fishing spot: zoomed-out camera, rain, a zone learned elsewhere."""

    def scene(self, rng, bobber=None, h=418, w=1024):
        f = 90 + rng.normal(0, 2, (h, w)).astype(np.float32)          # dark, calm water
        for _ in range(25):                                            # rain streaks
            x, y = rng.integers(0, w), rng.integers(0, h - 12)
            f[y:y + 12, x] = 140
        if bobber:
            x, y = bobber
            f[y - 1:y + 2, x - 1:x + 2] = 200                         # a small, far bobber
        return f

    def test_small_far_bobber_in_the_rain_is_found(self):
        rng = np.random.default_rng(3)
        f = bare_fisher()
        pre = [self.scene(rng) for _ in range(3)]
        post = [self.scene(rng, (530, 234)) for _ in range(4)]
        cx, cy, score, k = f.search_spot(pre, post, None)
        self.assertLessEqual(abs(cx - 530), 3)
        self.assertLessEqual(abs(cy - 234), 3)
        self.assertGreaterEqual(score, f.args.bobber_min)
        self.assertLess(k, 14)                                         # the half size won
        _, _, single, _ = bare_fisher(["--bobber-scales", "1"]).search_spot(pre, post, None)
        self.assertLess(single, f.args.bobber_min)                     # one size alone: not found

    def test_small_bobber_is_still_watched_with_a_full_size_box(self):
        rng = np.random.default_rng(3)
        f = bare_fisher()
        orig = fiska.log
        fiska.log = lambda m: None
        try:
            pre = [self.scene(rng) for _ in range(3)]
            post = [self.scene(rng, (530, 234)) for _ in range(4)]
            box = f.find_bobber((128, 144, 2048, 835), pre, post)
        finally:
            fiska.log = orig
        self.assertEqual(f.cur["bobber_k"], 7)                     # found at half size...
        self.assertEqual(box[2], 2 * int(14 * 3 / 2) * 2)          # ...watched at full size

    def test_head_just_above_the_excluded_character_does_not_win(self):
        rng = np.random.default_rng(4)
        f = bare_fisher()
        pre = [self.scene(rng) for _ in range(3)]
        post = [self.scene(rng, (300, 150)) for _ in range(4)]
        for p in post:
            p[285:302, 575:600] = 30                                   # head, just above the box
            p[302:418, 540:640] = 40                                   # body (excluded)
        cx, cy, _, _ = f.search_spot(pre, post, None)
        self.assertLessEqual(abs(cx - 300), 3, (cx, cy))
        self.assertLessEqual(abs(cy - 150), 3, (cx, cy))

    def test_bobber_still_in_the_air_is_looked_at_again(self):
        rng = np.random.default_rng(5)
        f = bare_fisher()
        lines = []
        orig = fiska.log
        fiska.log = lines.append
        try:
            pre = [self.scene(rng) for _ in range(3)]
            flying = [self.scene(rng, (400, 60 + 25 * i)) for i in range(4)]     # still falling
            landed = lambda: [self.scene(rng, (420, 200)) for _ in range(3)]
            box = f.find_bobber((128, 144, 2048, 835), pre, flying, more_post=landed)
        finally:
            fiska.log = orig
        cx = (box[0] + box[2] // 2 - 128) // 2
        cy = (box[1] + box[3] // 2 - 144) // 2
        self.assertLessEqual(abs(cx - 420), 3, lines)
        self.assertLessEqual(abs(cy - 200), 3, lines)

    def test_usual_area_beats_an_npc_off_to_the_side(self):
        """Before any catch: an idling NPC on the dock (bigger change) loses to the
        bobber straight ahead, because the usual area is searched first."""
        rng = np.random.default_rng(6)
        f = bare_fisher()
        f.window_size = [2560, 1440]
        region = fiska.frac_box((0, 0, 2560, 1440), f.args.search)
        pre = [self.scene(rng) for _ in range(3)]
        post = [self.scene(rng, (575, 200)) for _ in range(4)]      # screen (1278, 544)
        for p in pre:
            p[160:215, 180:200] = 60                                  # NPC, one pose
        for p in post:
            p[160:215, 184:204] = 45                                  # ...shifted and darker
        mask, where = f.landing_zone(region, pre[0].shape)
        self.assertEqual(where, "usual area")
        cx, cy, _, _ = f.search_spot(pre, post, mask)
        self.assertLessEqual(abs(cx - 575), 3)
        self.assertLessEqual(abs(cy - 200), 3)
        _, where = bare_fisher(["--prior", "off"]).landing_zone(region, pre[0].shape)
        self.assertIsNone(where)

    def test_one_lucky_catch_on_an_npc_does_not_stretch_the_zone(self):
        f = bare_fisher()
        f.window_size = [2560, 1440]
        region = fiska.frac_box((0, 0, 2560, 1440), f.args.search)
        rng = np.random.default_rng(1)
        f.landings.extend((1300 + rng.normal(0, 25), 540 + rng.normal(0, 25)) for _ in range(9))
        f.landings.append((494.0, 538.0))                             # the NPC "catch"
        mask, where = f.landing_zone(region, (418, 1024))
        self.assertEqual(where, "landing zone")
        self.assertTrue(mask[(540 - 144) // 2, (1300 - 128) // 2])
        self.assertFalse(mask[(538 - 144) // 2, (494 - 128) // 2])

    def test_zone_from_another_spot_is_forgotten_after_three_misses(self):
        f = bare_fisher()
        f.landings.extend([(1250.0, 500.0)] * 10)
        orig = fiska.log
        fiska.log = lambda m: None
        try:
            for outcome in ("no_bite", "not_water", "caught", "no_bite", "no_bite"):
                f.cur = {"search": "landing zone"}
                f.check_zone(outcome)
            self.assertEqual(len(f.landings), 10)          # a catch in between resets the count
            f.cur = {"search": "everywhere"}
            f.check_zone("no_bite")                          # not in the zone: doesn't count
            self.assertEqual(len(f.landings), 10)
            f.cur = {"search": "landing zone"}
            f.check_zone("empty")
            self.assertEqual(len(f.landings), 0)
        finally:
            fiska.log = orig


class Shift(unittest.TestCase):
    def frames(self, rng, n, dy=0, specks=False):
        out = []
        for i in range(n):
            f = 90 + rng.normal(0, 2, (42, 42)).astype(np.float32)
            f[18 + dy:25 + dy, 18:24] = 180                       # bobber
            f[14 + dy:18 + dy, 15:20] = 60                        # feather
            if specks:                                             # glitter passing through
                x = (i * 5) % 36
                f[8:10, x:x + 5] = 255
                f[30:32, (x + 17) % 36:(x + 17) % 36 + 5] = 255
            out.append(f)
        return out

    def test_bite_moves_the_bobber(self):
        rng = np.random.default_rng(1)
        shift = fiska.bobber_shift(self.frames(rng, 30), self.frames(rng, 12, dy=4))
        self.assertGreaterEqual(shift, 3.0)

    def test_glitter_leaves_it_still(self):
        rng = np.random.default_rng(2)
        shift = fiska.bobber_shift(self.frames(rng, 30), self.frames(rng, 12, specks=True))
        self.assertLess(shift, 1.5)

    def test_too_few_frames(self):
        rng = np.random.default_rng(3)
        self.assertIsNone(fiska.bobber_shift(self.frames(rng, 3), self.frames(rng, 12)))

    def test_a_bite_moves_the_fine_structure_too(self):
        rng = np.random.default_rng(4)
        fine, texture = fiska.bite_texture(self.frames(rng, 30), self.frames(rng, 12, dy=4))
        self.assertGreaterEqual(fine, 1.0)
        self.assertLessEqual(texture, 1.4)

    def test_a_sun_streak_sliding_over_a_still_bobber(self):
        rng = np.random.default_rng(5)
        after = self.frames(rng, 12)
        yy = np.arange(42, dtype=np.float32)[:, None]
        for i, f in enumerate(after):                              # broad bright band moving down
            f += 70 * np.exp(-((yy - (10 + 3 * i)) / 8.0) ** 2)
        fine, _ = fiska.bite_texture(self.frames(rng, 30), after)
        self.assertLess(fine, 1.0)

    def test_sparkles_over_a_still_bobber(self):
        rng = np.random.default_rng(6)
        after = self.frames(rng, 12)
        for f in after:
            for _ in range(6):
                y, x = rng.integers(11, 30), rng.integers(11, 30)
                f[y:y + 2, x:x + 3] = 255
        _, texture = fiska.bite_texture(self.frames(rng, 30), after)
        self.assertGreater(texture, 1.4)


class BiteModelTest(unittest.TestCase):
    def test_fit_save_load_predict(self):
        import json, os, tempfile
        import train_bite
        rng = np.random.default_rng(0)
        names = list(fiska.BITE_FEATURES)
        X = rng.normal(0, 1, (300, len(names)))
        y = (X[:, 0] - X[:, 2] > 0).astype(float)          # real bites: moved, not sparkly
        mean, std = train_bite.standardise(X)
        w, b = train_bite.fit((X - mean) / std, y)
        model = {"features": names, "mean": mean.tolist(), "std": std.tolist(),
                 "weights": w.tolist(), "bias": float(b), "threshold": 0.5}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            with open(path, "w") as fh:
                json.dump(model, fh)
            m = fiska.BiteModel(path)
        bite = dict(zip(names, [2.0, 0, -2.0] + [0] * (len(names) - 3)))
        glitter = dict(zip(names, [-2.0, 0, 2.0] + [0] * (len(names) - 3)))
        self.assertGreater(m.probability(bite), 0.9)
        self.assertLess(m.probability(glitter), 0.1)

    def synthetic(self, sessions, noise=0.0, seed=0, flip=False):
        """[(session, cast, label, features)]: real bites move (shift) and are not sparkly."""
        rng = np.random.default_rng(seed)
        out = []
        for s in sessions:
            for i in range(60):
                lab = 1 if i % 4 else 0
                f = {k: float(rng.normal(0, 1)) for k in fiska.BITE_FEATURES}
                sign = (1 if lab else -1) * (-1 if flip else 1)
                f["shift"] += 1.5 * sign + rng.normal(0, noise)
                f["texture"] -= 1.5 * sign
                out.append((s, str(i), lab, f))
        return out

    def run_learn(self, data, models, **kw):
        import train_bite
        orig = train_bite.collect
        train_bite.collect = lambda rec, a, log=None: data
        lines = []
        try:
            return train_bite.learn("unused", models=models, say=lines.append, **kw), lines
        finally:
            train_bite.collect = orig

    def test_learning_promotes_keeps_and_skips(self):
        import json, os, tempfile
        import train_bite
        with tempfile.TemporaryDirectory() as models:
            old = self.synthetic(["a", "b", "c"])
            verdict, _ = self.run_learn(old, models, force=True)
            self.assertEqual(verdict, "promoted")
            first = json.load(open(os.path.join(models, "current.json")))
            self.assertEqual(first["sessions"], ["a", "b", "c"])
            verdict, _ = self.run_learn(old, models)
            self.assertEqual(verdict, "nothing new")
            # a new session with mislabelled bites: the new model gets worse on it -> kept
            bad = old + self.synthetic(["d"], seed=5, flip=True)
            verdict, lines = self.run_learn(bad, models)
            self.assertEqual(verdict, "kept", lines)
            # a new session like the others -> promoted, history kept
            good = old + self.synthetic(["e"], seed=6)
            verdict, lines = self.run_learn(good, models)
            self.assertEqual(verdict, "promoted", lines)
            self.assertEqual(len([n for n in os.listdir(models) if n.startswith("bite_model-")]) >= 1, True)
            self.assertIn("e", json.load(open(os.path.join(models, "current.json")))["sessions"])
            self.assertEqual(train_bite.current_model_path(models), os.path.join(models, "current.json"))

    def test_slim_keeps_what_the_bite_model_needs(self):
        import os, tempfile, time
        import train_bite
        with tempfile.TemporaryDirectory() as rec:
            for i, name in enumerate(("old", "new")):
                d = os.path.join(rec, name)
                os.makedirs(d)
                open(os.path.join(d, "casts.csv"), "w").write("time,cast,outcome\n")
                path = os.path.join(d, "cast0001.npz")
                np.savez(path, pre=np.zeros((3, 50, 50, 3), np.uint8), post=np.zeros((4, 50, 50, 3), np.uint8),
                         motion=np.ones((40, 2)), clip0_kind=np.array("bite"),
                         clip0_frames=np.zeros((10, 8, 8), np.uint8))
                t = time.time() - (1000 if name == "old" else 0)
                os.utime(path, (t, t))
                os.utime(d, (t, t))
            train_bite.slim(rec, keep_sessions=1, say=lambda m: None)
            with np.load(os.path.join(rec, "old", "cast0001.npz")) as old:
                self.assertNotIn("pre", old)
                self.assertIn("clip0_frames", old)
                self.assertIn("motion", old)
            with np.load(os.path.join(rec, "new", "cast0001.npz")) as new:
                self.assertIn("pre", new)

    def test_one_big_session_does_not_outweigh_the_rest(self):
        import train_bite
        sessions = np.array(["darnassus"] * 400 + ["dock"] * 40)
        y = np.array([1.0] * 380 + [0.0] * 20 + [1.0] * 30 + [0.0] * 10)
        w = train_bite.session_weights(sessions, y, cap=60)
        self.assertAlmostEqual(w[:380].sum(), 60)                  # 380 real bites count as 60
        self.assertAlmostEqual(w[380:400].sum(), 20)                # under the cap: unchanged
        self.assertAlmostEqual(w[400:].sum(), 40)

    def test_the_shipped_model_loads(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(fiska.__file__)), "bite_model.json")
        m = fiska.BiteModel(path)
        self.assertEqual(m.features, list(fiska.BITE_FEATURES))
        self.assertTrue(0 < m.threshold < 1)


class Console(unittest.TestCase):
    def test_key_names_round_trip(self):
        for name in ("pause", "f5", "f12", "a", "7", "insert"):
            self.assertEqual(fiska.name_from_vk(fiska.vk_from_name(name)), name)
        self.assertIsNone(fiska.name_from_vk(0x01))                      # mouse button

    def test_overlay_shows_the_areas_in_screen_coordinates(self):
        f = bare_fisher()
        f.watch_box = None
        box = (100, 50, 2560, 1440)
        shapes = {label: rect for label, rect, _ in f.overlay_shapes(box)}
        x, y, w, h = shapes["search area"]
        self.assertAlmostEqual(x, 100 + 0.05 * 2560)
        self.assertAlmostEqual(y, 50 + 0.10 * 1440)
        self.assertAlmostEqual(w, 0.80 * 2560)
        self.assertIn("usual landing area", shapes)
        self.assertIn("character (ignored)", shapes)
        f.landings.extend([(1300.0, 540.0)] * 6)
        f.watch_box = (1258, 498, 84, 84)
        labels = [label for label, _, _ in f.overlay_shapes(box)]
        self.assertIn("landing zone (6 catches)", labels)
        self.assertNotIn("usual landing area", labels)
        self.assertIn("watching", labels)

    def test_stop_ends_the_loop(self):
        f = bare_fisher()
        f.quit = True
        with self.assertRaises(fiska.Stop):
            f.ready()


class FrameEditing(unittest.TestCase):
    """Dragging the frames on the overlay (no window: the handlers are driven
    with fake mouse events)."""

    def setUp(self):
        import fiska_gui
        self.gui = fiska_gui
        self.saved = []
        fiska_gui.save_settings = lambda s: self.saved.append(dict(s))
        app = fiska_gui.App.__new__(fiska_gui.App)
        app.settings, app.argv, app.editing = {"toggle": "pause", "overlay": True}, [], True
        app.args = app.make_args()
        app.fisher = bare_fisher()
        app.fisher.args = app.args
        self.app = app
        self.overlay = fiska_gui.Overlay(app)
        self.overlay.box = (0, 0, 2560, 1440)
        self.log = fiska.log
        fiska.log = lambda m: None

    def tearDown(self):
        fiska.log = self.log
        import importlib
        importlib.reload(self.gui)                                  # undo the save_settings stub

    def ev(self, x, y):
        return type("E", (), {"x": x, "y": y})()

    def test_hit_test_finds_edges_corners_and_inside(self):
        frames = {"prior": (0.4, 0.3, 0.6, 0.5), "search": (0.05, 0.1, 0.85, 0.68),
                  "exclude": (0.46, 0.52, 0.56, 1.0)}
        W, H = 1000, 1000
        self.assertEqual(self.gui.hit_test(frames, 400, 400, W, H), ("prior", {"l"}))
        self.assertEqual(self.gui.hit_test(frames, 600, 300, W, H), ("prior", {"r", "t"}))
        self.assertEqual(self.gui.hit_test(frames, 500, 400, W, H), ("prior", {"move"}))
        self.assertEqual(self.gui.hit_test(frames, 200, 400, W, H), ("search", {"move"}))
        self.assertEqual(self.gui.hit_test(frames, 52, 400, W, H), ("search", {"l"}))
        self.assertIsNone(self.gui.hit_test(frames, 950, 950, W, H))

    def test_drag_stays_inside_and_keeps_a_minimum_size(self):
        self.assertEqual(self.gui.drag((0.4, 0.3, 0.6, 0.5), {"move"}, 0.9, 0.0)[2], 1.0)
        x1, _, x2, _ = self.gui.drag((0.4, 0.3, 0.6, 0.5), {"l"}, 0.5, 0.0)
        self.assertAlmostEqual(x2 - x1, self.gui.MIN_FRAC)

    def test_dragging_the_usual_area_moves_it_for_the_fisher_and_saves_it(self):
        f = self.app.fisher
        f.landings.extend([(1300.0, 540.0)] * 6)
        f.prior_off = True
        x1, y1 = self.app.args.prior[0] * 2560, self.app.args.prior[1] * 1440
        self.overlay.on_press(self.ev(x1 + 100, y1 + 100))        # inside: move
        self.overlay.on_drag(self.ev(x1 + 100, y1 + 100 + 144))   # 10 % down
        self.overlay.on_release(self.ev(x1 + 100, y1 + 100 + 144))
        self.assertAlmostEqual(self.app.args.prior[1], 0.34, places=3)
        self.assertIs(f.args, self.app.args)                       # the fisher sees it
        self.assertEqual(self.saved[-1]["prior"], list(self.app.args.prior))
        self.assertEqual(len(f.landings), 0)                       # old zone forgotten
        self.assertFalse(f.prior_off)
        self.assertTrue(self.app.args.prior_locked)

    def test_a_placed_usual_area_is_not_switched_off_after_misses(self):
        f = self.app.fisher
        self.app.args.prior_locked = True
        for _ in range(3):
            f.cur = {"search": "usual area"}
            f.check_zone("no_bite")
        self.assertFalse(f.prior_off)

    def test_calm_water_at_night_does_not_switch_the_usual_area_off(self):
        f = self.app.fisher
        self.app.args.prior_locked = False
        for _ in range(5):
            f.cur = {"search": "usual area"}
            f.check_zone("not_water")
        self.assertFalse(f.prior_off)
        for _ in range(3):
            f.cur = {"search": "usual area"}
            f.check_zone("no_bite")
        self.assertTrue(f.prior_off)                               # real misses still do

    def test_saved_frames_are_used_on_the_next_start(self):
        self.app.settings["search"] = [0.1, 0.1, 0.9, 0.7]
        self.assertEqual(self.app.make_args().search, (0.1, 0.1, 0.9, 0.7))


class BreaksTest(unittest.TestCase):
    def make(self, work=(20, 30), rest=(5, 10)):
        return fiska.Breaks(work, rest, rand=lambda lo, hi: (lo + hi) / 2)   # 25 min / 7.5 min

    def test_due_after_the_work_period(self):
        b = self.make()
        b.add_work(24 * 60)
        self.assertFalse(b.due())
        b.add_work(60)
        self.assertTrue(b.due())
        self.assertEqual(b.take(), 7.5 * 60)
        b.reset()
        self.assertFalse(b.due())

    def test_long_user_pause_counts_as_the_break(self):
        b = self.make()
        b.add_work(24 * 60)
        b.user_paused(60)                 # a minute away: still due soon
        b.add_work(60)
        self.assertTrue(b.due())
        b.user_paused(6 * 60)             # longer than the shortest break
        self.assertFalse(b.due())

    def test_zero_turns_breaks_off(self):
        b = self.make(work=(0, 0))
        b.add_work(10 * 3600)
        self.assertFalse(b.due())

    def test_ranges_parse(self):
        self.assertEqual(fiska.parse_args([]).work_minutes, (20.0, 30.0))
        self.assertEqual(fiska.parse_args([]).break_minutes, (5.0, 10.0))
        self.assertEqual(fiska.parse_args(["--break-minutes", "7"]).break_minutes, (7.0, 7.0))


class FakeGame:
    """Simulated game: bobber appears on cast and bursts with motion on a bite.

    own_splash: the bite happens at OUR bobber (else only a neighbour's splash
    is heard). lure: the first key press applies a lure (a 1 s cast).
    dry=True: the 'player' casts by themself and reels in 0.6 s after the bite.
    """

    def __init__(self, own_splash, dry=False, lure=False, intruder=False, bite_len=0.5, water=1.0, ramp=False,
                 glitter=False):
        self.own_splash, self.dry, self.lure, self.intruder, self.ramp = own_splash, dry, lure, intruder, ramp
        self.glitter = glitter     # motion like a bite, but the bobber stays put (sun glitter)
        self.bite_len, self.water = bite_len, water
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
        self.splash_at = now + 4.0

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
                "casting": now < self.casting_until, "v11": True}

    def grab_gray(self, left, top, w, h):
        now = fiska.time.monotonic()
        with self.lock:
            f = 90 + self.rng.normal(0, self.water if w <= 300 else 1, (h // 2, w // 2)).astype(np.float32)
            biting = w <= 300 and self.splash_at and 0 <= now - self.splash_at < self.bite_len
            if w <= 300:               # the bobber, centred; a real bite pulls it under
                dy = max(2, f.shape[0] // 8) if biting and self.own_splash and not self.glitter else 0
                cy, cx = f.shape[0] // 2 + dy, f.shape[1] // 2
                f[cy - 2:cy + 2, cx - 2:cx + 2] += 25
            if biting:
                if self.intruder:      # a pet flies through the box instead
                    f += self.rng.normal(0, 15, f.shape).astype(np.float32)
                elif self.ramp:        # something walks in: starts small, keeps growing
                    f += self.rng.normal(0, 2 + 40 * (now - self.splash_at), f.shape).astype(np.float32)
                elif self.glitter:     # sparkle: sharp, everywhere, ~3x calm
                    f += self.rng.normal(0, 3, f.shape).astype(np.float32)
                elif self.own_splash:  # a bite: a soft splash flickering over the bobber
                    hh, ww = f.shape
                    yy, xx = np.mgrid[0:hh, 0:ww]
                    blob = np.exp(-(((yy - hh / 2) / (hh / 4)) ** 2 + ((xx - ww / 2) / (ww / 4)) ** 2))
                    self.flicker = not getattr(self, "flicker", False)
                    f += ((14.0 if self.flicker else 0.0) * blob).astype(np.float32)
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
        argv = ["--grace", "0.3", "--timeout", "7", "--loot-min", "0", "--loot-max", "0",
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

    def test_something_passing_the_box_is_not_a_bite(self):
        s = self.run_casts(FakeGame(own_splash=False, intruder=True))
        self.assertEqual((s["caught"], s["no_loot"], s["no_bite"]), (0, 0, 1), s)

    def test_intruder_that_ramps_up_under_the_cap_is_not_a_bite(self):
        s = self.run_casts(FakeGame(own_splash=False, ramp=True))
        self.assertEqual((s["caught"], s["no_loot"]), (0, 0), s)

    def test_glitter_that_leaves_the_bobber_still_is_not_a_bite(self):
        """Bite-sized motion, but the bobber does not move (sun glitter, the swirl's
        little fish): ignored."""
        s = self.run_casts(FakeGame(own_splash=False, glitter=True, bite_len=1.5))
        self.assertEqual((s["caught"], s["no_loot"], s["no_bite"]), (0, 0, 1), s)

    def test_blip_that_does_not_hold_is_not_a_bite(self):
        s = self.run_casts(FakeGame(own_splash=True, bite_len=0.12))
        self.assertEqual((s["caught"], s["no_loot"]), (0, 0), s)

    def test_run_takes_breaks_between_casts(self):
        import os, tempfile
        game = FakeGame(own_splash=True)
        game.device, game.close = {"name": "fake"}, lambda: None
        lines = []
        orig_log = fiska.log
        fiska.log = lambda msg: lines.append((fiska.time.monotonic(), msg))
        with tempfile.TemporaryDirectory() as d:
            argv = ["--grace", "0.3", "--timeout", "7", "--loot-min", "0", "--loot-max", "0",
                    "--reaction-min", "0", "--reaction-max", "0", "--bobber-min", "4", "--window", "",
                    "--state", os.path.join(d, "s.json"),
                    "--work-minutes", "0.01", "--break-minutes", "0.02", "--max-minutes", "0.2"]
            args = fiska.parse_args(argv)
            orig = (fiska.press_key, fiska.game_window, fiska.client_box, fiska.Screen, fiska.Listener)
            fiska.press_key = game.press
            fiska.game_window = lambda title: 1
            fiska.client_box = lambda hwnd: (0, 0, 1000, 1000)
            fiska.Screen = lambda *a, **k: game
            fiska.Listener = lambda device=None: game
            try:
                f = fiska.Fisher(args)
                f.run()
            finally:
                (fiska.press_key, fiska.game_window, fiska.client_box,
                 fiska.Screen, fiska.Listener) = orig
                fiska.log = orig_log
        msgs = [m for _, m in lines]
        starts = [t for t, m in lines if m.startswith("break:")]
        overs = [t for t, m in lines if m == "break over"]
        self.assertGreaterEqual(len(starts), 1, msgs)
        self.assertGreaterEqual(f.stats["caught"], 2, msgs)
        # every break follows a finished cast and ends before the next one starts
        order = [m.split()[0] for m in msgs if m.startswith(("break:", "break over", "caught!", "cast "))]
        for i, m in enumerate(order):
            if m == "break:":
                self.assertEqual(order[i - 1], "caught!", order)
                self.assertEqual(order[i + 1], "break", order)
        self.assertGreaterEqual(overs[0] - starts[0], 1.1)    # 0.02 min = 1.2 s

    def test_box_that_is_not_water_is_recast(self):
        game = FakeGame(own_splash=True, water=0.0)    # a static spot: wood, UI
        s = self.run_casts(game)
        self.assertEqual((s["not_water"], s["caught"]), (1, 0), s)
        self.assertEqual(game.presses, 2)              # cast, then reel in to recast

    def test_record_writes_csv_log_and_pictures(self):
        import csv, glob, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            self.run_casts(FakeGame(own_splash=True), extra=["--record", d])
            with open(os.path.join(d, "casts.csv"), encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r["outcome"] for r in rows], ["caught"])
            self.assertTrue(rows[0]["bobber_x"] and rows[0]["calm"] and rows[0]["level"])
            self.assertTrue(glob.glob(os.path.join(d, "cast0001_bobber.png")))
            self.assertTrue(glob.glob(os.path.join(d, "cast0001_bite.png")))
            self.assertTrue(glob.glob(os.path.join(d, "cast0001.npz")))

    def test_a_recorded_catch_becomes_a_training_example(self):
        import os, tempfile
        import train_bite
        with tempfile.TemporaryDirectory() as d:
            self.run_casts(FakeGame(own_splash=True), extra=["--record", d])
            data = train_bite.collect(d, fiska.parse_args([]), log=lambda m: None)
        self.assertEqual(len(data), 1)
        session, cast, label, feats = data[0]
        self.assertEqual((cast, label), ("1", 1))
        self.assertEqual(set(feats), set(fiska.BITE_FEATURES))

    def test_the_rules_still_work_without_a_model(self):
        s = self.run_casts(FakeGame(own_splash=True), extra=["--bite-model", "off"])
        self.assertEqual(s["caught"], 1, s)
        s = self.run_casts(FakeGame(own_splash=False, glitter=True, bite_len=1.5),
                           extra=["--bite-model", "off"])
        self.assertEqual((s["caught"], s["no_loot"]), (0, 0), s)

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


