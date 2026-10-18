#!/usr/bin/env python3
"""Collect Robinhood Chain pool data, aggregate by token, score."""
import json, math, os, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

import stocks   # STOCKS is imported, never duplicated, so the two cannot drift

NETWORK = "robinhood"
BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.2"}

MIN_TXNS_24H   = 300     # "real activity"
MIN_SCORE_TXNS = 200     # below this: no score at all
MIN_AGE_DEPTH  = 1.0     # days before liq/FDV means anything
MAX_AGE_DAYS   = 14
HISTORY_DAYS   = 15     # halved with the move to */15 to hold file size flat

# tier thresholds.
# Persistence is measured in wall clock, not snapshots. A token qualifies when
# its healthy appearances inside the last PERSIST_LOOKBACK_H span at least
# PERSIST_SPAN_H, and cover at least PERSIST_MIN_FRAC of the snapshots that
# actually landed in that window.
#
# What this replaces: "healthy in >= PERSIST_MIN of the last PERSIST_RUNS
# snapshots", which was silently a function of the cron. At */30 ten of sixteen
# asked for roughly 5h of span, at */15 for roughly 2.5h, so halving the poll
# interval halved the bar without a line changing. Worse, it is unsatisfiable
# rather than merely loose when the file is short: after the 15-day pause left
# four snapshots in history.jsonl, no token could reach ten and BEST went to
# zero. Both bars below are cadence-invariant -- one is hours, the other a
# fraction of whatever landed -- so neither moves when the cron does, and a
# fraction of four is still two. Replayed over 09-18..10-02 against 9,680
# stored BEST prints, BEST per snapshot comes out:
#
#   cadence   stored   this rule   count 10/16   fixed >=6 in 6h
#   */30        18.8      18.4         18.3           18.4
#   */60        18.6      18.0         17.4           17.3
#   */90        18.5      17.8         16.5            0.0
#
# -- the same answer at the cadence that produced the data, and the only one of
# the three that still holds when the spacing triples. A fixed in-window
# minimum is no better than a snapshot count: at */90 only five snapshots fit a
# 6h window, so a minimum of six can never be met.
#
# PERSIST_MIN_OBS is a floor under the count the fraction asks for, not a cap:
# need = max(PERSIST_MIN_OBS, ceil(PERSIST_MIN_FRAC * snapshots_in_window)). It
# binds only when the window is thin, which is precisely when the span test
# stops meaning anything. At the current four runs an hour a 6h window holds up
# to 24 prints and the fraction asks for 15, so this floor is inert; after a
# delivery gap leaves two snapshots five hours apart, the fraction asks for
# two, and a token seen at both ends of the gap and nowhere in between clears a
# "4h of span" bar having been observed twice. Eight is roughly two hours of
# real observations at the current cadence, so that token now fails -- and with
# it the whole class of tokens that are only visible either side of an outage.
# Measured on the live file (25 snapshots, 2026-10-18T09:00Z): identical sets
# at 2 and at 8, 41 tokens each; on a window reduced to two snapshots 4.68h
# apart, 40 tokens pass at 2 and 0 at 8.
#
# PERSIST_RUNS and PERSIST_MIN are kept for one week of real delivery data, so
# the old gate can be recomputed alongside the new one if the two disagree in
# production; nothing reads them now.
PERSIST_LOOKBACK_H = 6.0   # window the appearances are counted in
PERSIST_SPAN_H     = 4.0   # earliest to latest healthy appearance must cover this
PERSIST_MIN_FRAC   = 0.60  # of the snapshots that landed inside the window
PERSIST_MIN_OBS    = 8     # floor on that count; see the note above
PERSIST_RUNS     = 16    # retired: snapshots looked back over for BEST
PERSIST_MIN      = 10    # retired: of which this many had to show it healthy
BEST_MIN_AGE     = 2.0
BEST_MIN_LIQ_FDV = 5.0
BEST_MAX_TPT     = 4.0
EMERGING_MAX_AGE = 3.0
EMERGING_MAX_TPT = 5.0   # looser than BEST: early trading is concentrated
DEAD_TXNS_24H    = 10    # below this, a token past MAX_AGE_DAYS is inactive

