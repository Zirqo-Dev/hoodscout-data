"""HS-91 gate 3, option B: ONE pooled DEAD_72H base rate (report only).

Blind by construction: every outcome is printed as a pooled total. Nothing is
split, grouped or sorted by entry liquidity, tier or anything else, and no
per-token row is printed. The only per-arm figures are arm SIZES (eligible
tokens with entry liq >= 50k vs below), never arm outcomes.

Reads data/followup/enrolled.json, data/followup/watch/ and, from data/log,
only ts, ca, liq and price of enrolled tokens. Never data/holders.

Rule, as given for HS-91 option B:
  population  data/followup/enrolled.json, enrolled_at before this run
  entry       the token's last data/log row at or before enrolled_at, within 30 min
  eligible    entry liq > 0, enrolled_at - first_seen <= 96 h,
              entry + 72 h <= the latest watch run
  window      watch rows for the frozen main_pool with ts in (entry, entry + 72 h]
  known       attributes.reserve_in_usd parses as a number
  dead point  (L) reserve < max(1000, 0.10 * entry liq), unless data/log has a
              row for that ca within +/-1 h with liq >= the same bar (migration)
              (P) base_token_price_usd <= 0.10 * entry price, standing alone
  DEAD_72H    two consecutive watch points (consecutive known rows in the
              window; an unknown row breaks the run) both dead
  censored    fewer than 12 known points in the window

Usage: python analysis/hs91/pooled_rate.py [REPORT.md]
"""
import bisect, glob, gzip, json, math, os, sys
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
FOLLOWUP = os.path.join(ROOT, "data", "followup")
LOG_DIR = os.path.join(ROOT, "data", "log")

ENTRY_TOL = timedelta(minutes=30)
MAX_ENROL_LAG = timedelta(hours=96)
HORIZON = timedelta(hours=72)
MIG_TOL = timedelta(hours=1)
LIQ_ABS, LIQ_REL, PRICE_REL = 1000.0, 0.10, 0.10
MIN_KNOWN = 12
FLOOR = 50_000
GAPS = (-10, -15, -20)
ALPHA, POWER = 0.05, 0.80
PACE_FROM = datetime(2026, 10, 24, tzinfo=timezone.utc)
READ_DATES = (datetime(2026, 12, 7, tzinfo=timezone.utc),
              datetime(2026, 12, 14, tzinfo=timezone.utc),
              datetime(2027, 1, 11, tzinfo=timezone.utc))


def parse_ts(s):
    return datetime.fromisoformat(s)


