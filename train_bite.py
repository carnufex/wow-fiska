"""Keep the bite model learning from every recorded session.

    .venv\\Scripts\\python train_bite.py             # learn: retrain, promote only if not worse
    .venv\\Scripts\\python train_bite.py --report    # only show how a retrained model would do
    .venv\\Scripts\\python train_bite.py --force     # promote the retrained model regardless
    .venv\\Scripts\\python train_bite.py --slim 10   # free disk: drop search images of all but
                                                    # the 10 newest sessions (bite data is kept)

fiska.py and the console run the same learning step on their own during
breaks and when a run stops, so this is only needed by hand for --report,
--force or --slim.

Labels come from the casts themselves: the bite that led to a catch is a
real bite; the bite of an empty reel-in, and any earlier trigger in a cast
that then caught a fish (a cast has one bite), are false. Each trigger is
turned into fiska.bite_features() from the bobber-box clip and the motion
trace (cached per session in bite_features.json, so only new casts cost
time), and a class-balanced logistic regression is fitted.

Promotion: the new model replaces the current one (models/current.json)
only if, on the sessions the current model has never seen, the new model
- judged session by session, each session predicted by a model trained
without it - lost no more real bites plus let through no more false bites
than the current model did. Every promoted model is also kept in
models/bite_model-<time>.json, so any earlier one can be put back.
"""

import argparse
import csv
import datetime as dt
import glob
import json
import os
import shutil

import numpy as np

import fiska

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
SHIPPED = os.path.join(HERE, "bite_model.json")
CACHE = "bite_features.json"
# Bump when fiska.bite_features() or the labelling changes: caches are rebuilt.
FEATURE_VERSION = 1


def current_model_path(models=None):
    current = os.path.join(models or MODELS, "current.json")
    return current if os.path.exists(current) else SHIPPED


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


def cast_examples(path, row, a):
    """[(label, features)] for the triggers of one recorded cast."""
    z = np.load(path)
    if "motion" not in z or len(z["motion"]) < 30:
        return []
    cl = [c for c in clips(z) if c[0] in ("bite", "still", "light", "rejected")]
    bites = [c for c in cl if c[0] == "bite"]
    if not bites:
        return []
    final = max(bites, key=lambda c: c[1])
    out = []
    for kind, t0, times, frames in cl:
        if kind == final[0] and abs(t0 - final[1]) < 1e-6:
            label = 1 if row["outcome"] == "caught" else 0
        elif row["outcome"] == "caught" and t0 < final[1] - 1.0:
            label = 0                        # an earlier trigger in a cast that caught
        else:
            continue                         # unknown (e.g. before an empty reel-in)
        before = [f for t, f in zip(times, frames) if t0 - 1.0 < t <= t0 - 0.4]
        after = [f for t, f in zip(times, frames) if t0 < t <= t0 + a.confirm_time]
        mot = motion_at(z["motion"], t0, a)
        if mot is None:
            continue
        level, threshold, base, peak, hold = mot
        feats = fiska.bite_features(before, after, level, threshold, base, peak, hold)
        if feats:
            out.append((label, feats))
    return out


