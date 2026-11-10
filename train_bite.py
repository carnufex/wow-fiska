"""Train the bite model (bite_model.json) from recorded sessions.

    .venv\\Scripts\\python train_bite.py            # reads rec\\, writes bite_model.json
    .venv\\Scripts\\python train_bite.py --dry      # only report, don't write

Every recorded cast labels itself: the bite that led to a catch is a real
bite; the bite of an empty reel-in, and any earlier trigger in a cast that
then caught a fish (a cast has one bite), are false. Each trigger is turned
into fiska.bite_features() from the bobber-box clip and the motion trace, a
logistic regression is fitted, checked session by session (each session
predicted by a model that never saw it), and its threshold set so it keeps
--keep of the real bites. Run it after new sessions to learn from them.
"""

import argparse
import csv
import datetime as dt
import glob
import json
import os
import sys

import numpy as np

import fiska

HERE = os.path.dirname(os.path.abspath(__file__))


# -- dataset -----------------------------------------------------------------
def motion_at(motion, t0, a):
    """The tracker's numbers at a trigger at t0, from the recorded trace."""
    t, m = motion[:, 0], motion[:, 1]
    recent = m[(t > t0 - a.motion_window) & (t <= t0)]
    before = m[(t > t0 - 0.3 - 3.0) & (t <= t0 - 0.3)]
    held = m[(t > t0) & (t <= t0 + a.confirm_time)]
    if len(recent) < 2 or len(before) < 20 or len(held) < 2:
        return None
    base = float(np.median(before))
    threshold = max(base * a.motion_ratio, base + a.motion_min)
    peak = float(m[(t > t0 - 0.15) & (t <= t0)].max())
    return float(np.median(recent)), threshold, base, peak, float(np.median(held))


def clips(z):
    """[(kind, trigger time, times, frames)] of the clips in a cast's .npz."""
    out = []
    for i in range(8):
        if f"clip{i}_kind" not in z:
            continue
        kind, t = str(z[f"clip{i}_kind"]), float(z[f"clip{i}_t"])
        # 'bite' clips are stamped with the trigger, the others with the decision.
        t0 = t if kind == "bite" else t - 0.2
        out.append((kind, t0, z[f"clip{i}_times"], z[f"clip{i}_frames"]))
    return out


def file_matches_row(path, row):
    """Back-to-back runs once restarted the cast numbers in one folder and
    overwrote each other's files: use a file only if it was written during
    the cast the csv row describes."""
    start = dt.datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").timestamp()
    end = start + float(row["duration_s"] or 0) + 15
    return start - 2 <= os.path.getmtime(path) <= end


def collect(rec, a, log=print):
    """[(session, cast, label, features)] from every folder under rec with a casts.csv."""
    out = []
    for csv_path in sorted(glob.glob(os.path.join(rec, "**", "casts.csv"), recursive=True)):
        folder = os.path.dirname(csv_path)
        session = os.path.relpath(folder, rec)
        n = 0
        for row in csv.DictReader(open(csv_path, encoding="utf-8")):
            if row.get("outcome") not in ("caught", "empty"):
                continue
            path = os.path.join(folder, f"cast{int(row['cast']):04d}.npz")
            if not os.path.exists(path) or not file_matches_row(path, row):
                continue
            z = np.load(path)
            if "motion" not in z or len(z["motion"]) < 30:
                continue
            cl = [c for c in clips(z) if c[0] in ("bite", "still", "light", "rejected")]
            bites = [c for c in cl if c[0] == "bite"]
            if not bites:
                continue
            final = max(bites, key=lambda c: c[1])
            for kind, t0, times, frames in cl:
                if c_is(final, kind, t0):
                    label = 1 if row["outcome"] == "caught" else 0
                elif row["outcome"] == "caught" and t0 < final[1] - 1.0:
                    label = 0            # an earlier trigger in a cast that caught
                else:
                    continue             # unknown (e.g. before an empty reel-in)
                before = [f for t, f in zip(times, frames) if t0 - 1.0 < t <= t0 - 0.4]
                after = [f for t, f in zip(times, frames) if t0 < t <= t0 + a.confirm_time]
                mot = motion_at(z["motion"], t0, a)
                if mot is None:
                    continue
                level, threshold, base, peak, hold = mot
                feats = fiska.bite_features(before, after, level, threshold, base, peak, hold)
                if feats:
                    out.append((session, row["cast"], label, feats))
                    n += 1
        if n:
            log(f"  {session}: {n} triggers")
    return out


def c_is(final, kind, t0):
    return kind == final[0] and abs(t0 - final[1]) < 1e-6