# recent move: strictly additive, never an input to screen_score or tier
RECENT_MOVE_MIN_AGE_H = 30.0  # 24h byr24 window + the 6h trailing window
PRICE_BANDS = ((5.0, 5), (2.0, 3), (0.5, 1))      # chg_h6 %
ACCEL_BANDS = ((50.0, 5), (20.0, 3), (5.0, 1))    # buyer_accel %

# Chain stablecoins are quote assets, not anything to discover, and they sit
# close enough to the depth and concentration thresholds to keep resurfacing.
# Addresses are confirmed live before being added here, never from memory:
# 0x5fc5... reads name() 'Global Dollar', symbol() 'USDG', 6 decimals, trading
# at $0.9997 across 20 pools since 2026-06-29.
STABLECOINS = {
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",   # USDG / Global Dollar
}
TRENDING_PAGES = 2
NEW_POOL_PAGES = 3

# Deep counterparties are invisible to trending and new_pools, and to symbol
# search: /search/pools?query=BONER returns four ~$3K impostors and misses the
# real Boner Coin entirely, which holds $2.7M against HIMS and $1.9M against AI.
# Pools are fetched for these tokens directly instead.
EXTRA_POOL_LOOKUPS = {
    "0x98096d17e191b3da1d5f99a6d7b3584351b11e18",   # BONER / 'Boner Coin'
}
POOL_LOOKUP_CAS = {c.lower() for c in stocks.STOCKS.values()} | EXTRA_POOL_LOOKUPS


def get(path, tries=4):
    """GeckoTerminal rate-limits the free tier. Without backoff a 429 aborted
    the call and was logged as a warning, which reads as "this token has no
    pools" rather than "we were throttled" -- and the direct lookups run at
    :00/:30, the same minutes stocks.py polls the same endpoints. Same backoff
    as verify_stocks.gt()."""
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            wait = 20 * (i + 1)
            print(f"  429 on {path}, retrying in {wait}s")
            time.sleep(wait)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect():
    pools, seen = [], set()
    paths = ([f"/networks/{NETWORK}/trending_pools?page={i}" for i in range(1, TRENDING_PAGES+1)]
           + [f"/networks/{NETWORK}/new_pools?page={i}" for i in range(1, NEW_POOL_PAGES+1)])
    for p in paths:
        try:
            data = get(p).get("data", [])
        except Exception as e:
            print(f"WARN {p}: {e}")
            continue
        for item in data:
            addr = (item.get("attributes") or {}).get("address")
            if addr and addr not in seen:
                seen.add(addr)
                pools.append(item)
        time.sleep(2.5)

    # Outcome per address is returned and written to latest.json: a lookup that
    # silently returns nothing is indistinguishable in the output from a token
    # with no pools, and these six went missing for days before anyone noticed.
    lookups = []
    for ca in sorted(POOL_LOOKUP_CAS):
        try:
            data = get(f"/networks/{NETWORK}/tokens/{ca}/pools").get("data", [])
        except Exception as e:
            print(f"WARN pools {ca}: {e}")
            lookups.append({"ca": ca, "ok": False, "pools_returned": 0,
                            "pools_new": 0, "error": f"{type(e).__name__}: {e}"})
            continue
        added = 0
        for item in data:
            addr = (item.get("attributes") or {}).get("address")
            if addr and addr not in seen:
                seen.add(addr)
                pools.append(item)
                added += 1
        print(f"lookup {ca[:10]}: {len(data)} pools, {added} new")
        lookups.append({"ca": ca, "ok": True, "pools_returned": len(data),
                        "pools_new": added, "error": None})
        time.sleep(2.5)
    return pools, lookups


