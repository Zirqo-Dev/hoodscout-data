#!/usr/bin/env python3
"""Collect Robinhood Chain pool data from GeckoTerminal and score it."""
import json, os, time, urllib.request
from datetime import datetime, timezone

NETWORK = "robinhood"
BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.1"}

# --- tunable thresholds ---
MIN_LIQ_USD    = 30_000
MIN_FDV_USD    = 150_000
MAX_FDV_USD    = 50_000_000
MIN_TXNS_24H   = 300
MAX_AGE_DAYS   = 14
TRENDING_PAGES = 2
NEW_POOL_PAGES = 3
# -------------------------

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

def score(item, now):
    a = item.get("attributes") or {}
    rel = item.get("relationships") or {}
    tx = a.get("transactions") or {}
    tx24, tx6 = tx.get("h24") or {}, tx.get("h6") or {}
    pc = a.get("price_change_percentage") or {}

    buys, sells = tx24.get("buys", 0), tx24.get("sells", 0)
    buyers, sellers = tx24.get("buyers", 0), tx24.get("sellers", 0)
    txns, traders = buys + sells, buyers + sellers
    tpt = round(txns / traders, 2) if traders else None

    liq, fdv = num(a.get("reserve_in_usd")), num(a.get("fdv_usd"))
    liq_fdv = round(100 * liq / fdv, 2) if liq and fdv else None

    age_days = None
    if a.get("pool_created_at"):
        try:
            t = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
            age_days = round((now - t).total_seconds() / 86400, 2)
        except ValueError:
            pass

    ch6, ch24 = num(pc.get("h6")), num(pc.get("h24"))
    b6, s6 = tx6.get("buys", 0), tx6.get("sells", 0)
    contradiction = bool(b6 > s6 * 1.2 and ch6 is not None and ch6 < 0)

    flow  = 10 if (tpt is not None and tpt <= 4) else 5 if (tpt is not None and tpt <= 8) else 1
    depth = 10 if (liq_fdv is not None and liq_fdv >= 5) else 5 if (liq_fdv is not None and liq_fdv >= 3) else 1

    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    ca = base_id.split("_", 1)[1] if "_" in base_id else None

    return {
        "name": a.get("name"), "pool": a.get("address"), "base_ca": ca,
        "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
        "price_usd": num(a.get("base_token_price_usd")),
        "fdv_usd": fdv, "liq_usd": liq, "liq_fdv_pct": liq_fdv,
        "vol24_usd": num((a.get("volume_usd") or {}).get("h24")),
        "txns24": txns, "traders24": traders, "trades_per_trader": tpt,
        "buys24": buys, "sells24": sells,
        "age_days": age_days, "chg_h6": ch6, "chg_h24": ch24,
        "flag_contradiction": contradiction,
        "screen_score": round(flow * 0.625 + depth * 0.375, 2),
        "gt_url": f"https://www.geckoterminal.com/{NETWORK}/pools/{a.get('address')}",
    }

def passes(r):
    return ((r["liq_usd"] or 0) >= MIN_LIQ_USD
            and MIN_FDV_USD <= (r["fdv_usd"] or 0) <= MAX_FDV_USD
            and r["txns24"] >= MIN_TXNS_24H
            and r["age_days"] is not None and r["age_days"] <= MAX_AGE_DAYS)

def main():
    now = datetime.now(timezone.utc)
    rows = [score(p, now) for p in collect()]
    rows = [r for r in rows if r["pool"]]
    cands = sorted([r for r in rows if passes(r)],
                   key=lambda r: (-r["screen_score"], -(r["vol24_usd"] or 0)))
    out = {"generated_at": now.isoformat(), "network": NETWORK,
           "pools_seen": len(rows), "candidates": cands, "all_pools": rows}
    os.makedirs("data/daily", exist_ok=True)
    json.dump(out, open("data/latest.json", "w"), indent=2)
    json.dump(out, open(f"data/daily/{now:%Y-%m-%d}.json", "w"), indent=2)
    print(f"{len(rows)} pools seen, {len(cands)} candidates")

if __name__ == "__main__":
    main()
