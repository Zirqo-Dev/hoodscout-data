#!/usr/bin/env python3
"""Track Robinhood Stock Token float, locked share, and premium vs a pre-blackout anchor."""
import json, os, time, urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://api.geckoterminal.com/api/v2"
RPC = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
NETWORK = "robinhood"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.3"}

# Every entry is confirmed by scripts/verify_stocks.py: the on-chain name
# carries the "Robinhood Token" suffix and the deployed bytecode is identical
# to the others (283 bytes, sha256 caced2aa743efc36). Ticker squatters are
# thick on this chain, so nothing goes in here on a name match alone.
STOCKS = {
    "HIMS": "0xccee82fe024c36fa15e1005ede3e9e4787e23d09",
    "AMC":  "0x05a3d1cd21d0c88145e82600e62e7e496e0f222b",
    "NVDA": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",  # NVIDIA, verified 2026-09-06
    "MSTR": "0xec262a75e413fafd0df80480274532c79d42da09",  # Strategy Inc.
    "GME":  "0x1b0e319c6a659f002271b69db8a7df2f911c153e",  # GameStop
}

# counterparties that are trading venues, not "locking" pools
REFERENCE = {
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0x0000000000000000000000000000000000000000",  # native / WETH
}

# last moment the AP can still arbitrage before the blackout (UTC)
ANCHOR_UTC = os.environ.get("ANCHOR_UTC", "2026-09-05T00:00:00+00:00")

ALERT_PREMIUM   = 20.0   # % over anchor
ALERT_SUPPLY_6H = 2.5    # % move in 6h
ALERT_LOCKED    = 40.0   # % floor

# fdv/price can go stale on GeckoTerminal and fake a mint/burn, so a supply move
# is only trusted when on-chain totalSupply agrees within this margin
SUPPLY_TOLERANCE = 1.0   # %