def parse_pool(item, now):
    a = item.get("attributes") or {}
    rel = item.get("relationships") or {}
    tx = a.get("transactions") or {}
    t24, t6 = tx.get("h24") or {}, tx.get("h6") or {}
    pc = a.get("price_change_percentage") or {}
    vol = a.get("volume_usd") or {}

    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    ca = base_id.split("_", 1)[1].lower() if "_" in base_id else None

    age = None
    if a.get("pool_created_at"):
        try:
            t = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
            age = round((now - t).total_seconds() / 86400, 2)
        except ValueError:
            pass

    name = a.get("name") or ""
    return {
        "ca": ca, "name": name,
        "symbol": name.split("/")[0].strip() if "/" in name else name,
        "quote": name.split("/")[1].strip().split()[0] if "/" in name else None,
        "pool": a.get("address"),
        "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
        "price": num(a.get("base_token_price_usd")),
        "fdv": num(a.get("fdv_usd")),
        "liq": max(0.0, num(a.get("reserve_in_usd")) or 0.0),
        "vol24": num(vol.get("h24")) or 0.0,
        "b24": t24.get("buys") or 0, "s24": t24.get("sells") or 0,
        "byr24": t24.get("buyers") or 0, "slr24": t24.get("sellers") or 0,
        "b6": t6.get("buys") or 0, "s6": t6.get("sells") or 0,
        "chg6": num(pc.get("h6")), "chg24": num(pc.get("h24")),
        "age": age,
    }


def aggregate(pools):
    by = {}
    for p in pools:
        if p["ca"] and p["pool"]:
            by.setdefault(p["ca"], []).append(p)

    out = []
    for ca, ps in by.items():
        deep = max(ps, key=lambda x: x["liq"])       # deepest pool = best price reference
        ages = [x["age"] for x in ps if x["age"] is not None]
        out.append({
            "symbol": deep["symbol"], "ca": ca, "pool_count": len(ps),
            "main_pool": deep["pool"], "main_dex": deep["dex"], "main_quote": deep["quote"],
            "price_usd": deep["price"], "fdv_usd": deep["fdv"],
            "liq_usd": round(sum(x["liq"] for x in ps), 2),
            "vol24_usd": round(sum(x["vol24"] for x in ps), 2),
            "buys24": sum(x["b24"] for x in ps), "sells24": sum(x["s24"] for x in ps),
            "buyers24": sum(x["byr24"] for x in ps), "sellers24": sum(x["slr24"] for x in ps),
            "buys6": sum(x["b6"] for x in ps), "sells6": sum(x["s6"] for x in ps),
            "chg_h6": deep["chg6"], "chg_h24": deep["chg24"],
            "age_days": max(ages) if ages else None,
            "gt_url": f"https://www.geckoterminal.com/{NETWORK}/pools/{deep['pool']}",
        })
    return out


def score_token(t):
    txns = t["buys24"] + t["sells24"]
    traders = t["buyers24"] + t["sellers24"]
    t["txns24"], t["traders24"] = txns, traders
    t["trades_per_trader"] = round(txns / traders, 2) if traders else None

    liq, fdv, age = t["liq_usd"], t["fdv_usd"], t["age_days"]
    lf = round(100 * liq / fdv, 2) if liq and fdv else None
    t["liq_fdv_pct"] = lf
           
    # anomaly if liq >= FDV, unmeasurable, or dust
    t["flag_liq_anomaly"] = bool(lf is None or lf >= 90 or liq < 1000)

    # buy-count skew disagreeing with price direction
    t["flag_contradiction"] = bool(
        t["buys6"] > t["sells6"] * 1.2 and t["chg_h6"] is not None and t["chg_h6"] < 0)

    if txns < MIN_SCORE_TXNS:
        t["screen_score"], t["score_basis"] = None, "unscored: too few trades"
        return t

    tpt = t["trades_per_trader"]
    flow = 10 if (tpt is not None and tpt <= 4) else 5 if (tpt is not None and tpt <= 8) else 1

    if age is None or age < MIN_AGE_DEPTH or t["flag_liq_anomaly"]:
        t["screen_score"] = round(float(flow), 2)
        t["score_basis"] = "flow only (pool too young or liq/FDV implausible)"
    else:
        depth = 10 if lf >= 5 else 5 if lf >= 3 else 1
        t["screen_score"] = round(flow * 0.625 + depth * 0.375, 2)
        t["score_basis"] = "flow+depth"
    return t


