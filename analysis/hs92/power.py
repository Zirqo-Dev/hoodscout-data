#!/usr/bin/env python3
"""HS-92 gate 3: power check for a candidate HIT/STOP hypothesis (NOT registered).

Report-only. Reads data/followup/enrolled.json, data/log and
data/followup/candles; writes nothing. Outcomes are computed only for tokens
enrolled before H-0008's log watermark (asserted in outcome()), and only one
pooled split is printed: no outcome is ever split by tier or any predictor.

Candidate rule, as given for HS-92 (window per the owner's answer 0):
  population  enrolled.json tokens with enrolled_at - first_seen <= 24 h
  entry       the token's last data/log row with enrolled_at - 30 min <= ts
              <= enrolled_at, deduplicated on (ts, ca) as label.py does;
              none, or price not > 0: dropped and counted
  arms        A = entry tier == 'BEST'; B = every other tier
  window      entry ts to first_seen + 7 d (the end of the follow-up watch),
              hourly candles that open at or after entry and close by the end
  HIT         a high >= 2.0 x entry price before a low <= 0.5 x entry price
  STOP        the low first; a candle touching both is STOP
  TIMEOUT     neither, and the candles cover the window; exit at the last
              close at or before first_seen + 7 d
  CENSORED    neither, and the candles do not cover the window
  IMMATURE    first_seen + 7 d not yet passed or no candle file: excluded and
              counted, not censored
Cost per token is label.py's nets() at entry with liq_out = liq_in.

Standard library plus scripts/label.py (imported for its cost model only).

Usage: python analysis/hs92/power.py
"""
import bisect, glob, gzip, json, os, random, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import label  # noqa: E402  (cost model: nets, FEE_FALLBACK, NOTIONALS)

WATERMARK = datetime.fromisoformat("2026-10-28T17:30:26.797920+00:00")  # H-0008
LAG_MAX = timedelta(hours=24)
SENS_LAG = timedelta(hours=96)
MIN_PRIMARY = 25                 # below this, the 96 h sensitivity line is added
WINDOW = timedelta(days=7)       # followup.py WINDOW: the watch ends at first_seen + 7 d
ENTRY_LOOKBACK = timedelta(minutes=30)
HOUR = timedelta(hours=1)
UP, DOWN = 2.0, 0.5
RATIO_LO, RATIO_HI = 0.5, 2.0
CANDLE_LIMIT = 200               # followup.py asks for limit=200
SIZE = label.NOTIONALS[0]        # 500 USD
BEST = "BEST"
OUTCOMES = ("HIT", "STOP", "TIMEOUT", "CENSORED")

NS = (40, 60, 80, 100, 120)
B_MULT = 4
DELTAS = (0, 5, 10, 15)          # pp added to arm A's evaluable hit share
REPS, SEED = 10_000, 20261102

PACE_FROM = datetime(2026, 10, 24, tzinfo=timezone.utc)
REGISTER = datetime(2026, 11, 2, tzinfo=timezone.utc)


def parse(s):
    return datetime.fromisoformat(s)


# ------------------------------------------------------------------ loading

