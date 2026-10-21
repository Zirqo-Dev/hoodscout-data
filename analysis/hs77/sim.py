"""HS-77 power simulation: token-block bootstrap of the PRE-watermark pool.

Null world: the in-sample gap is removed, then a known true edge delta is
added. Weekly regime: each real week's own gap deviation is removed, then a
synthetic weekly shock N(0, sigma_w) is added to the tested arm. The registered
decision rules of H-0001 and H-0002 are applied as evaluate.py applies them
(pooled medians; 4 ISO-week subperiods, a week usable at >= 30 rows; "sign
holds" = at least 3 of 4 weekly gaps share the pooled gap's sign).
Not simulated: the fee-sweep and known-fee-only gates (no fee data before the
watermark) and the D1 censoring rule.
"""
import json, sys
from collections import defaultdict
import numpy as np

pool = json.load(open(sys.argv[1]))
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
rng = np.random.default_rng(20261021)
WEEKS = {"2026-W36": 0, "2026-W37": 0, "2026-W38": 1, "2026-W39": 2, "2026-W40": 3}
BAR = 0.01


def tokens_of(rows, arm_of, time_bins=False):
    """time_bins: split the arm rows' real time span into 4 equal bins and use
    them as the 4 subperiods (H-0002: rms is only logged from 2026-09-14)."""
    by = defaultdict(list)
    sel = [r for r in rows if arm_of(r) is not None]
    lo = min(r["cell"] for r in sel); hi = max(r["cell"] for r in sel) + 1
    for r in sel:
        a = arm_of(r)
        wk = min(3, (r["cell"] - lo) * 4 // (hi - lo)) if time_bins else WEEKS[r["week"]]
        by[r["ca"]].append((r["net"], r["base"] if r["base"] is not None else 0.0,
                            wk, r["cell"], a))
    return [np.array(v, dtype=float) for v in by.values()]


def weekly_dev(toks, gap_fn):
    allr = np.vstack(toks)
    g = gap_fn(allr)
    dev = {}
    for k in range(4):
        dev[k] = gap_fn(allr[allr[:, 2] == k]) - g
    return g, dev


_NC = {}
def ncells(toks, arms):
    key = (id(toks), tuple(arms))
    if key not in _NC:
        _NC[key] = np.array([[len({int(c) for c, a in zip(t[:, 3], t[:, 4]) if int(a) == arm})
                              for arm in arms] for t in toks])
    return _NC[key]


def draw(toks, target, arm_for_n):
    """Whole tokens with replacement until eff_n (min arm) >= target.
    A token drawn twice counts as two tokens (distinct cells)."""
    nc = ncells(toks, arm_for_n)
    picked, tot = [], np.zeros(len(arm_for_n))
    while tot.min() < target:
        i = int(rng.integers(len(toks)))
        picked.append(i); tot += nc[i]
    return np.vstack([toks[i] for i in picked]), len(picked)


def sim(toks, gap_fn, apply_shift, classify, deltas, sigmas, targets, arms):
    g0, dev = weekly_dev(toks, gap_fn)
    out = {}
    for target in targets:
        for s in sigmas:
            for d in deltas:
                res = defaultdict(int); ntok = []
                for _ in range(REPS):
                    x, nt = draw(toks, target, arms)
                    ntok.append(nt)
                    wk = x[:, 2].astype(int)
                    shock = rng.normal(0, s, 4) if s else np.zeros(4)
                    adj = -g0 + d - np.array([dev[k] for k in range(4)])[wk] + shock[wk]
                    x = apply_shift(x, adj)
                    g = gap_fn(x)
                    gk = [gap_fn(x[wk == k]) if (wk == k).sum() >= 30 else None
                          for k in range(4)]
                    res[classify(g, gk)] += 1
                out[(target, s, d)] = ({k: v / REPS for k, v in res.items()},
                                       int(np.median(ntok)))
    return out


def stable(g, gk):
    u = [v for v in gk if v is not None]
    return len(u) >= 4 and sum((v > 0) == (g > 0) for v in u) >= 3, len(u)


# ---- H-0001: BEST vs per-row scored-pile baseline -------------------------
def h1_arm(r):
    return 0 if (r["tier"] == "BEST" and r["net"] is not None
                 and r["base"] is not None) else None

def h1_gap(x):
    return np.median(x[:, 0]) - np.median(x[:, 1])

def h1_shift(x, adj):
    x = x.copy(); x[:, 0] = np.maximum(-1.0, x[:, 0] + adj); return x

def h1_class(g, gk):
    if g < BAR:
        return "pass (null stands)"
    st, n = stable(g, gk)
    if n < 4:
        return "waiting"
    return "fail (BEST carries info)" if st else "inconclusive"


# ---- H-0002: rms >= +4 vs rms <= -4 ----------------------------------------
def h2_arm(r):
    if r["net"] is None or r["rms"] is None:
        return None
    return 0 if r["rms"] >= 4 else (1 if r["rms"] <= -4 else None)

def h2_gap(x):
    a, b = x[x[:, 4] == 0, 0], x[x[:, 4] == 1, 0]
    if len(a) == 0 or len(b) == 0:
        return 0.0
    return np.median(a) - np.median(b)

def h2_shift(x, adj):
    x = x.copy(); m = x[:, 4] == 0
    x[m, 0] = np.maximum(-1.0, x[m, 0] + adj[m]); return x

def h2_class(g, gk):
    if g > -BAR:
        return "fail (no edge)"
    st, n = stable(g, gk)
    if n < 4:
        return "waiting"
    return "pass (edge found)" if st else "inconclusive"


which = sys.argv[3] if len(sys.argv) > 3 else "both"
res = {}
if which in ("h1", "both"):
    t1 = tokens_of(pool, h1_arm)
    r1 = sim(t1, h1_gap, h1_shift, h1_class,
             deltas=[0.0, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.03],
             sigmas=[0.0, 0.005, 0.01, 0.015], targets=[300, 1800], arms=[0])
    g0, dev = weekly_dev(t1, h1_gap)
    res["H-0001"] = {"weekly_dev_sd_pp": round(100 * float(np.std(list(dev.values()))), 2),
                     "tokens_in_pool": len(t1),
                     "grid": {f"E{k[0]}|sw{k[1]*100:g}|d{k[2]*100:g}": v for k, v in r1.items()}}
if which in ("h2", "both"):
    t2 = tokens_of(pool, h2_arm, time_bins=True)
    r2 = sim(t2, h2_gap, h2_shift, h2_class,
             deltas=[0.0, -0.005, -0.01, -0.015, -0.02, -0.03],
             sigmas=[0.0, 0.005, 0.01, 0.015], targets=[150, 2000], arms=[0, 1])
    g0, dev = weekly_dev(t2, h2_gap)
    res["H-0002"] = {"weekly_dev_sd_pp": round(100 * float(np.std(list(dev.values()))), 2),
                     "tokens_in_pool": len(t2),
                     "grid": {f"E{k[0]}|sw{k[1]*100:g}|d{k[2]*100:g}": v for k, v in r2.items()}}
json.dump(res, open(sys.argv[4] if len(sys.argv) > 4 else "sim_out.json", "w"), indent=1)
print("done")