def get(path):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def eth_call(to, selector):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": selector}, "latest"]}).encode()
    req = urllib.request.Request(RPC, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if d.get("error"):
        raise RuntimeError(f"rpc {d['error']}")
    res = d.get("result")
    # "0x" comes back when there is no contract at the address
    if not res or res == "0x":
        return None
    return int(res, 16)


def chain_supply(ca):
    total = eth_call(ca, "0x18160ddd")   # totalSupply()
    dec = eth_call(ca, "0x313ce567")     # decimals()
    if total is None or dec is None:
        return None
    return total / 10 ** dec


def confirm_supply(r):
    try:
        chain = chain_supply(r["ca"])
    except Exception as e:
        print(f"WARN rpc {r['symbol']}: {e}")
        try:
            print(f"WARN rpc {r['symbol']} headers: {dict(e.headers)}")
            print(f"WARN rpc {r['symbol']} body: {e.read(200)!r}")
        except Exception as diag:
            print(f"WARN rpc {r['symbol']} no response detail: {diag!r}")
        return f" [UNCONFIRMED: chain RPC check failed ({type(e).__name__})]"

    if not chain:
        return " [UNCONFIRMED: chain RPC returned no totalSupply]"

    r["chain_supply"] = round(chain, 3)
    gap = 100 * abs(r["supply"] / chain - 1)
    r["chain_supply_gap_pct"] = round(gap, 2)
    if gap > SUPPLY_TOLERANCE:
        return (f" [UNCONFIRMED: on-chain totalSupply {chain:,.0f}, "
                f"{gap:.1f}% off the pool-derived figure]")
    return ""


def addr_of(rel, side):
    tid = ((rel.get(side) or {}).get("data") or {}).get("id", "")
    return tid.split("_", 1)[1].lower() if "_" in tid else None


def measure(sym, ca):
    data = get(f"/networks/{NETWORK}/tokens/{ca}/pools").get("data", [])
    cats = {"locked": 0.0, "reference": 0.0, "cross": 0.0}
    price, fdv, deepest_ref = None, None, 0.0
    pools = []

    for item in data:
        a, rel = item.get("attributes") or {}, item.get("relationships") or {}
        base, quote = addr_of(rel, "base_token"), addr_of(rel, "quote_token")
        if ca not in (base, quote):
            continue
        other = quote if base == ca else base
        res = num(a.get("reserve_in_usd")) or 0.0

        if other in REFERENCE:
            cat = "reference"
            if res > deepest_ref:          # deepest stable pool = price truth
                deepest_ref = res
                price, fdv = num(a.get("token_price_usd")), num(a.get("fdv_usd"))
        elif other in {v.lower() for v in STOCKS.values()}:
            cat = "cross"
        else:
            cat = "locked"

        cats[cat] += res
        pools.append({"name": a.get("name"), "cat": cat,
                      "reserve": round(res, 2), "pool": a.get("address")})

    if not price or not fdv:
        return None

    supply = fdv / price
    locked_units = cats["locked"] / 2 / price          # ~50/50 assumption
    pools.sort(key=lambda p: -p["reserve"])

    return {
        "symbol": sym, "ca": ca, "price_usd": round(price, 6),
        "supply": round(supply, 3), "float_usd": round(fdv, 2),
        "reserve_locked": round(cats["locked"], 2),
        "reserve_reference": round(cats["reference"], 2),
        "reserve_cross": round(cats["cross"], 2),
        "locked_units_est": round(locked_units, 1),
        "locked_pct_est": round(100 * locked_units / supply, 2) if supply else None,
        "free_float_units": round(supply - locked_units, 1),
        "pool_count": len(pools), "pools_capped": len(data) >= 20,
        "top_pools": pools[:6],
    }


def history(path="data/stocks.jsonl"):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def alerts(rows, hist, now):
    fired = []
    anchor_t = datetime.fromisoformat(ANCHOR_UTC)
    for r in rows:
        past = [h for h in hist if h.get("symbol") == r["symbol"]]

        anchor = [h for h in past
                  if datetime.fromisoformat(h["ts"]) <= anchor_t and h.get("price_usd")]
        if anchor:
            base = anchor[-1]["price_usd"]
            prem = 100 * (r["price_usd"] / base - 1)
            r["anchor_price"] = base
            r["premium_pct"] = round(prem, 2)
            if prem >= ALERT_PREMIUM:
                fired.append(f"{r['symbol']} premium {prem:+.1f}% vs anchor ${base:.2f} "
                             f"(now ${r['price_usd']:.2f})")

        old = [h for h in past
               if (now - datetime.fromisoformat(h["ts"])).total_seconds() <= 6 * 3600
               and h.get("supply")]
        if old:
            d = 100 * (r["supply"] / old[0]["supply"] - 1)
            r["supply_chg_6h_pct"] = round(d, 2)
            if abs(d) >= ALERT_SUPPLY_6H:
                verb = "MINTED" if d > 0 else "BURNED"
                fired.append(f"{r['symbol']} float {verb} {abs(d):.1f}% in 6h "
                             f"({old[0]['supply']:,.0f} to {r['supply']:,.0f})"
                             + confirm_supply(r))

        lp = r.get("locked_pct_est")
        if lp is not None and lp < ALERT_LOCKED:
            fired.append(f"{r['symbol']} locked share {lp:.1f}% below {ALERT_LOCKED}% floor")
    return fired


def main():
    now = datetime.now(timezone.utc)
    hist = history()
    rows = []
    for sym, ca in STOCKS.items():
        try:
            m = measure(sym, ca.lower())
            if m:
                m["ts"] = now.isoformat()
                rows.append(m)
        except Exception as e:
            print(f"WARN {sym}: {e}")
        time.sleep(2.5)

    if not rows:
        print("no data")
        return

    fired = alerts(rows, hist, now)
    os.makedirs("data", exist_ok=True)
    with open("data/stocks.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if k != "top_pools"}) + "\n")
    json.dump({"generated_at": now.isoformat(), "anchor_utc": ANCHOR_UTC,
               "alerts": fired, "tokens": rows},
              open("data/stocks_latest.json", "w"), indent=2)

    for r in rows:
        print(f"{r['symbol']}: ${r['price_usd']:.4f} supply {r['supply']:,.0f} "
              f"locked {r.get('locked_pct_est')}% prem {r.get('premium_pct')}")
    if fired:
        with open("ALERT.txt", "w") as f:
            # trailing newline required: the workflow reads this with `while
            # read`, which drops a final line that has no line terminator
            f.write("\n".join(fired) + "\n")
        print("ALERTS:", *fired, sep="\n  ")


if __name__ == "__main__":
    main()
