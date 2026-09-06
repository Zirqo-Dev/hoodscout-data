#!/usr/bin/env python3
"""Collect Robinhood Chain pool data, aggregate by token, score."""
import json, os, time, urllib.request
from datetime import datetime, timezone, timedelta

NETWORK = "robinhood"
BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.2"}

MIN_TXNS_24H   = 300     # "real activity"
MIN_SCORE_TXNS = 200     # below this: no score at all
MIN_AGE_DEPTH  = 1.0     # days before liq/FDV means anything
MAX_AGE_DAYS   = 14
HISTORY_DAYS   = 30

# tier thresholds
PERSIST_RUNS     = 8     # snapshots looked back over for BEST
PERSIST_MIN      = 5     # of which this many must show the token healthy
BEST_MIN_AGE     = 2.0
BEST_MIN_LIQ_FDV = 5.0
BEST_MAX_TPT     = 4.0
EMERGING_MAX_AGE = 3.0
EMERGING_MAX_TPT = 5.0   # looser than BEST: early trading is concentrated
DEAD_TXNS_24H    = 10    # below this, a token past MAX_AGE_DAYS is inactive
TRENDING_PAGES = 2
NEW_POOL_PAGES = 3


def get(path):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


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
    return pools


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


def persistence(hist):
    """Per-CA count of healthy appearances across the last PERSIST_RUNS snapshots."""
    recent = set(sorted({r["ts"] for r in hist if r.get("ts")})[-PERSIST_RUNS:])
    seen = {}
    for r in hist:
        # only scored tokens are ever written to history, so presence is the
        # "scored" half of the test; rows predating the fattened format carry
        # no anomaly flag, and absent is read as healthy rather than dropping
        # the token — it self-corrects as new snapshots land
        if r.get("ts") in recent and r.get("ca") and r.get("flag_liq_anomaly") is not True:
            seen.setdefault(r["ca"], set()).add(r["ts"])
    return {ca: len(ts) for ca, ts in seen.items()}


def buyer_rate_rising(ca, hist, window=3):
    """buyers24 strictly increasing across the last few snapshots for this CA."""
    rows = sorted([r for r in hist if r.get("ca") == ca and r.get("byr24") is not None],
                  key=lambda r: r.get("ts") or "")[-window:]
    if len(rows) < 2:
        return False
    vals = [r["byr24"] for r in rows]
    return all(b > a for a, b in zip(vals, vals[1:]))


def classify(t, avoid, persist, hist):
    """Assign exactly one tier. Nothing with a real signal is dropped."""
    if t["ca"] in avoid:
        return "AVOID"

    contra, age = t["flag_contradiction"], t["age_days"]
    lf, tpt = t["liq_fdv_pct"], t["trades_per_trader"]

    if (persist.get(t["ca"], 0) >= PERSIST_MIN and not contra
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
                "chg_h24": t["chg_h24"], "main_pool": t["main_pool"],
                "flag_contradiction": t["flag_contradiction"],
                "flag_liq_anomaly": t["flag_liq_anomaly"],
                # every snapshot, not just the one daily file that survives,
                # so tier persistence is queryable across runs
                "tier": t.get("tier")}))
    with open(path, "w") as f:
        f.write("\n".join(kept) + "\n")


def main():
    now = datetime.now(timezone.utc)
    avoid = load_avoid()
    pools = [parse_pool(p, now) for p in collect()]
    tokens = [score_token(t) for t in aggregate(pools)]

    hist = load_history()
    persist = persistence(hist)
    for t in tokens:
        t["tier"] = classify(t, avoid, persist, hist)

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

    out = {"generated_at": now.isoformat(), "network": NETWORK,
           "pools_seen": len(pools), "tokens_seen": len(tokens),
           "tier_counts": counts, "tokens_by_tier": by_tier,
           "avoided": avoided, "candidates": cands, "tokens": tokens}
    os.makedirs("data/daily", exist_ok=True)
    json.dump(out, open("data/latest.json", "w"), indent=2)
    json.dump(out, open(f"data/daily/{now:%Y-%m-%d}.json", "w"), indent=2)
    write_history(tokens, now)
    print(f"{len(pools)} pools -> {len(tokens)} tokens, "
          f"{len(cands)} candidates, {len(avoided)} avoided")


if __name__ == "__main__":
    main()