def load_history(path="data/history.jsonl"):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def persistence(hist, now):
    """Which CAs are persistent, and how many healthy prints each has.

    Returns (ok, counts): a set of CAs clearing the bar, and the per-CA count of
    healthy appearances inside the window -- published as persist_count, so the
    number shown is the one the gate was decided on.

    The bar is PERSIST_SPAN_H of span and PERSIST_MIN_FRAC coverage of the
    snapshots that landed in the last PERSIST_LOOKBACK_H. Coverage is measured
    against what actually arrived, not against what the cron nominally promised,
    so dropped fires loosen the count in step with the span staying fixed.
    """
    cutoff = now - timedelta(hours=PERSIST_LOOKBACK_H)
    rows, stamps = [], set()
    for r in hist:
        ts = r.get("ts")
        if not ts or not r.get("ca"):
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt < cutoff or dt > now:
            continue
        stamps.add(dt)
        # only scored tokens are ever written to history, so presence is the
        # "scored" half of the test; rows predating the fattened format carry
        # no anomaly flag, and absent is read as healthy rather than dropping
        # the token — it self-corrects as new snapshots land
        if r.get("flag_liq_anomaly") is not True:
            rows.append((r["ca"], dt))

    need = max(PERSIST_MIN_OBS, math.ceil(PERSIST_MIN_FRAC * len(stamps)))
    seen = {}
    for ca, dt in rows:
        seen.setdefault(ca, set()).add(dt)
    ok, counts = set(), {}
    for ca, dts in seen.items():
        counts[ca] = len(dts)
        span = (max(dts) - min(dts)).total_seconds() / 3600
        if len(dts) >= need and span >= PERSIST_SPAN_H:
            ok.add(ca)
    return ok, counts


def buyer_rate_rising(ca, hist, window=5):
    """buyers24 strictly increasing across the last few snapshots for this CA.

    window counts snapshots, so it tracks the collect cadence: raised 3 -> 5
    when the cron went */30 -> */15, so the trend is still measured over about
    an hour rather than half of one. byr24 is a trailing 24h count and barely
    moves in 15 minutes, so too short a window tests noise, not direction."""
    rows = sorted([r for r in hist if r.get("ca") == ca and r.get("byr24") is not None],
                  key=lambda r: r.get("ts") or "")[-window:]
    if len(rows) < 2:
        return False
    vals = [r["byr24"] for r in rows]
    return all(b > a for a, b in zip(vals, vals[1:]))


def last_byr24(ca, hist, n=5):
    """The last n byr24 prints for a CA, oldest first.

    Published alongside persist_count so the two gates that depend on how much
    history exists can be read off latest.json. buyer_rate_rising tests exactly
    this list and, below `window` prints, tests fewer increases rather than
    failing -- so a thin history makes the EMERGING gate easier to pass, not
    harder, and nothing in the tier alone shows that."""
    rows = sorted([r for r in hist if r.get("ca") == ca and r.get("byr24") is not None],
                  key=lambda r: r.get("ts") or "")[-n:]
    return [r["byr24"] for r in rows]


def classify(t, avoid, persist_ok, hist):
    """Assign exactly one tier. Nothing with a real signal is dropped."""
    if t["ca"] in avoid:
        return "AVOID"

    contra, age = t["flag_contradiction"], t["age_days"]
    lf, tpt = t["liq_fdv_pct"], t["trades_per_trader"]

    if (t["ca"] in persist_ok and not contra
            and lf is not None and lf >= BEST_MIN_LIQ_FDV
            and tpt is not None and tpt <= BEST_MAX_TPT
            and age is not None and age >= BEST_MIN_AGE):
        return "BEST"

    if (age is not None and age <= EMERGING_MAX_AGE and not contra
            and tpt is not None and tpt <= EMERGING_MAX_TPT
            and buyer_rate_rising(t["ca"], hist)):
        return "EMERGING"

    if t["txns24"] >= MIN_TXNS_24H:
        return "WATCH"

    # no liquidity means no venue to trade against, however new the token is
    if t["liq_usd"] <= 0 or (age is not None and age > MAX_AGE_DAYS
                             and t["txns24"] < DEAD_TXNS_24H):
        return "DEAD"

    # thin, new or small, but tradeable: a fresh pool where liquidity still
    # approximates FDV lands here rather than being dropped as anomalous
    return "SPECULATIVE"


