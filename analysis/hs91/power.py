"""HS-91 gate 3: power check for a candidate DEAD_72H hypothesis (NOT registered).

Report-only. Reads ONLY data/history.jsonl as of commit d9f8047 (2026-09-04 to
2026-10-02), pre-watermark data already used by the in-sample backtest, the
same way analysis/hs77/build_pool.py does: a file path on the command line,
every row's ts asserted before the 2026-10-18T13:30:25Z watermark.

Candidate rule, as given for HS-91:
  unit      one token
  entry     the token's 4th distinct snapshot ts (followup.py MIN_SNAPSHOTS)
  arms      entry liq >= FLOOR vs below; entry liq (and entry price) must be > 0
  DEAD_72H  on a 3 h grid from entry (nearest print within +/-45 min of each
            point), two consecutive grid points where
            liq < max(1000, 0.10 * entry liq) OR price <= 0.10 * entry price.
            A point with no print is unknown; fewer than 12 of 24 known points
            is censored.
  metric    death rate (>= FLOOR arm) minus death rate (< FLOOR arm), in pp.
The 24 h rate is the same rule cut to the first 8 grid points, censored below
4 of 8 known (the 72 h half-known share, scaled). That cut is this script's
choice, not part of the candidate.

Standard library only, so the tests run on requirements-test.txt alone.

Usage: python analysis/hs91/power.py HISTORY.jsonl [REPORT.md]
"""
import bisect, json, random, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta

WATERMARK = datetime.fromisoformat("2026-10-18T13:30:25+00:00")
MIN_SNAPSHOTS = 4
STEP = timedelta(hours=3)
TOL = timedelta(minutes=45)
LIQ_ABS, LIQ_REL, PRICE_REL = 1000.0, 0.10, 0.10
HORIZONS = {24: (8, 4), 72: (24, 12)}        # hours: (grid points, min known)
FLOOR, SENS_FLOORS = 50_000, (25_000, 100_000)
SIM_N = (100, 140, 200)
THRESHOLDS = (-10, -15, -20)
REPS, SEED = 5000, 20261028
FA_MAX, DET_MIN = 0.05, 0.80