# -- model -------------------------------------------------------------------
def fit(X, y, l2=1.0, iters=300):
    """Class-balanced L2 logistic regression (Newton's method)."""
    n, d = X.shape
    wpos, wneg = 0.5 / max(y.mean(), 1e-6), 0.5 / max(1 - y.mean(), 1e-6)
    sw = np.where(y == 1, wpos, wneg)
    Xb = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, l2)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Xb @ w)))
        g = Xb.T @ (sw * (p - y)) + reg * w
        H = (Xb * (sw * p * (1 - p))[:, None]).T @ Xb + np.diag(reg)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w[:-1], w[-1]


def standardise(X):
    mean, std = X.mean(axis=0), X.std(axis=0) + 1e-9
    return mean, std


def hand_rule(f, a):
    """The rules before the model (shift, fine shift, texture), for comparison."""
    return f["shift"] >= a.shift_min and f["fine_shift"] * 42 >= a.fine_shift_min and \
        f["texture"] <= a.texture_max


def threshold_for(p_pos, keep):
    """Highest threshold that still keeps `keep` of the real bites."""
    return float(np.quantile(p_pos, 1 - keep)) if len(p_pos) else 0.5


def main(argv=None, say=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rec", default=os.path.join(HERE, "rec"))
    ap.add_argument("--out", default=os.path.join(HERE, "bite_model.json"))
    ap.add_argument("--keep", type=float, default=0.985, help="share of real bites to keep (default 0.985)")
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--dry", action="store_true", help="report only, don't write the model")
    opts = ap.parse_args(argv)
    a = fiska.parse_args([])
    names = list(fiska.BITE_FEATURES)

    say(f"reading {opts.rec}")
    data = collect(opts.rec, a, log=say)
    if not data:
        say("no labelled triggers found")
        return None
    X = np.array([[f[k] for k in names] for _, _, _, f in data])
    y = np.array([lab for _, _, lab, _ in data], dtype=float)
    sessions = np.array([s for s, _, _, _ in data])
    say(f"{len(y)} triggers: {int(y.sum())} real bites, {int(len(y) - y.sum())} false, "
          f"{len(set(sessions))} sessions")

    # Each session predicted by a model trained on the others.
    p_oof = np.zeros(len(y))
    for s in sorted(set(sessions)):
        test = sessions == s
        if (~test).sum() == 0 or len(set(y[~test])) < 2:
            p_oof[test] = 0.5
            continue
        mean, std = standardise(X[~test])
        w, b = fit((X[~test] - mean) / std, y[~test], opts.l2)
        p_oof[test] = 1 / (1 + np.exp(-(((X[test] - mean) / std) @ w + b)))
    thr = threshold_for(p_oof[y == 1], opts.keep)
    model_lost = int(((p_oof < thr) & (y == 1)).sum())
    model_pass = int(((p_oof >= thr) & (y == 0)).sum())
    hand = np.array([hand_rule(f, a) for _, _, _, f in data])
    hand_lost = int((~hand & (y == 1)).sum())
    hand_pass = int((hand & (y == 0)).sum())
    neg = int(len(y) - y.sum())
    say(f"session-by-session check (threshold {thr:.2f}):")
    say(f"  model: real bites lost {model_lost}/{int(y.sum())}, false bites let through {model_pass}/{neg}")
    say(f"  rules: real bites lost {hand_lost}/{int(y.sum())}, false bites let through {hand_pass}/{neg}")
    for s in sorted(set(sessions)):
        m = sessions == s
        say(f"    {s:22s} real {int(y[m].sum()):4d} lost {int(((p_oof < thr) & (y == 1) & m).sum()):3d}"
              f" | false {int((y[m] == 0).sum()):3d} through {int(((p_oof >= thr) & (y == 0) & m).sum()):3d}"
              f"  (rules: lost {int((~hand & (y == 1) & m).sum())}, through {int((hand & (y == 0) & m).sum())})")

    mean, std = standardise(X)
    w, b = fit((X - mean) / std, y, opts.l2)
    model = {
        "features": names, "mean": mean.tolist(), "std": std.tolist(),
        "weights": w.tolist(), "bias": float(b), "threshold": thr,
        "trained_on": {"real": int(y.sum()), "false": neg, "sessions": len(set(sessions)),
                       "date": dt.date.today().isoformat()},
        "check": {"model_lost": model_lost, "model_through": model_pass,
                  "rules_lost": hand_lost, "rules_through": hand_pass},
    }
    say("weights (standardised): " + ", ".join(f"{k} {v:+.2f}" for k, v in zip(names, w)))
    if not opts.dry:
        with open(opts.out, "w", encoding="utf-8") as fh:
            json.dump(model, fh, indent=1)
        say(f"wrote {opts.out}")
    return model


if __name__ == "__main__":
    main()