def collect(rec, a, log=print):
    """[(session, cast, label, features)] from every folder under rec with a
    casts.csv. Features are cached per session and only recomputed for casts
    whose .npz changed."""
    out = []
    for csv_path in sorted(glob.glob(os.path.join(rec, "**", "casts.csv"), recursive=True)):
        folder = os.path.dirname(csv_path)
        session = os.path.relpath(folder, rec)
        cache_path = os.path.join(folder, CACHE)
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
            if cache.get("version") != FEATURE_VERSION:
                cache = {}
        except (OSError, ValueError):
            cache = {}
        casts = cache.get("casts", {})
        fresh, n, new = {}, 0, 0
        with open(csv_path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            if row.get("outcome") not in ("caught", "empty"):
                continue
            name = f"cast{int(row['cast']):04d}.npz"
            path = os.path.join(folder, name)
            if not os.path.exists(path) or not file_matches_row(path, row):
                continue
            stamp = [os.path.getmtime(path), os.path.getsize(path), row["outcome"]]
            hit = casts.get(name)
            if hit and hit["stamp"] == stamp:
                items = hit["items"]
            else:
                items = [[lab, f] for lab, f in cast_examples(path, row, a)]
                new += 1
            fresh[name] = {"stamp": stamp, "items": items}
            for lab, f in items:
                out.append((session, row["cast"], lab, f))
                n += 1
        if new or len(fresh) != len(casts):
            try:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump({"version": FEATURE_VERSION, "casts": fresh}, fh)
            except OSError as err:
                log(f"could not write {cache_path}: {err}")
        if n:
            log(f"  {session}: {n} triggers" + (f" ({new} new casts read)" if new else ""))
    return out


# -- model -------------------------------------------------------------------
def fit(X, y, l2=1.0, iters=300, weights=None):
    """Class-balanced L2 logistic regression (Newton's method); `weights`
    scales each example (session balance)."""
    n, d = X.shape
    weights = np.ones(n) if weights is None else weights
    wpos = 0.5 / max((weights * y).sum() / weights.sum(), 1e-6)
    wneg = 0.5 / max((weights * (1 - y)).sum() / weights.sum(), 1e-6)
    sw = np.where(y == 1, wpos, wneg) * weights
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
    return X.mean(axis=0), X.std(axis=0) + 1e-9


def predict(model, X):
    z = (X - np.array(model["mean"])) / np.array(model["std"])
    return 1 / (1 + np.exp(-(z @ np.array(model["weights"]) + model["bias"])))


def hand_rule(f, a):
    """The rules before the model (shift, fine shift, texture), for comparison."""
    return f["shift"] >= a.shift_min and f["fine_shift"] * 42 >= a.fine_shift_min and \
        f["texture"] <= a.texture_max


SESSION_CAP = 60   # a session counts as at most this many triggers per class


def session_weights(sessions, y, cap=SESSION_CAP):
    """Weight per example so no session counts for more than `cap` real and
    `cap` false bites: one long Darnassus session (420 bites) outweighed all
    other spots together, and the spot will never be fished again."""
    w = np.ones(len(y))
    for s in set(sessions):
        for lab in (0, 1):
            m = (sessions == s) & (y == lab)
            if m.sum() > cap:
                w[m] = cap / m.sum()
    return w


def weighted_quantile(values, weights, q):
    order = np.argsort(values)
    cum = np.cumsum(weights[order]) / weights.sum()
    return float(values[order][min(np.searchsorted(cum, q), len(values) - 1)])


def train(data, keep=0.985, l2=1.0):
    """(model, out-of-fold probabilities) - each session predicted by a model
    trained on the others; the threshold keeps `keep` of the real bites, with
    every session weighted (session_weights)."""
    names = list(fiska.BITE_FEATURES)
    X = np.array([[f[k] for k in names] for _, _, _, f in data])
    y = np.array([lab for _, _, lab, _ in data], dtype=float)
    sessions = np.array([s for s, _, _, _ in data])
    sw = session_weights(sessions, y)
    p_oof = np.full(len(y), 0.5)
    for s in sorted(set(sessions)):
        test = sessions == s
        if (~test).sum() and len(set(y[~test])) == 2:
            mean, std = standardise(X[~test])
            w, b = fit((X[~test] - mean) / std, y[~test], l2, weights=sw[~test])
            p_oof[test] = 1 / (1 + np.exp(-(((X[test] - mean) / std) @ w + b)))
    pos = y == 1
    thr = weighted_quantile(p_oof[pos], sw[pos], 1 - keep) if pos.any() else 0.5
    mean, std = standardise(X)
    w, b = fit((X - mean) / std, y, l2, weights=sw)
    model = {
        "features": names, "mean": mean.tolist(), "std": std.tolist(),
        "weights": w.tolist(), "bias": float(b), "threshold": thr,
        "sessions": sorted(set(sessions.tolist())),
        "trained_on": {"real": int(y.sum()), "false": int(len(y) - y.sum()),
                       "sessions": len(set(sessions)),
                       "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M")},
        "check": {"lost": int(((p_oof < thr) & (y == 1)).sum()),
                  "through": int(((p_oof >= thr) & (y == 0)).sum())},
    }
    return model, p_oof, X, y, sessions


def errors(p, y, thr):
    """(real bites lost, false bites let through)."""
    return int(((p < thr) & (y == 1)).sum()), int(((p >= thr) & (y == 0)).sum())


