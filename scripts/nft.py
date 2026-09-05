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

DISCOVER_ORDER  = "seven_day_volume"   # one_day_volume also accepted
DISCOVER_LIMIT  = 20
NEW_DAYS        = 30     # created within this many days counts as very new
LOW_OWNER_RATIO = 0.15   # owners/supply under this reads as bundled, not spread

# Every metadata field OpenSea returns that a collection fills in itself, plus
# safelist_status. Counting the non-empty ones measures how completely the
# listing is filled in and nothing else: each of these is free text a copycat
# can populate in minutes, so the count carries no claim about the project.
META_FIELDS = ["twitter_username", "project_url", "description", "discord_url",
               "telegram_url", "instagram_username", "wiki_url", "safelist_status"]

# "Nearly everything filled in" — 6 of 8 keeps the proportion of the 4-of-5
# this flag was specified with
DRESSED_MIN = 6

# Only slugs verified against a live /collections/{slug} response belong here:
#   python scripts/nft.py --verify <slug> [<slug> ...]
# prints name, chains, floor, supply and whether the collection is on
# Robinhood Chain. Slugs are not guessable from a display name, so an
# unverified guess silently tracks the wrong collection.
WATCHLIST = {
    "stonkbrokers-434284142": "StonkBrokers",   # 4444 supply, verified 2026-09-05
    "cashcatss": "Cash Cats",                   # 9995 supply; note the double s
    "chain-mancers": "Chain Mancers",           # 5000 supply
    "itsriggles": "Riggles",                    # 2222 supply, thinnest of the four
}


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


def filled_meta(item):
    # non-empty, not merely present: OpenSea returns "" for unset urls, so a
    # key check would score an empty discord_url as filled in
    return [f for f in META_FIELDS if str(item.get(f) or "").strip()]


def age_days(created, now):
    try:
        d = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (now - d).days


def discover_row(item, key, now):
    slug = item["collection"]
    # total_supply and created_date are detail-only; num_owners is stats-only
    detail = get(f"/collections/{slug}", key)
    stats = get(f"/collections/{slug}/stats", key)
    total, day = stats.get("total") or {}, day_interval(stats)

    supply = detail.get("total_supply")
    owners = total.get("num_owners")
    ratio = round(owners / supply, 4) if owners and supply else None
    age = age_days(detail.get("created_date"), now)
    filled = filled_meta(detail)

    r = {"slug": slug, "name": detail.get("name"),
         "created_date": detail.get("created_date"), "age_days": age,
         "total_supply": supply, "num_owners": owners,
         "owner_supply_ratio": ratio,
         "floor_price": num(total.get("floor_price")),
         "floor_symbol": total.get("floor_price_symbol"),
         "volume_total": num(total.get("volume")),
         "volume_24h": num(day.get("volume")), "sales_24h": day.get("sales"),
         "safelist_status": detail.get("safelist_status"),
         "opensea_url": detail.get("opensea_url"),
         "social_presence_score": len(filled),
         "social_fields_checked": len(META_FIELDS),
         "social_fields_filled": filled}

    r["flag_low_owner_ratio"] = bool(ratio is not None and ratio < LOW_OWNER_RATIO)
    r["flag_new"] = bool(age is not None and age <= NEW_DAYS)
    # complete listing + supply in few hands + brand new is the copycat shape
    r["flag_dressed_but_thin"] = bool(
        r["social_presence_score"] >= DRESSED_MIN
        and r["flag_low_owner_ratio"] and r["flag_new"])
    return r


def discover(key, order, now):
    listed = get(f"/collections?chain={CHAIN}&order_by={order}"
                 f"&limit={DISCOVER_LIMIT}", key).get("collections") or []
    rows = []
    for item in listed:
        if not item.get("collection"):
            continue
        try:
            rows.append(discover_row(item, key, now))
        except Exception as e:
            print(f"WARN {item.get('collection')}: {err(e)}")
        time.sleep(1)
    # same thin-floor rule as tracked collections; a collection with no row in
    # nft.jsonl yet simply has no prior floor to compare against
    return enrich(rows, history())


def err(e):
    body = ""
    try:
        body = e.read(300).decode("utf-8", "replace").replace("\n", " ")
    except Exception:
        pass
    return f"{e} {body}".strip()


def probe():
    """Dump raw discovery/detail responses so field names can be read, not assumed."""
    key = mint_key()
    listed, detail, orders = None, None, {}

    print("=== RAW list: /collections?chain=%s&limit=3 ===" % CHAIN)
    try:
        listed = get(f"/collections?chain={CHAIN}&limit=3", key)
        print(json.dumps(listed, indent=2, sort_keys=True)[:4000])
    except Exception as e:
        print("FAIL:", err(e))

    for ob in ["market_cap", "seven_day_volume", "one_day_volume",
               "created_date", "num_owners"]:
        try:
            d = get(f"/collections?chain={CHAIN}&order_by={ob}&limit=1", key)
            orders[ob] = f"OK ({len(d.get('collections') or [])} item)"
        except Exception as e:
            orders[ob] = err(e)[:160]
        time.sleep(1)

    print()
    print("=== RAW detail: /collections/stonkbrokers-434284142 ===")
    try:
        detail = get("/collections/stonkbrokers-434284142", key)
        print(json.dumps(detail, indent=2, sort_keys=True)[:4000])
    except Exception as e:
        print("FAIL:", err(e))

    # summary last: log tailing shows the end of the step
    print()
    print("=== SUMMARY ===")
    print("list top-level keys:", sorted(listed) if isinstance(listed, dict) else None)
    items = (listed or {}).get("collections") or []
    print("list item count:", len(items))
    print("list item keys:", sorted({k for it in items for k in it}) if items else None)
    print("order_by accepted:")
    for k, v in orders.items():
        print(f"    {k}: {v}")
    print("detail keys:", sorted(detail) if isinstance(detail, dict) else None)
    for f in ["twitter_username", "project_url", "description", "discord_url",
              "is_verified", "safelist_status", "created_date", "opensea_url",
              "total_supply", "owner"]:
        present = isinstance(detail, dict) and f in detail
        val = repr((detail or {}).get(f))[:80] if present else "-"
        print(f"    {f:18} present={present} value={val}")


def main():
    key = None

    if "--probe" in sys.argv:
        probe()
        return

    key = mint_key()

    if "--discover" in sys.argv:
        now = datetime.now(timezone.utc)
        order = os.environ.get("DISCOVER_ORDER") or DISCOVER_ORDER
        rows = discover(key, order, now)
        os.makedirs("data", exist_ok=True)
        # surfacing only: never feeds WATCHLIST, which stays hand-verified
        json.dump({"generated_at": now.isoformat(), "chain": CHAIN,
                   "order_by": order, "social_fields": META_FIELDS,
                   "collections": rows},
                  open("data/nft_discover.json", "w"), indent=2)
        for r in rows:
            print(f"{r['slug']}: floor {r['floor_price']} owners/supply "
                  f"{r['owner_supply_ratio']} age {r['age_days']}d "
                  f"meta {r['social_presence_score']}/{r['social_fields_checked']} "
                  f"dressed_but_thin={r['flag_dressed_but_thin']}")
        return

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