def watch_reason(t, persist_ok, hist):
    """Why a WATCH token is not in a higher tier. Labelling only — this reads
    the same conditions classify() uses and never changes a tier."""
    if t["flag_contradiction"]:
        return "contradiction"

    age, lf, tpt = t["age_days"], t["liq_fdv_pct"], t["trades_per_trader"]

    # would BEST or EMERGING have taken it but for trade concentration alone?
    best_but_tpt = (t["ca"] in persist_ok
                    and lf is not None and lf >= BEST_MIN_LIQ_FDV
                    and age is not None and age >= BEST_MIN_AGE
                    and (tpt is None or tpt > BEST_MAX_TPT))
    emerging_but_tpt = (age is not None and age <= EMERGING_MAX_AGE
                        and (tpt is None or tpt > EMERGING_MAX_TPT)
                        and buyer_rate_rising(t["ca"], hist))
    if best_but_tpt or emerging_but_tpt:
        return "tpt_high"

    # old enough and seen often enough to have been judged: what holds it back
    # is pool depth, which is a quality bar rather than a maturity one
    if (age is not None and age >= BEST_MIN_AGE
            and t["ca"] in persist_ok
            and (lf is None or lf < BEST_MIN_LIQ_FDV)):
        return "thin_depth"

    return "unproven"