def learn(rec=None, force=False, report=False, keep=0.985, say=print, models=None):
    """Retrain on every session; promote if not worse on what the current
    model has never seen. Returns 'promoted', 'kept', 'nothing new' or 'no data'."""
    rec = rec or os.path.join(HERE, "rec")
    models = models or MODELS
    a = fiska.parse_args([])
    data = collect(rec, a, log=lambda m: None)
    if not data:
        say("learning: no labelled bites in the recordings yet")
        return "no data"
    model, p_oof, X, y, sessions = train(data, keep)
    say(f"learning: {model['trained_on']['real']} real and {model['trained_on']['false']} false "
        f"bites from {model['trained_on']['sessions']} sessions; session by session the new model "
        f"would lose {model['check']['lost']} real and let {model['check']['through']} false through")
    path = current_model_path(models)
    try:
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
    except (OSError, ValueError):
        current = None
    if report:
        if current:
            p_cur = predict(current, X)
            lost, through = errors(p_cur, y, current["threshold"])
            say(f"  current model on all of it: loses {lost}, lets {through} through "
                f"(it has seen {len(current.get('sessions', []))} of these sessions)")
        for s in sorted(set(sessions)):
            m = sessions == s
            lost, through = errors(p_oof[m], y[m], model["threshold"])
            say(f"    {s:22s} real {int(y[m].sum()):4d} lost {lost:3d} | false "
                f"{int((y[m] == 0).sum()):3d} through {through:3d}")
        return "report"

    seen = set(current.get("sessions", [])) if current else set()
    new = np.array([s not in seen for s in sessions])
    if current and not new.any() and not force:
        say("learning: no new sessions since the current model - kept it")
        return "nothing new"
    verdict = "promoted"
    if current and not force:
        if len(current.get("features", [])) != len(model["features"]):
            say("learning: the features changed - the new model replaces the old one")
        else:
            cur = errors(predict(current, X[new]), y[new], current["threshold"])
            cand = errors(p_oof[new], y[new], model["threshold"])
            say(f"learning: on the {int(new.sum())} triggers the current model has not seen: "
                f"current loses {cur[0]} real / lets {cur[1]} false through, new {cand[0]} / {cand[1]}")
            if sum(cand) > sum(cur):
                verdict = "kept"
            # ...and not worse overall either: a session with wrong labels can win
            # "its" comparison while the threshold collapses for everything else.
            def rate(check, trained):
                return check["lost"] / max(trained["real"], 1) + check["through"] / max(trained["false"], 1)
            if "check" in current and "lost" in current["check"]:
                was, now = rate(current["check"], current["trained_on"]), rate(model["check"], model["trained_on"])
                if now > was + 0.05:
                    say(f"learning: overall the new model would be worse (lost+through rate "
                        f"{now:.2f} vs {was:.2f})")
                    verdict = "kept"
            if not 0.01 <= model["threshold"] <= 0.99:
                say(f"learning: the new threshold {model['threshold']:.2f} is implausible")
                verdict = "kept"
    if verdict == "kept":
        say("learning: the new model is not better - kept the current one")
        return "kept"
    os.makedirs(models, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    kept = os.path.join(models, f"bite_model-{stamp}.json")
    with open(kept, "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1)
    current_path = os.path.join(models, "current.json")
    shutil.copyfile(kept, current_path + ".tmp")
    os.replace(current_path + ".tmp", current_path)    # a reader never sees half a file
    say(f"learning: new bite model in use (models/bite_model-{stamp}.json, threshold "
        f"{model['threshold']:.2f})")
    return "promoted"


def slim(rec=None, keep_sessions=10, say=print):
    """Drop the search images (pre/post frames, 94 % of a cast file) from all
    but the newest sessions. Clips and traces - all the bite model uses - stay."""
    rec = rec or os.path.join(HERE, "rec")
    folders = sorted({os.path.dirname(p) for p in
                      glob.glob(os.path.join(rec, "**", "casts.csv"), recursive=True)},
                     key=os.path.getmtime, reverse=True)
    freed = 0
    for folder in folders[keep_sessions:]:
        for path in glob.glob(os.path.join(folder, "cast*.npz")):
            with np.load(path) as npz:
                z = dict(npz)
            if "pre" not in z:
                continue
            before = os.path.getsize(path)
            mtime = os.path.getmtime(path)
            z.pop("pre")
            z.pop("post", None)
            tmp = path + ".tmp.npz"
            np.savez_compressed(tmp, **z)
            os.replace(tmp, path)
            os.utime(path, (mtime, mtime))      # keep the time: it ties the file to its cast
            freed += before - os.path.getsize(path)
    say(f"slim: freed {freed / 1e9:.1f} GB (search images of {max(0, len(folders) - keep_sessions)} "
        f"older sessions; bite clips and traces kept)")
    return freed


def main(argv=None, say=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rec", default=os.path.join(HERE, "rec"))
    ap.add_argument("--report", action="store_true", help="show the check, change nothing")
    ap.add_argument("--force", action="store_true", help="promote the new model regardless")
    ap.add_argument("--keep", type=float, default=0.985, help="share of real bites to keep")
    ap.add_argument("--slim", type=int, metavar="N",
                    help="drop search images from all but the N newest sessions")
    opts = ap.parse_args(argv)
    if opts.slim is not None:
        return slim(opts.rec, opts.slim, say)
    return learn(opts.rec, force=opts.force, report=opts.report, keep=opts.keep, say=say)


if __name__ == "__main__":
    main()
