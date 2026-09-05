#!/usr/bin/env python3
"""Track Robinhood Chain NFT collection floors, volume, and thin-volume floor moves."""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://api.opensea.io/api/v2"
CHAIN = "robinhood"
UA = "hoodscout-data/0.3"

HISTORY_DAYS   = 30
THIN_SALES     = 5     # 24h sales below which a floor move is not trade-backed
FLOOR_MOVE_PCT = 3.0   # floor move worth asking whether trades back it

# Only slugs verified against a live /collections/{slug} response belong here:
#   python scripts/nft.py --verify <slug> [<slug> ...]
# prints name, chains, floor, supply and whether the collection is on
# Robinhood Chain. Slugs are not guessable from a display name, so an
# unverified guess silently tracks the wrong collection.
WATCHLIST = {}


def post(path):
    req = urllib.request.Request(BASE + path, data=b"",
                                 headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get(path, key):
    req = urllib.request.Request(BASE + path,
                                 headers={"Accept": "application/json",
                                          "X-API-KEY": key, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def mint_key():
    # free-tier keys are self-service; minting per run avoids depending on a
    # key lifetime the docs give inconsistently
    d = post("/auth/keys")
    key = d.get("api_key") or d.get("key")
    if not key:
        raise RuntimeError(f"no api_key in auth response (keys: {sorted(d)})")
    return key


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chains_of(coll):
    return sorted({c.get("chain") for c in (coll.get("contracts") or []) if c.get("chain")})


def day_interval(stats):
    for i in stats.get("intervals") or []:
        if i.get("interval") == "one_day":
            return i
    return {}


def evidence(slug, key):
    coll = get(f"/collections/{slug}", key)
    total = (get(f"/collections/{slug}/stats", key).get("total")) or {}
    chains = chains_of(coll)
    return {"slug": slug, "name": coll.get("name"), "chains": chains,
            "on_robinhood_chain": CHAIN in chains,
            "total_supply": coll.get("total_supply"),
            "floor_price": total.get("floor_price"),
            "floor_symbol": total.get("floor_price_symbol"),
            "volume_total": total.get("volume"),
            "num_owners": total.get("num_owners")}


def measure(slug, key):
    coll = get(f"/collections/{slug}", key)
    chains = chains_of(coll)
    if CHAIN not in chains:
        print(f"WARN {slug}: not on {CHAIN} (chains={chains})")
        return None

    stats = get(f"/collections/{slug}/stats", key)
    total, day = stats.get("total") or {}, day_interval(stats)
    supply = coll.get("total_supply")
    listed = total.get("num_listed")

    return {"slug": slug, "name": coll.get("name"), "chains": chains,
            "total_supply": supply,
            "floor_price": num(total.get("floor_price")),
            "floor_symbol": total.get("floor_price_symbol"),
            "volume_total": num(total.get("volume")),
            "volume_24h": num(day.get("volume")),
            "sales_24h": day.get("sales"),
            "num_owners": total.get("num_owners"),
            # OpenSea does not always return a listed count; counting listings
            # directly would mean paginating every open order
            "listed_pct": round(100 * listed / supply, 2) if listed and supply else None}


def history(path="data/nft.jsonl"):
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


def enrich(rows, hist):
    prev = {}
    for h in hist:
        if h.get("slug"):
            prev[h["slug"]] = h          # file is append-ordered, so last wins

    for r in rows:
        p = prev.get(r["slug"]) or {}
        before = num(p.get("floor_price"))
        r["floor_chg_pct"] = (round(100 * (r["floor_price"] / before - 1), 2)
                              if before and r["floor_price"] else None)
        # a floor move on almost no sales is a quote, not a trade signal
        r["flag_thin_floor_move"] = bool(
            r["floor_chg_pct"] is not None and abs(r["floor_chg_pct"]) >= FLOOR_MOVE_PCT
            and r["sales_24h"] is not None and r["sales_24h"] < THIN_SALES)
    return rows


def write_history(rows, now, path="data/nft.jsonl"):
    cutoff, kept = now - timedelta(days=HISTORY_DAYS), []
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
    for r in rows:
        kept.append(json.dumps({"ts": ts, "slug": r["slug"],
                                "floor_price": r["floor_price"],
                                "volume_24h": r["volume_24h"],
                                "sales_24h": r["sales_24h"],
                                "num_owners": r["num_owners"]}))
    with open(path, "w") as f:
        f.write("\n".join(kept) + "\n")


def main():
    key = mint_key()

    if "--verify" in sys.argv:
        for slug in sys.argv[sys.argv.index("--verify") + 1:]:
            try:
                print(json.dumps(evidence(slug, key), indent=2))
            except Exception as e:
                print(f"FAIL {slug}: {e}")
            time.sleep(1)
        return

    if not WATCHLIST:
        print("WATCHLIST empty; verify slugs with --verify before tracking")
        return

    now = datetime.now(timezone.utc)
    rows = []
    for slug in WATCHLIST:
        try:
            m = measure(slug, key)
            if m:
                m["ts"] = now.isoformat()
                rows.append(m)
        except Exception as e:
            print(f"WARN {slug}: {e}")
        time.sleep(1)

    if not rows:
        print("no data")
        return

    enrich(rows, history())
    os.makedirs("data", exist_ok=True)
    write_history(rows, now)
    json.dump({"generated_at": now.isoformat(), "chain": CHAIN, "collections": rows},
              open("data/nft_latest.json", "w"), indent=2)

    for r in rows:
        print(f"{r['slug']}: floor {r['floor_price']} {r['floor_symbol'] or ''} "
              f"chg {r['floor_chg_pct']}% sales24 {r['sales_24h']} "
              f"thin={r['flag_thin_floor_move']}")


if __name__ == "__main__":
    main()