def load_log(cas, log_dir):
    """{ca: rows sorted by dt} for the given cas, deduplicated on (ts, ca),
    first occurrence kept, over the same files in the same order as
    label.load_log()."""
    paths = sorted(glob.glob(f"{log_dir}/*/*.jsonl") + glob.glob(f"{log_dir}/*.jsonl.gz"))
    seen, out = set(), defaultdict(list)
    for p in paths:
        with (gzip.open(p, "rt") if p.endswith(".gz") else open(p)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["dt"] = parse(r["ts"])
                except (ValueError, KeyError):
                    continue
                key = (r["ts"], r["ca"])
                if r["ca"] not in cas or key in seen:
                    continue
                seen.add(key)
                out[r["ca"]].append(r)
    for v in out.values():
        v.sort(key=lambda r: r["dt"])
    return out


def load_candles(path):
    """[(open_dt, o, h, l, c), ...] oldest first, or None if no file."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    rows = [(datetime.fromtimestamp(int(c[0]), timezone.utc), *map(float, c[1:5]))
            for c in d.get("ohlcv_list") or []]
    return sorted(rows)


def watch_clock(watch_dir):
    """run_ts of the latest follow-up run: the clock for maturity and pace."""
    metas = sorted(glob.glob(os.path.join(watch_dir, "*", "*.meta.json")))
    with open(metas[-1], encoding="utf-8") as f:
        return parse(json.load(f)["run_ts"])


# ------------------------------------------------------------------ per token

def lag(e):
    return parse(e["enrolled_at"]) - parse(e["first_seen"])


def entry_row(rows, enrolled_at):
    """The last row with enrolled_at - 30 min <= dt <= enrolled_at, or None."""
    times = [r["dt"] for r in rows]
    i = bisect.bisect_right(times, enrolled_at)
    if i and times[i - 1] >= enrolled_at - ENTRY_LOOKBACK:
        return rows[i - 1]
    return None


def entry_ratio(price, candles, entry_dt):
    """Entry log price / close of the last candle closing at or before entry.

    Entry side only: no candle after entry is read. None if there is none."""
    before = [c for c in candles if c[0] + HOUR <= entry_dt]
    return price / before[-1][4] if before and before[-1][4] > 0 else None


def cost(row):
    """Round-trip cost at entry: label.nets() at gross 0 with liq_out = liq_in,
    unknown fee at label.FEE_FALLBACK. None when entry liq is not > 0."""
    liq = row.get("liq")
    if not isinstance(liq, (int, float)) or liq <= 0:
        return None
    fee = row.get("fee") if row.get("fee") is not None else label.FEE_FALLBACK
    return -label.nets(0.0, liq, liq, fee)[f"ret_net_{SIZE}"]


def outcome(e, entry_dt, entry_price, candles):
    """(outcome, exit_price, info) for one pre-watermark token.

    info: covered (window end and start both inside the candle file),
    window_hours (hour slots in the window), missing_hours (slots with no
    candle: GeckoTerminal omits hours with no trades)."""
    assert parse(e["enrolled_at"]) < WATERMARK, \
        f"outcome requested for a post-watermark enrolment {e['ca']}"
    end = parse(e["first_seen"]) + WINDOW
    up, dn = UP * entry_price, DOWN * entry_price
    win = [c for c in candles if c[0] >= entry_dt and c[0] + HOUR <= end]
    first_slot = entry_dt.replace(minute=0, second=0, microsecond=0)
    if first_slot < entry_dt:
        first_slot += HOUR
    slots = max(0, int((end - first_slot) // HOUR))
    covered = bool(candles) and candles[-1][0] + HOUR >= end and (
        len(candles) < CANDLE_LIMIT or candles[0][0] <= entry_dt)
    info = {"covered": covered, "window_hours": slots,
            "missing_hours": max(0, slots - len(win))}
    for _, _, h, l, _ in win:
        if l <= dn:
            return "STOP", None, info
        if h >= up:
            return "HIT", None, info
    if not covered:
        return "CENSORED", None, info
    closed = [c for c in candles if c[0] + HOUR <= end]
    return "TIMEOUT", (closed[-1][4] if closed else None), info


def build_sample(enrolled, by_ca, candles_dir, clock, lag_max):
    """Outcomes for pre-watermark enrolments within lag_max.

    Returns (tokens, counts): tokens is a list of dicts with outcome, info,
    ratio and cost; counts tallies every exclusion and drop by reason."""
    counts = Counter()
    tokens = []
    for e in enrolled:
        if parse(e["enrolled_at"]) >= WATERMARK:
            continue
        counts["pre-watermark enrolments"] += 1
        if lag(e) > lag_max:
            counts["excluded: lag > limit"] += 1
            continue
        ent = entry_row(by_ca.get(e["ca"], []), parse(e["enrolled_at"]))
        if ent is None:
            counts["dropped: no entry row"] += 1
            continue
        price = ent.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            counts["dropped: entry price not > 0"] += 1
            continue
        candles = load_candles(os.path.join(candles_dir, f"{e['main_pool']}.json"))
        if clock < parse(e["first_seen"]) + WINDOW or candles is None:
            counts["immature (excluded, not censored)"] += 1
            continue
        ratio = entry_ratio(price, candles, ent["dt"])
        if ratio is None:
            counts["dropped: no candle closed at or before entry"] += 1
            continue
        if not RATIO_LO <= ratio <= RATIO_HI:
            counts["dropped: entry/candle ratio outside [0.5, 2]"] += 1
            tokens.append({"ratio": ratio, "outcome": None})
            continue
        out, _, info = outcome(e, ent["dt"], price, candles)
        tokens.append({"ratio": ratio, "outcome": out, "info": info,
                       "cost": cost(ent), "lag_h": lag(e) / HOUR})
    return tokens, counts


# ------------------------------------------------------------------ power

def shares(tokens):
    n = len(tokens)
    c = Counter(t["outcome"] for t in tokens)
    return {k: c[k] / n for k in OUTCOMES} if n else None


def shifted(p, delta_pp):
    """p with the evaluable (non-censored) hit share raised by delta_pp,
    taken from STOP and TIMEOUT in proportion; CENSORED unchanged.
    Returns (p', capped)."""
    ev = 1 - p["CENSORED"]
    q = p["HIT"] / ev + delta_pp / 100
    capped = q > 1
    q = min(q, 1.0)
    rest_old = p["STOP"] + p["TIMEOUT"]
    rest_new = ev * (1 - q)
    k = rest_new / rest_old if rest_old else 0.0
    return ({"HIT": q * ev, "STOP": p["STOP"] * k, "TIMEOUT": p["TIMEOUT"] * k,
             "CENSORED": p["CENSORED"]}, capped)


def draw_hit_share(rng, n, p):
    """Evaluable hit share in one arm of n tokens, or None if all censored."""
    k = n - rng.binomialvariate(n, p["CENSORED"])
    if k == 0:
        return None
    ev = 1 - p["CENSORED"]
    return rng.binomialvariate(k, min(1.0, p["HIT"] / ev)) / k


def pass_rate(p_a, p_b, b, n_a, n_b, reps, rng):
    """Share of draws where arm A hit share >= b and > arm B hit share."""
    hits = 0
    for _ in range(reps):
        ha, hb = draw_hit_share(rng, n_a, p_a), draw_hit_share(rng, n_b, p_b)
        if ha is not None and hb is not None and ha >= b and ha > hb:
            hits += 1
    return hits / reps


def power_table(p, b):
    """{delta_pp: {n_a: rate}} and the deltas that hit the 100% cap."""
    table, capped = {}, []
    for d in DELTAS:
        p_a, cap = shifted(p, d)
        if cap:
            capped.append(d)
        table[d] = {n: pass_rate(p_a, p, b, n, B_MULT * n, REPS,
                                 random.Random(f"{SEED}:{d}:{n}"))
                    for n in NS}
    return table, capped


# ------------------------------------------------------------------ report

def pct(x):
    return f"{100 * x:5.1f}%"


def quartiles(v):
    q = statistics.quantiles(v, n=4, method="inclusive")
    return q[0], statistics.median(v), q[2]


def report_sample(name, tokens, counts):
    """Prints checks, the pooled split, costs and power for one sample.
    Returns the number of tokens with an outcome."""
    print(f"\n=== {name} ===")
    for k, v in counts.items():
        print(f"  {k:48s} {v:4d}")
    ratios = [t["ratio"] for t in tokens]
    if ratios:
        print(f"  unit check: median entry/candle price ratio {statistics.median(ratios):.4f}"
              f" over {len(ratios)} tokens")
    ev = [t for t in tokens if t["outcome"]]
    print(f"  tokens with an outcome: {len(ev)}")
    if not ev:
        print("  no outcomes; nothing further computed")
        return 0
    cov = Counter(t["info"]["covered"] for t in ev)
    wh = sum(t["info"]["window_hours"] for t in ev)
    mh = sum(t["info"]["missing_hours"] for t in ev)
    print(f"  coverage: window covered {cov[True]}, not covered {cov[False]};"
          f" hour slots {wh}, slots with no candle {mh} ({pct(mh / wh) if wh else 'n/a'})")
    lags = [t["lag_h"] for t in ev]
    print(f"  window length: {7 * 24 - max(lags):.1f} to {7 * 24 - min(lags):.1f} h"
          " (first_seen + 7 d minus entry, approx.)")
    p = shares(ev)
    c = Counter(t["outcome"] for t in ev)
    print("  POOLED OUTCOME SPLIT (never split by any predictor):")
    for k in OUTCOMES:
        print(f"    {k:9s} {c[k]:4d}  {pct(p[k])}")
    if p["CENSORED"] < 1:
        print(f"    evaluable hit share (censored excluded): {pct(p['HIT'] / (1 - p['CENSORED']))}")
    costs = [t["cost"] for t in ev if t["cost"] is not None]
    if len(costs) < len(ev):
        print(f"  cost unknown (entry liq not > 0): {len(ev) - len(costs)}")
    if len(costs) < 2:
        print("  fewer than 2 costs; break-even and power not computed")
        return len(ev)
    q1, med, q3 = quartiles(costs)
    b = (0.5 + med) / 1.5
    print(f"  round-trip cost c at {SIZE} USD: median {pct(med)}, IQR {pct(q1)} to {pct(q3)}")
    print(f"  break-even hit share b = (0.5 + {med:.4f}) / 1.5 = {pct(b)}")
    if p["CENSORED"] >= 1:
        print("  every token censored; power not computable")
        return len(ev)
    table, capped = power_table(p, b)
    print(f"  POWER: pass = A hit share >= b AND > B hit share; censored excluded;"
          f" B = {B_MULT}x A; {REPS} draws, seed {SEED}")
    print("    arm A n    " + "".join(f"{n:>8d}" for n in NS))
    for d in DELTAS:
        lab = "false pass " if d == 0 else f"detect +{d:2d}pp"
        print(f"    {lab:11s}" + "".join(f"{pct(table[d][n]):>8s}" for n in NS))
    if capped:
        print(f"    (arm A hit share capped at 100% for +{capped} pp)")
    return len(ev)


def report_arms(enrolled, by_ca):
    """Tier counts at entry over the whole enrolled set (predictor side)."""
    tiers, missing = Counter(), 0
    for e in enrolled:
        ent = entry_row(by_ca.get(e["ca"], []), parse(e["enrolled_at"]))
        if ent is None:
            missing += 1
        else:
            tiers[ent.get("tier")] += 1
    print(f"\n=== ARM SIZES at entry, whole enrolled set ({len(enrolled)} tokens) ===")
    for t, v in sorted(tiers.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  tier {str(t):10s} {v:4d}")
    print(f"  no entry row    {missing:4d}")
    a = tiers[BEST]
    print(f"  arm A (BEST) {a}, arm B (other) {sum(tiers.values()) - a}")


def report_pace(enrolled, by_ca, clock):
    since = [e for e in enrolled if parse(e["enrolled_at"]) >= PACE_FROM]
    days = (clock - PACE_FROM) / timedelta(days=1)
    short = [e for e in since if lag(e) <= LAG_MAX]
    print(f"\n=== PACE since {PACE_FROM.date()} (clock: latest watch run {clock.isoformat()}) ===")
    print(f"  enrolments {len(since)} over {days:.2f} days = {len(since) / days:.2f}/day")
    print(f"  with lag <= 24 h: {len(short)} ({len(short) / days:.2f}/day)")
    for name, group in (("all since", since), ("lag <= 24 h", short)):
        rows = [entry_row(by_ca.get(e["ca"], []), parse(e["enrolled_at"])) for e in group]
        tiers = Counter(r.get("tier") for r in rows if r is not None)
        print(f"  tiers at entry, {name}: "
              + ", ".join(f"{t} {v}" for t, v in sorted(tiers.items(), key=lambda kv: -kv[1]))
              + f"; no entry row {sum(1 for r in rows if r is None)}")
    found = [r for r in (entry_row(by_ca.get(e["ca"], []), parse(e["enrolled_at"]))
                         for e in short) if r is not None]
    a = sum(1 for r in found if r.get("tier") == BEST)
    if not found or not a:
        print("  arm A share at entry (lag <= 24 h): 0; no arm A n is reached at this pace")
        return
    rate_a = a / days
    print(f"  arm A share at entry (lag <= 24 h, entry found): {a}/{len(found)} = {pct(a / len(found))}")
    print(f"  arm A rate {rate_a:.2f}/day; registered {REGISTER.date()}:")
    print("    arm A n   enrolled by   window ends by (+7 d)")
    for n in NS:
        t = REGISTER + timedelta(days=n / rate_a)
        print(f"    {n:7d}   {t.date()}    {(t + WINDOW).date()}")


def main():
    fol = os.path.join(ROOT, "data", "followup")
    with open(os.path.join(fol, "enrolled.json"), encoding="utf-8") as f:
        enrolled = json.load(f)
    clock = watch_clock(os.path.join(fol, "watch"))
    by_ca = load_log({e["ca"] for e in enrolled}, os.path.join(ROOT, "data", "log"))
    candles_dir = os.path.join(fol, "candles")
    print("HS-92 power check, candidate NOT registered. Sizing sample: enrolled_at <"
          f" {WATERMARK.isoformat()} (H-0008 watermark), mature at the clock.")
    print(f"Cost model: label.py nets(), fee fallback {label.FEE_FALLBACK},"
          f" liq_out = liq_in, {SIZE} USD.")
    tokens, counts = build_sample(enrolled, by_ca, candles_dir, clock, LAG_MAX)
    n = report_sample("SIZING SAMPLE: lag <= 24 h", tokens, counts)
    if n < MIN_PRIMARY:
        tokens, counts = build_sample(enrolled, by_ca, candles_dir, clock, SENS_LAG)
        report_sample("SENSITIVITY ONLY: lag <= 96 h (window 3 to 7 days)", tokens, counts)
    report_arms(enrolled, by_ca)
    report_pace(enrolled, by_ca, clock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