def buyer_accel(ca, hist, recent_h=2.0, trailing_h=6.0):
    """Mean buyers24 over the last ~2h against the trailing ~2-6h, anchored to
    the token's own latest snapshot so a token that stopped reporting is not
    measured against wall-clock now. Windows are time-based because snapshot
    spacing is uneven. Ported from example_queries.sql, which held the only
    implementation of this comparison."""
    rows = [r for r in hist
            if r.get("ca") == ca and r.get("byr24") is not None and r.get("ts")]
    if not rows:
        return None
    try:
        anchor = datetime.fromisoformat(max(r["ts"] for r in rows))
    except ValueError:
        return None

    recent, trailing = [], []
    for r in rows:
        try:
            hrs = (anchor - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        except ValueError:
            continue
        if hrs <= recent_h:
            recent.append(r["byr24"])
        elif hrs <= trailing_h:
            trailing.append(r["byr24"])

    if not recent or not trailing:
        return None
    before = sum(trailing) / len(trailing)
    return round(100 * ((sum(recent) / len(recent)) / before - 1), 2) if before else None


def _band(v, bands):
    """Map a signed value onto a symmetric -5..+5 band. Bands rather than a
    continuous function because both inputs are noisy enough that a token's
    exact percentile would be false precision."""
    if v is None:
        return None
    for edge, pts in bands:
        if v >= edge:
            return pts
        if v <= -edge:
            return -pts
    return 0


def recent_move(t, hist):
    """How far a token has just moved, kept separate from whether it is a real
    market. Verification over the full history.jsonl found BEST tokens did no
    better than WATCH on the next print (median -0.45% vs -0.15%, win rate
    44.7% vs 45.1%), so tier cannot be read as a direction call. This is a
    second, independent description and is never folded back into screen_score
    or tier.

    Named for what it measures and nothing more. It was called momentum until a
    forward test over 6,656 scored rows (2026-09-14..17) showed the opposite of
    persistence: a positive score preceded *worse* returns than a negative one
    at 1h, 6h and 24h. At 6h the gap survived regime, token-clustering and
    paired within-token tests (positive beat the same token's negative prints
    in 22 of 79 cases), and roughly 85% of it was reproduced by chg_h6 alone --
    short-horizon mean reversion, not momentum. The acceleration term showed no
    independent forward signal at any horizon tested. Read this as "how far it
    just moved", never as a direction call; the flag that did read it that way
    was retired rather than inverted.

    Returns (score, basis, accel_pct). Score is price direction over 6h plus
    buyer-rate acceleration, each banded to +/-5, so -10..+10.
    """
    age = t.get("age_days")
    age_h = age * 24 if age is not None else None

    # buyer_accel compares byr24 across two windows, but byr24 is a trailing
    # 24h count: for a token younger than 24h it climbs as the window fills,
    # whatever buyers actually do, so the ratio reports acceleration that is
    # not there. The bias only fully clears once both windows sit past the
    # fill, hence 24h + the 6h trailing window rather than a token simply
    # being "a few hours old". Guarded here rather than inside buyer_accel,
    # which the discover ordering already uses unguarded.
    accel = None
    if age_h is not None and age_h >= RECENT_MOVE_MIN_AGE_H:
        accel = buyer_accel(t["ca"], hist)

    price_pts = _band(t.get("chg_h6"), PRICE_BANDS)
    accel_pts = _band(accel, ACCEL_BANDS)

    if price_pts is None and accel_pts is None:
        return None, "unscored: no 6h change and no usable buyer rate", None
    if accel_pts is None:
        basis = ("price only (too young for buyer rate)"
                 if age_h is not None and age_h < RECENT_MOVE_MIN_AGE_H
                 else "price only (not enough history for buyer rate)")
    elif price_pts is None:
        basis = "accel only (no 6h change reported)"
    else:
        basis = "price+accel"
    return (price_pts or 0) + (accel_pts or 0), basis, accel


def load_avoid():
    try:
        with open("avoid.json") as f:
            return {e["ca"].lower(): e.get("reason", "")
                    for e in json.load(f) if e.get("ca")}
    except Exception as e:
        print(f"WARN avoid.json: {e}")
        return {}


def write_history(tokens, now):
    path, cutoff, kept = "data/history.jsonl", now - timedelta(days=HISTORY_DAYS), []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                if datetime.fromisoformat(json.loads(line)["ts"]) >= cutoff:
                    kept.append(line)
            except Exception:
                continue
    ts = now.isoformat()
    for t in tokens:
        if t["txns24"] >= MIN_SCORE_TXNS:
            kept.append(json.dumps({
                "ts": ts, "ca": t["ca"], "sym": t["symbol"],
                "byr24": t["buyers24"], "price": t["price_usd"], "liq": t["liq_usd"],
                "sellers24": t["sellers24"], "fdv_usd": t["fdv_usd"],
                "vol24_usd": t["vol24_usd"], "buys6": t["buys6"],
                "sells6": t["sells6"], "chg_h6": t["chg_h6"],
                "recent_move_score": t.get("recent_move_score"),
                "chg_h24": t["chg_h24"], "main_pool": t["main_pool"],
                "flag_contradiction": t["flag_contradiction"],
                "flag_liq_anomaly": t["flag_liq_anomaly"],
                # every snapshot, not just the one daily file that survives,
                # so tier persistence is queryable across runs
                "tier": t.get("tier")}))
    with open(path, "w") as f:
        f.write("\n".join(kept) + "\n")


def write_log(tokens, now):
    """One row per token per run, written once and never touched again.

    The evidence layer's raw material. history.jsonl is a 15-day working set
    the collector reads and prunes; this is the permanent record nothing reads.
    Ten fields with short keys, because at ~140 rows a run the key names are a
    material share of the bytes. See docs/EVIDENCE_LAYER.md.

    One file per run rather than a daily partition, for a reason specific to
    committing data to git: a file appended 96 times costs git 96 blobs, a file
    written once costs one -- 48x the blob bytes for identical content on disk.
    The second reason matters more: a per-run filename cannot collide, so
    unlike history.jsonl this is immune to the Commit-step conflict that failed
    run #1360.

    Every token is logged, not just the ones above MIN_SCORE_TXNS that reach
    history.jsonl. Nothing here can be backfilled: a row not written now is
    gone, and over-capturing is the reversible mistake.

    Returns (path, rows). Raises only to its caller, which swallows it.
    """
    rows = [json.dumps({
        "ts": now.isoformat(), "ca": t["ca"], "tier": t.get("tier"),
        "rms": t.get("recent_move_score"), "pc": t.get("persist_count"),
        "price": t["price_usd"], "liq": t["liq_usd"], "fdv": t["fdv_usd"],
        "txns": t["txns24"], "byr": t["buyers24"],
    }, separators=(",", ":")) for t in tokens]
    if not rows:
        return None, 0
    # seconds in the name, not just HHMM: two dispatches landed 3m43s apart on
    # 2026-10-18, and a filename that can repeat is a filename that can conflict
    path = f"data/log/{now:%Y-%m-%d}/{now:%H%M%S}.jsonl"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(rows) + "\n")
    return path, len(rows)