def num(x):
    """float(x) if x parses as a finite number, else None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------------ loading

def load_enrolled(now):
    with open(os.path.join(FOLLOWUP, "enrolled.json"), encoding="utf-8") as f:
        return [e for e in json.load(f) if parse_ts(e["enrolled_at"]) < now]


def load_log(cas):
    """{ca: ([dt], [(dt, liq, price)])} for the given cas, fields ts/ca/liq/price only."""
    seen, out = set(), {}
    for p in sorted(glob.glob(os.path.join(LOG_DIR, "*", "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    ca, ts = r["ca"], r["ts"]
                except (ValueError, KeyError):
                    continue
                if ca not in cas or (ts, ca) in seen:
                    continue
                seen.add((ts, ca))
                out.setdefault(ca, []).append((parse_ts(ts), num(r.get("liq")), num(r.get("price"))))
    res = {}
    for ca, v in out.items():
        v.sort(key=lambda t: t[0])
        res[ca] = ([t[0] for t in v], v)
    return res


def load_watch(keys):
    """({(ca, pool): [(dt, reserve|None, price|None)]}, latest run dt)."""
    out, latest = {}, None
    base = os.path.join(FOLLOWUP, "watch")
    for p in sorted(glob.glob(os.path.join(base, "*", "*.meta.json"))):
        with open(p, encoding="utf-8") as f:
            t = parse_ts(json.load(f)["run_ts"])
        latest = t if latest is None or t > latest else latest
    for p in sorted(glob.glob(os.path.join(base, "*", "*.jsonl.gz"))):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                k = (r.get("ca"), r.get("pool"))
                if k not in keys:
                    continue
                a = r.get("attributes") or {}
                dt = parse_ts(r["ts"])
                latest = dt if latest is None or dt > latest else latest
                out.setdefault(k, []).append(
                    (dt, num(a.get("reserve_in_usd")), num(a.get("base_token_price_usd"))))
    for v in out.values():
        v.sort(key=lambda t: t[0])
    return out, latest


# ------------------------------------------------------------------ rule

def entry_row(log, enrolled_at):
    """Last log row at or before enrolled_at, within ENTRY_TOL, or None."""
    if log is None:
        return None
    times, rows = log
    i = bisect.bisect_right(times, enrolled_at)
    if i == 0 or enrolled_at - times[i - 1] > ENTRY_TOL:
        return None
    return rows[i - 1]


def window_points(watch_rows, entry_dt):
    """Watch rows with ts in (entry, entry + 72 h], one per ts (first kept)."""
    out, seen = [], set()
    for t, res, price in watch_rows:
        if entry_dt < t <= entry_dt + HORIZON and t not in seen:
            seen.add(t)
            out.append((t, res, price))
    return out


def migrated(log, t, bar):
    """data/log has a row within +/-1 h of t with liq >= bar."""
    if log is None:
        return False
    times, rows = log
    i, j = bisect.bisect_left(times, t - MIG_TOL), bisect.bisect_right(times, t + MIG_TOL)
    return any(r[1] is not None and r[1] >= bar for r in rows[i:j])


def point_flags(pt, entry_liq, entry_price, log):
    """(L, P) for one known watch point; (False, False) for an unknown one."""
    t, res, price = pt
    if res is None:
        return False, False
    bar = max(LIQ_ABS, LIQ_REL * entry_liq)
    lq = res < bar and not migrated(log, t, bar)
    pr = price is not None and entry_price is not None and entry_price > 0 \
        and price <= PRICE_REL * entry_price
    return lq, pr


def classify(points, entry_liq, entry_price, log):
    """('censored', reason) | ('alive', None) | ('dead', clause).

    clause: 'L' if only an L-L pair exists, 'P' if only a P-P pair, 'both'
    if both exist, 'mixed' if the only dead pairs mix one L and one P point."""
    known = sum(p[1] is not None for p in points)
    if known < MIN_KNOWN:
        if not points:
            return "censored", "no watch rows in window"
        if len(points) < MIN_KNOWN:
            return "censored", "fewer than 12 watch rows in window"
        return "censored", "12+ rows but fewer than 12 with a numeric reserve"
    flags = [point_flags(p, entry_liq, entry_price, log) for p in points]
    ll = pp = any_pair = False
    for (l1, p1), (l2, p2) in zip(flags, flags[1:]):
        if (l1 or p1) and (l2 or p2):
            any_pair = True
            ll |= l1 and l2
            pp |= p1 and p2
    if not any_pair:
        return "alive", None
    return "dead", ("both" if ll and pp else "L" if ll else "P" if pp else "mixed")


# ------------------------------------------------------------------ stats

def wilson(k, n, z=1.959963984540054):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def n_per_arm(p_center, gap, alpha=ALPHA, power=POWER):
    """Two-proportion normal approximation, one-sided. Arms at p_center -/+ |gap|/2.
    None if either arm rate leaves (0, 1)."""
    d = abs(gap)
    p1, p2 = p_center + d / 2, p_center - d / 2
    if not (0 < p2 and p1 < 1):
        return None
    pb = (p1 + p2) / 2
    za, zb = NormalDist().inv_cdf(1 - alpha), NormalDist().inv_cdf(power)
    n = (za * math.sqrt(2 * pb * (1 - pb)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / d ** 2
    return math.ceil(n)


def critical_gap_pp(p, n, alpha=ALPHA):
    """One-sided rejection cutoff for the gap at n per arm under no true gap."""
    return -100 * NormalDist().inv_cdf(1 - alpha) * math.sqrt(2 * p * (1 - p) / n)


# ------------------------------------------------------------------ run

def run(now):
    enrolled = load_enrolled(now)
    cas = {e["ca"] for e in enrolled}
    log = load_log(cas)
    watch, latest = load_watch({(e["ca"], e["main_pool"]) for e in enrolled})
    c = {"enrolled": len(enrolled), "no_entry": 0, "liq": 0, "lag": 0, "immature": 0,
         "eligible": 0, "censored": 0, "evaluable": 0, "dead": 0}
    reasons, clauses = {}, {"L": 0, "P": 0, "both": 0, "mixed": 0}
    arms = {"hi": 0, "lo": 0}
    shape = {"since": 0, "since_pass": 0}   # enrolments since PACE_FROM, and those passing entry/liq/lag
    for e in enrolled:
        ea, fs = parse_ts(e["enrolled_at"]), parse_ts(e["first_seen"])
        since = ea >= PACE_FROM
        shape["since"] += since
        lg = log.get(e["ca"])
        ent = entry_row(lg, ea)
        if ent is None:
            c["no_entry"] += 1
            continue
        t0, liq0, price0 = ent
        if not (liq0 is not None and liq0 > 0):
            c["liq"] += 1
            continue
        if ea - fs > MAX_ENROL_LAG:
            c["lag"] += 1
            continue
        shape["since_pass"] += since
        if latest is None or t0 + HORIZON > latest:
            c["immature"] += 1
            continue
        c["eligible"] += 1
        arms["hi" if liq0 >= FLOOR else "lo"] += 1
        status, why = classify(window_points(watch.get((e["ca"], e["main_pool"]), []), t0),
                               liq0, price0, lg)
        if status == "censored":
            c["censored"] += 1
            reasons[why] = reasons.get(why, 0) + 1
            continue
        c["evaluable"] += 1
        if status == "dead":
            c["dead"] += 1
            clauses[why] += 1
    return c, reasons, clauses, arms, shape, latest


def report(now, c, reasons, clauses, arms, shape, latest):
    L = []
    w = L.append
    pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"
    w("# HS-91 gate 3, option B: pooled DEAD_72H base rate (report only)")
    w("")
    w(f"Run at {now.isoformat(timespec='seconds')}; latest watch run "
      f"{latest.isoformat(timespec='seconds') if latest else 'none'}. "
      "Pooled totals only: no outcome is split by entry liquidity, tier or anything else.")
    w("The candidate hypothesis is NOT registered. Nothing here decides anything.")
    w("")
    w("## 1. Counts")
    w("")
    matched = c["enrolled"] - c["no_entry"]
    w(f"- Enrolled (enrolled_at before this run): {c['enrolled']}")
    w(f"- Entry matched (data/log row at or before enrolled_at, within 30 min): {matched}"
      f" (dropped, no entry row: {c['no_entry']})")
    w(f"- Dropped, entry liq not > 0: {c['liq']}")
    w(f"- Dropped, enrolled_at - first_seen > 96 h: {c['lag']}")
    w(f"- Dropped, entry + 72 h after the latest watch run (not yet mature): {c['immature']}")
    w(f"- Eligible: {c['eligible']}")
    w(f"- Censored (fewer than 12 known points): {c['censored']}")
    for k in sorted(reasons):
        w(f"  - {k}: {reasons[k]}")
    w(f"- Evaluable: {c['evaluable']}")
    w(f"- Dead: {c['dead']}")
    w("")
    w("## 2. Pooled DEAD_72H rate")
    w("")
    p = c["dead"] / c["evaluable"] if c["evaluable"] else None
    lo, hi = wilson(c["dead"], c["evaluable"])
    w(f"Pooled rate: {c['dead']} / {c['evaluable']} = {pct(p)} (95% Wilson {pct(lo)} to {pct(hi)}).")
    w(f"Deaths by clause (pooled): L only {clauses['L']}, P only {clauses['P']}, "
      f"both {clauses['both']}, mixed L/P pair only {clauses['mixed']}.")
    w("")
    w("## 3. Arm sizes only (eligible tokens; no outcomes)")
    w("")
    tot = arms["hi"] + arms["lo"]
    share_hi = arms["hi"] / tot if tot else None
    w(f"Entry liq >= 50k: {arms['hi']}. Below 50k: {arms['lo']}. "
      f"Share >= 50k: {pct(share_hi)}.")
    w("")
    w("## 4. Sizing (analytic)")
    w("")
    w("Two-proportion normal approximation, alpha 0.05 one-sided, power 0.80. The two "
      "arms sit at p -/+ |gap|/2 around the centre p. n is EVALUABLE tokens per arm.")
    w("")
    w("| centre p | gap -10 pp | gap -15 pp | gap -20 pp |")
    w("|---|---|---|---|")
    need = {}
    if p is not None:
        for lab, pc in (("pooled - 10 pp", p - .10), ("pooled", p), ("pooled + 10 pp", p + .10)):
            ns = [n_per_arm(pc, g / 100) for g in GAPS]
            if lab == "pooled + 10 pp":
                need = dict(zip(GAPS, ns))
            w(f"| {lab} ({pct(pc)}) | " + " | ".join("n/a" if n is None else str(n) for n in ns) + " |")
        w("")
        w("n/a: an arm rate would leave (0, 1).")
    w("")
    w("## 5. Pace")
    w("")
    days = (latest - PACE_FROM).total_seconds() / 86400 if latest else 0
    rate = shape["since"] / days if days > 0 else 0.0
    pass_share = shape["since_pass"] / shape["since"] if shape["since"] else 0.0
    cens_share = c["censored"] / c["eligible"] if c["eligible"] else 0.0
    w(f"New enrolments since 2026-10-24: {shape['since']} over {days:.2f} days = "
      f"{rate:.2f} per day. Share passing entry match, liq and 96 h rules: {pct(pass_share)}. "
      f"Pooled censored share among eligible: {pct(cens_share)}.")
    w("Projection = evaluable today + per-day rate x pass share x (1 - censored share) x "
      "days from the latest watch run to (read date - 72 h), split by the item 3 share.")
    w("")
    w("| read date | projected evaluable total | >= 50k | < 50k |")
    w("|---|---|---|---|")
    proj = {}
    for d in READ_DATES:
        mature_days = max(0.0, ((d - HORIZON) - latest).total_seconds() / 86400) if latest else 0
        total = c["evaluable"] + rate * pass_share * (1 - cens_share) * mature_days
        h = total * share_hi if share_hi is not None else 0
        proj[d] = (total, h, total - h)
        w(f"| {d.date()} (Mon) | {total:.0f} | {h:.0f} | {total - h:.0f} |")
    w("")
    w("## 6. Suggestion for the owner (not a decision)")
    w("")
    w(line(p + .10 if p is not None else None, need, proj))
    return "\n".join(L) + "\n"


def line(p, need, proj):
    """The smallest gap (in |pp|) whose n is met by the smaller projected arm
    at one of READ_DATES, at the earliest such date. Called with the
    conservative centre (pooled + 10 pp) and its n, since the pooled rate
    rests on few deaths."""
    head = "SUGGESTION (for the owner, not a decision): "
    if p is None or not need:
        return head + "no evaluable tokens yet; no threshold, min n or read date can be sized."
    for g in sorted(GAPS, key=abs):
        n = need.get(g)
        if n is None:
            continue
        for d in READ_DATES:
            if min(proj[d][1], proj[d][2]) >= n:
                return head + (f"size for a {g} pp gap: min {n} evaluable tokens per arm, "
                               f"read on {d.date()}; sized at the conservative centre "
                               f"{100 * p:.1f}% (pooled + 10 pp), where the one-sided 0.05 "
                               f"cutoff at that n is a gap <= {critical_gap_pp(p, n):.1f} pp.")
    return head + ("no tested gap reaches its min n in the smaller projected arm by "
                   f"{READ_DATES[-1].date()}; the < 50k / >= 50k split is the bottleneck.")


def main():
    now = datetime.now(timezone.utc)
    out = sys.argv[1] if len(sys.argv) > 1 else None
    text = report(now, *run(now))
    print(text, end="")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