def load_rows(path):
    """All rows, with dt parsed. Asserts every row is pre-watermark."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            d["dt"] = datetime.fromisoformat(d["ts"])
            assert d["dt"] < WATERMARK, f"post-watermark row {d['ts']}"
            rows.append(d)
    return rows


def by_token(rows):
    """{ca: rows sorted by dt, one row per distinct ts (first one kept)}."""
    out = defaultdict(dict)
    for r in rows:
        out[r["ca"]].setdefault(r["dt"], r)
    return {ca: [v[t] for t in sorted(v)] for ca, v in out.items()}


def grid_prints(series, times, entry_dt, n_points):
    """Nearest print within +/-TOL of entry + k*STEP, k = 1..n_points.

    None where no print falls in the window. Ties go to the earlier print."""
    out = []
    for k in range(1, n_points + 1):
        g = entry_dt + k * STEP
        i, j = bisect.bisect_left(times, g - TOL), bisect.bisect_right(times, g + TOL)
        win = series[i:j]
        out.append(min(win, key=lambda r: abs(r["dt"] - g)) if win else None)
    return out


def point_dead(row, entry_liq, entry_price):
    """Dead print: liq below the floor or price down 90%+. Missing liq is 0,
    as in build_pool.py; a missing price cannot trigger the price leg."""
    liq = row.get("liq") or 0.0
    if liq < max(LIQ_ABS, LIQ_REL * entry_liq):
        return True
    p = row.get("price")
    return p is not None and p <= PRICE_REL * entry_price


def classify(points, entry_liq, entry_price, min_known):
    """'dead' | 'alive' | 'censored' for one token's grid prints.

    Dead needs two consecutive known dead points; an unknown point between
    them breaks the run. Censoring is applied first: a token with too few
    known points is censored whether or not it shows a death."""
    known = sum(p is not None for p in points)
    if known < min_known:
        return "censored"
    flags = [p is not None and point_dead(p, entry_liq, entry_price) for p in points]
    return "dead" if any(a and b for a, b in zip(flags, flags[1:])) else "alive"


def tokens(rows):
    """One record per token that reaches MIN_SNAPSHOTS, with its outcomes."""
    latest = max(r["dt"] for r in rows)
    out = []
    for ca, series in by_token(rows).items():
        if len(series) < MIN_SNAPSHOTS:
            continue
        e = series[MIN_SNAPSHOTS - 1]
        rec = {"ca": ca, "entry_ts": e["ts"], "liq": e.get("liq") or 0.0,
               "price": e.get("price") or 0.0,
               "lag_h": (e["dt"] - series[0]["dt"]).total_seconds() / 3600}
        times = [r["dt"] for r in series]
        for h, (n_pts, min_known) in HORIZONS.items():
            if rec["liq"] <= 0 or rec["price"] <= 0:
                rec[h] = "not_enterable"
            elif e["dt"] + n_pts * STEP + TOL > latest:
                rec[h] = "pending"
            else:
                pts = grid_prints(series, times, e["dt"], n_pts)
                rec[h] = classify(pts, rec["liq"], rec["price"], min_known)
                if h == 72:
                    rec["known72"] = sum(p is not None for p in pts)
                    rec["deadsig72"] = classify(pts, rec["liq"], rec["price"], 0) == "dead"
        out.append(rec)
    return out


def arm(rec, floor):
    return "hi" if rec["liq"] >= floor else "lo"


def rate(outs):
    """Death rate among uncensored outcomes, or None."""
    ev = [o for o in outs if o in ("dead", "alive")]
    return (sum(o == "dead" for o in ev) / len(ev), len(ev)) if ev else (None, 0)


def gap_pp(bits_hi, bits_lo):
    return 100.0 * (sum(bits_hi) / len(bits_hi) - sum(bits_lo) / len(bits_lo))


def simulate(bits_hi, bits_lo, n, reps, rng):
    """Bootstrap n tokens per arm. Returns (null gaps, observed-world gaps).

    Null: both arms drawn from the pooled tokens (arm labels shuffled), so the
    true gap is zero. Observed: each arm drawn from its own tokens."""
    pooled = bits_hi + bits_lo
    null, obs = [], []
    for _ in range(reps):
        null.append(gap_pp(rng.choices(pooled, k=n), rng.choices(pooled, k=n)))
        obs.append(gap_pp(rng.choices(bits_hi, k=n), rng.choices(bits_lo, k=n)))
    return null, obs


def fires(gaps, threshold):
    return sum(g <= threshold for g in gaps) / len(gaps)


def pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def q(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


def report(recs, src_desc):
    L = []
    w = L.append
    w("# HS-91 gate 3: power check for the DEAD_72H candidate (report only)")
    w("")
    w(f"Source: {src_desc}. Every row asserted before {WATERMARK.isoformat()}.")
    w("The candidate hypothesis is NOT registered. Nothing here decides anything.")
    w("")
    w(f"Tokens reaching {MIN_SNAPSHOTS} distinct snapshots: {len(recs)}. "
      f"Not enterable (entry liq or price <= 0): "
      f"{sum(r[72] == 'not_enterable' for r in recs)}. "
      f"Pending at 72 h (grid runs past the last snapshot): "
      f"{sum(r[72] == 'pending' for r in recs)}.")
    w("")

    # 1. arms, censoring, lag
    w(f"## 1. Tokens per arm (floor {FLOOR:,} USD), censoring, enrolment lag")
    w("")
    w("| arm | tokens (72 h window closed) | censored 72 h | evaluable 72 h | censored 24 h | lag median h | lag p25 h | lag p75 h | lag p90 h | lag max h |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for a, name in (("hi", f">= {FLOOR // 1000}k"), ("lo", f"< {FLOOR // 1000}k")):
        rs = [r for r in recs if r[72] in ("dead", "alive", "censored") and arm(r, FLOOR) == a]
        c72 = sum(r[72] == "censored" for r in rs)
        rs24 = [r for r in recs if r[24] in ("dead", "alive", "censored") and arm(r, FLOOR) == a]
        c24 = sum(r[24] == "censored" for r in rs24)
        lags = [r["lag_h"] for r in rs]
        w(f"| {name} | {len(rs)} | {c72} ({pct(c72 / len(rs) if rs else None)}) | {len(rs) - c72} "
          f"| {c24} ({pct(c24 / len(rs24) if rs24 else None)}) "
          f"| {statistics.median(lags):.2f} | {q(lags, .25):.2f} | {q(lags, .75):.2f} "
          f"| {q(lags, .9):.2f} | {max(lags):.2f} |" if lags else f"| {name} | 0 | | | | | | | | |")
    w("")
    w("Enrolment lag = entry (4th distinct snapshot) minus the token's first snapshot.")
    for a, name in (("hi", f">= {FLOOR // 1000}k"), ("lo", f"< {FLOOR // 1000}k")):
        kn = [r["known72"] for r in recs if "known72" in r and arm(r, FLOOR) == a]
        if kn:
            w(f"Known grid points of 24 at 72 h, {name}: median {statistics.median(kn):g}, "
              f"p25 {q(kn, .25)}, p75 {q(kn, .75)}.")
    cens = [r for r in recs if r[72] == "censored"]
    if cens:
        sig = sum(r["deadsig72"] for r in cens)
        w(f"Censored tokens that nonetheless show two consecutive dead known points: "
          f"{sig} of {len(cens)} ({pct(sig / len(cens))}). History only lists tokens "
          f"while they are on the collector's lists, so a dying token tends to stop "
          f"printing; censoring likely removes deaths, not a random sample.")
    w("")

    # 2. base rates
    w("## 2. Base DEAD rate per arm (pre-watermark backtest)")
    w("")
    w("| floor | role | arm | DEAD 24 h (n evaluable) | DEAD 72 h (n evaluable) | gap 24 h pp | gap 72 h pp |")
    w("|---|---|---|---|---|---|---|")
    rates = {}
    for fl in (FLOOR,) + SENS_FLOORS:
        role = "candidate" if fl == FLOOR else "sensitivity only"
        rr = {}
        for a in ("hi", "lo"):
            rr[a] = {h: rate([r[h] for r in recs if arm(r, fl) == a]) for h in HORIZONS}
        rates[fl] = rr
        gaps = {h: (None if rr["hi"][h][0] is None or rr["lo"][h][0] is None
                    else 100 * (rr["hi"][h][0] - rr["lo"][h][0])) for h in HORIZONS}
        for a, name in (("hi", f">= {fl // 1000}k"), ("lo", f"< {fl // 1000}k")):
            g = (lambda h: "" if a == "lo" else ("n/a" if gaps[h] is None else f"{gaps[h]:+.1f}"))
            w(f"| {fl // 1000}k | {role} | {name} | {pct(rr[a][24][0])} ({rr[a][24][1]}) "
              f"| {pct(rr[a][72][0])} ({rr[a][72][1]}) | {g(24)} | {g(72)} |")
    w("")

    # 3. ceiling
    hi72, lo72 = rates[FLOOR]["hi"][72][0], rates[FLOOR]["lo"][72][0]
    w("## 3. Ceiling check (72 h, 50k floor)")
    w("")
    if hi72 is not None and lo72 is not None and hi72 > .8 and lo72 > .8:
        w(f"CEILING HIT: both arms die above 80% at 72 h ({pct(hi72)} and {pct(lo72)}). "
          "The metric has little room to separate the arms.")
    else:
        w(f"No ceiling: both arms do NOT die above 80% at 72 h (>= 50k: {pct(hi72)}, "
          f"< 50k: {pct(lo72)}).")
    w("")

    # 4. simulation
    ev = [r for r in recs if r[72] in ("dead", "alive")]
    bh = [int(r[72] == "dead") for r in ev if arm(r, FLOOR) == "hi"]
    bl = [int(r[72] == "dead") for r in ev if arm(r, FLOOR) == "lo"]
    obs_gap = gap_pp(bh, bl) if bh and bl else None
    w("## 4. Simulation (bootstrap by token, 72 h, 50k floor)")
    w("")
    if obs_gap is None:
        w("Not run: an arm has no evaluable tokens.")
        return "\n".join(L) + "\n", None
    w(f"Resampling pool: evaluable (uncensored) tokens, {len(bh)} in >= 50k and "
      f"{len(bl)} in < 50k. Observed gap {obs_gap:+.1f} pp. {REPS} reps per cell, "
      f"seed {SEED}. n counts EVALUABLE tokens per arm; censored tokens are not "
      f"in the pool, so enrolment must be larger by the censored share.")
    w("(a) no true gap: both arms drawn from the pooled tokens (labels shuffled). "
      "(b) observed gap: each arm drawn from its own tokens, so (b) assumes the "
      "in-sample gap is the truth, which flatters detection.")
    w("")
    for name, bits in ((">= 50k", bh), ("< 50k", bl)):
        if len(bits) < 30 or sum(bits) in (0, len(bits)):
            w(f"WARNING: the {name} pool has {len(bits)} evaluable tokens and "
              f"{sum(bits)} deaths. Resampling it at n = 100+ re-uses the same few "
              f"tokens; its spread in (b) is understated, and with 0 or all deaths "
              f"it has none at all.")
    w("")
    w("| n per arm | threshold pp | false alarm (a) | detection (b) | (b) gap p5 / median / p95 pp |")
    w("|---|---|---|---|---|")
    rng = random.Random(SEED)
    res = {}
    for n in SIM_N:
        null, obs = simulate(bh, bl, n, REPS, rng)
        for t in THRESHOLDS:
            res[(n, t)] = (fires(null, t), fires(obs, t))
            w(f"| {n} | {t} | {pct(res[(n, t)][0])} | {pct(res[(n, t)][1])} "
              f"| {q(obs, .05):+.1f} / {statistics.median(obs):+.1f} / {q(obs, .95):+.1f} |")
    w("")

    # 5. suggestion
    ok = [(n, t) for (n, t), (fa, det) in res.items() if fa <= FA_MAX and det >= DET_MIN]
    w("## 5. Suggestion for Pete (not a decision)")
    w("")
    w(f"Rule used: smallest n with false alarm <= {FA_MAX:.0%} and detection >= "
      f"{DET_MIN:.0%}; ties go to the stricter (more negative) threshold.")
    if obs_gap > max(THRESHOLDS):
        line = (f"SUGGESTION (for Pete, not a decision): do not set a threshold or min n "
                f"from this data. The in-sample gap is {obs_gap:+.1f} pp, above every "
                f"tested threshold (the >= 50k arm dies MORE, not less), the < 50k arm has "
                f"only {len(bl)} evaluable tokens, and most tokens are censored because "
                f"history stops printing them. History alone cannot size this candidate.")
    elif ok:
        n, t = min(ok, key=lambda x: (x[0], x[1]))
        line = (f"SUGGESTION (for Pete, not a decision): threshold {t} pp, min {n} "
                f"evaluable tokens per arm (false alarm {pct(res[(n, t)][0])}, "
                f"detection {pct(res[(n, t)][1])} if the in-sample gap is real).")
    else:
        line = (f"SUGGESTION (for Pete, not a decision): no tested threshold/n pair meets "
                f"false alarm <= {FA_MAX:.0%} and detection >= {DET_MIN:.0%}; the "
                f"candidate as stated looks underpowered on this data.")
    w(line)
    return "\n".join(L) + "\n", line


def main():
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    recs = tokens(load_rows(src))
    text, _ = report(recs, "data/history.jsonl at commit d9f8047 (2026-09-04 to 2026-10-02)")
    print(text, end="")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