def main():
    now = datetime.now(timezone.utc)
    avoid = load_avoid()
    raw, lookups = collect()
    pools = [parse_pool(p, now) for p in raw]
    tokens = [score_token(t) for t in aggregate(pools)]

    hist = load_history()
    persist_ok, persist_count = persistence(hist, now)
    for t in tokens:
        t["tier"] = classify(t, avoid, persist_ok, hist)
        t["watch_reason"] = (watch_reason(t, persist_ok, hist)
                             if t["tier"] == "WATCH" else None)
        # additive only: nothing below reads back into tier or screen_score.
        # persist_count is the healthy-print count the persistence gate was
        # decided on; it was a local and so invisible in the output, which
        # made "why is this BEST" unanswerable from the published files.
        t["persist_count"] = persist_count.get(t["ca"], 0)
        t["last5_byr24"] = last_byr24(t["ca"], hist)
        rm, basis, accel = recent_move(t, hist)
        t["recent_move_score"], t["recent_move_basis"] = rm, basis
        t["recent_move_accel_pct"] = accel   # age-guarded; discover's is not

    avoided = [{"symbol": t["symbol"], "ca": t["ca"], "reason": avoid[t["ca"]]}
               for t in tokens if t["ca"] in avoid]
    # kept as the pre-tier consumers expect it: the top two tiers, same order
    cands = sorted([t for t in tokens if t["tier"] in ("BEST", "EMERGING")],
                   key=lambda t: (t["score_basis"] != "flow+depth",
                                  -(t["screen_score"] or 0), -t["vol24_usd"]))

    by_tier, counts = {}, {}
    for t in tokens:
        by_tier.setdefault(t["tier"], []).append(
            {"symbol": t["symbol"], "ca": t["ca"], "screen_score": t["screen_score"]})
    for tier, items in by_tier.items():
        counts[tier] = len(items)

    # surfacing only: rising or simply unproven, never auto-promoted anywhere.
    # contradiction tokens are excluded — they failed a sanity check rather
    # than a maturity bar, which is a different thing to be looking at.
    discover = [t for t in tokens
                if t["ca"] not in STABLECOINS
                and (t["tier"] == "EMERGING"
                     or (t["tier"] == "WATCH"
                         and t["watch_reason"] in ("unproven", "tpt_high")))]
    discover = sorted(
        ({"symbol": t["symbol"], "ca": t["ca"], "tier": t["tier"],
          "watch_reason": t["watch_reason"],
          "buyer_accel_pct": buyer_accel(t["ca"], hist),
          "screen_score": t["screen_score"], "txns24": t["txns24"],
          "age_days": t["age_days"], "liq_usd": t["liq_usd"],
          "vol24_usd": t["vol24_usd"], "gt_url": t["gt_url"]} for t in discover),
        key=lambda r: (r["buyer_accel_pct"] is None, -(r["buyer_accel_pct"] or 0)))

    reasons = {}
    for t in tokens:
        if t["watch_reason"]:
            reasons[t["watch_reason"]] = reasons.get(t["watch_reason"], 0) + 1

    out = {"generated_at": now.isoformat(), "network": NETWORK,
           "pools_seen": len(pools), "tokens_seen": len(tokens),
           "pool_lookups": lookups,
           "pool_lookups_failed": sum(1 for l in lookups if not l["ok"]),
           "tier_counts": counts, "watch_reasons": reasons,
           "tokens_by_tier": by_tier, "discover": discover,
           "avoided": avoided, "candidates": cands, "tokens": tokens}
    os.makedirs("data/daily", exist_ok=True)
    json.dump(out, open("data/latest.json", "w"), indent=2)
    json.dump(out, open(f"data/daily/{now:%Y-%m-%d}.json", "w"), indent=2)
    write_history(tokens, now)

    # Last, after every file the pipeline depends on, and swallowed: nothing
    # reads the log, so a bug in it must not be able to cost a run its data
    # commit or move a tier. A missing log file is recoverable; a missing
    # snapshot is not.
    try:
        log_path, log_rows = write_log(tokens, now)
        print(f"log: {log_rows} rows -> {log_path}")
    except Exception as e:
        print(f"WARN write_log: {type(e).__name__}: {e}")

    bad = [l["ca"][:10] for l in lookups if not l["ok"]]
    print(f"{len(pools)} pools -> {len(tokens)} tokens, "
          f"{len(cands)} candidates, {len(avoided)} avoided")
    print(f"pool lookups: {len(lookups) - len(bad)}/{len(lookups)} ok"
          + (f", FAILED: {', '.join(bad)}" if bad else ""))


if __name__ == "__main__":
    main()
