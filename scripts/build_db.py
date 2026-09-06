#!/usr/bin/env python3
"""Rebuild hoodscout.db from the JSON/JSONL snapshots tracked in git.

Derived artifact: the database is deleted and rebuilt on every run, so it is
safe to run repeatedly and never diverges from the files it reads.
"""
import glob, json, os, sqlite3, sys

DB = "hoodscout.db"

SCHEMA = """
CREATE TABLE tokens (
    ca                 TEXT PRIMARY KEY,
    symbol             TEXT,
    first_seen         TEXT
);

CREATE TABLE meme_snapshots (
    ca                 TEXT NOT NULL,
    ts                 TEXT NOT NULL,
    price              REAL,
    liq                REAL,
    fdv_usd            REAL,
    vol24_usd          REAL,
    buys6              INTEGER,
    sells6             INTEGER,
    chg_h6             REAL,
    chg_h24            REAL,
    byr24              INTEGER,
    sellers24          INTEGER,
    main_pool          TEXT,
    flag_contradiction INTEGER,
    flag_liq_anomaly   INTEGER,
    tier               TEXT,
    screen_score       REAL,
    trades_per_trader  REAL,
    liq_fdv_pct        REAL,
    age_days           REAL,
    PRIMARY KEY (ca, ts)
);

CREATE TABLE stock_snapshots (
    ca                   TEXT NOT NULL,
    symbol               TEXT,
    ts                   TEXT NOT NULL,
    supply               REAL,
    price_usd            REAL,
    locked_pct_est       REAL,
    free_float_units     REAL,
    premium_pct          REAL,
    chain_supply         REAL,
    chain_supply_gap_pct REAL,
    pools_capped         INTEGER,
    PRIMARY KEY (ca, ts)
);

CREATE TABLE nft_snapshots (
    slug                  TEXT NOT NULL,
    name                  TEXT,
    ts                    TEXT NOT NULL,
    floor                 REAL,
    vol                   REAL,
    num_owners            INTEGER,
    total_supply          INTEGER,
    social_presence_score INTEGER,
    flag_thin_floor_move  INTEGER,
    flag_dressed_but_thin INTEGER,
    tracked               INTEGER,
    PRIMARY KEY (slug, ts)
);

CREATE TABLE avoid (
    ca      TEXT PRIMARY KEY,
    symbol  TEXT,
    reason  TEXT,
    added   TEXT
);

CREATE INDEX idx_meme_ts   ON meme_snapshots(ts);
CREATE INDEX idx_meme_tier ON meme_snapshots(tier);
CREATE INDEX idx_stock_sym ON stock_snapshots(symbol, ts);
CREATE INDEX idx_nft_slug  ON nft_snapshots(slug, ts);
"""


def read_jsonl(path):
    if not os.path.exists(path):
        print(f"WARN missing {path}")
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def read_json(path, default=None):
    if not os.path.exists(path):
        print(f"WARN missing {path}")
        return default
    try:
        return json.load(open(path))
    except ValueError as e:
        print(f"WARN {path}: {e}")
        return default


def flag(v):
    return None if v is None else int(bool(v))


def load_memes():
    """history.jsonl is the time series; full snapshots add the scored fields.

    History rows carry no tier/score/depth columns, and rows written before
    the format was widened carry only six fields, so most columns are legitimately
    NULL. Full snapshots (latest.json and each data/daily/*.json) share the run's
    timestamp with the history rows, so they merge on (ca, ts).
    """
    rows = {}
    for r in read_jsonl("data/history.jsonl"):
        ca, ts = r.get("ca"), r.get("ts")
        if not ca or not ts:
            continue
        rows[(ca, ts)] = {
            "ca": ca, "ts": ts, "symbol": r.get("sym"),
            "price": r.get("price"), "liq": r.get("liq"),
            "fdv_usd": r.get("fdv_usd"), "vol24_usd": r.get("vol24_usd"),
            "buys6": r.get("buys6"), "sells6": r.get("sells6"),
            "chg_h6": r.get("chg_h6"), "chg_h24": r.get("chg_h24"),
            "byr24": r.get("byr24"), "sellers24": r.get("sellers24"),
            "main_pool": r.get("main_pool"),
            "flag_contradiction": flag(r.get("flag_contradiction")),
            "flag_liq_anomaly": flag(r.get("flag_liq_anomaly")),
            "tier": None, "screen_score": None,
            "trades_per_trader": None, "liq_fdv_pct": None, "age_days": None,
        }

    for path in ["data/latest.json"] + sorted(glob.glob("data/daily/*.json")):
        snap = read_json(path)
        if not snap:
            continue
        ts = snap.get("generated_at")
        for t in snap.get("tokens") or []:
            ca = t.get("ca")
            if not ca or not ts:
                continue
            row = rows.setdefault((ca, ts), {"ca": ca, "ts": ts})
            row.update({
                "symbol": t.get("symbol") or row.get("symbol"),
                "price": t.get("price_usd"), "liq": t.get("liq_usd"),
                "fdv_usd": t.get("fdv_usd"), "vol24_usd": t.get("vol24_usd"),
                "buys6": t.get("buys6"), "sells6": t.get("sells6"),
                "chg_h6": t.get("chg_h6"), "chg_h24": t.get("chg_h24"),
                "byr24": t.get("buyers24"), "sellers24": t.get("sellers24"),
                "main_pool": t.get("main_pool"),
                "flag_contradiction": flag(t.get("flag_contradiction")),
                "flag_liq_anomaly": flag(t.get("flag_liq_anomaly")),
                "tier": t.get("tier"), "screen_score": t.get("screen_score"),
                "trades_per_trader": t.get("trades_per_trader"),
                "liq_fdv_pct": t.get("liq_fdv_pct"), "age_days": t.get("age_days"),
            })
    return list(rows.values())


def load_nfts():
    """nft.jsonl is the time series; the name, supply and metadata count live
    only in the latest and discovery snapshots, so they are merged in by slug."""
    latest = read_json("data/nft_latest.json") or {}
    disc = read_json("data/nft_discover.json") or {}

    tracked = set()
    try:
        sys.path.insert(0, "scripts")
        import nft as nft_mod
        tracked = set(nft_mod.WATCHLIST)
    except Exception as e:
        print(f"WARN watchlist: {e}")

    detail = {}
    for c in (disc.get("collections") or []) + (latest.get("collections") or []):
        if c.get("slug"):
            detail.setdefault(c["slug"], {}).update({k: v for k, v in c.items()
                                                     if v is not None})

    rows = {}
    for r in read_jsonl("data/nft.jsonl"):
        slug, ts = r.get("slug"), r.get("ts")
        if not slug or not ts:
            continue
        d = detail.get(slug, {})
        rows[(slug, ts)] = {
            "slug": slug, "name": d.get("name"), "ts": ts,
            "floor": r.get("floor_price"), "vol": r.get("volume_24h"),
            "num_owners": r.get("num_owners"), "total_supply": d.get("total_supply"),
            "social_presence_score": d.get("social_presence_score"),
            "flag_thin_floor_move": flag(d.get("flag_thin_floor_move")),
            "flag_dressed_but_thin": flag(d.get("flag_dressed_but_thin")),
            "tracked": int(slug in tracked),
        }

    # discovery surfaces collections that are not tracked and so never reach
    # nft.jsonl; keep them at the discovery run's timestamp
    for c in disc.get("collections") or []:
        slug, ts = c.get("slug"), disc.get("generated_at")
        if not slug or not ts or (slug, ts) in rows:
            continue
        rows[(slug, ts)] = {
            "slug": slug, "name": c.get("name"), "ts": ts,
            "floor": c.get("floor_price"), "vol": c.get("volume_24h"),
            "num_owners": c.get("num_owners"), "total_supply": c.get("total_supply"),
            "social_presence_score": c.get("social_presence_score"),
            "flag_thin_floor_move": flag(c.get("flag_thin_floor_move")),
            "flag_dressed_but_thin": flag(c.get("flag_dressed_but_thin")),
            "tracked": int(slug in tracked),
        }
    return list(rows.values())


def insert(con, table, rows, cols):
    if not rows:
        return 0
    con.executemany(
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    memes = load_memes()
    insert(con, "meme_snapshots", memes,
           ["ca", "ts", "price", "liq", "fdv_usd", "vol24_usd", "buys6", "sells6",
            "chg_h6", "chg_h24", "byr24", "sellers24", "main_pool",
            "flag_contradiction", "flag_liq_anomaly", "tier", "screen_score",
            "trades_per_trader", "liq_fdv_pct", "age_days"])

    tokens = {}
    for r in memes:
        cur = tokens.get(r["ca"])
        if cur is None or r["ts"] < cur["first_seen"]:
            tokens[r["ca"]] = {"ca": r["ca"], "symbol": r.get("symbol"),
                               "first_seen": r["ts"]}
        elif not cur.get("symbol") and r.get("symbol"):
            cur["symbol"] = r["symbol"]
    insert(con, "tokens", list(tokens.values()), ["ca", "symbol", "first_seen"])

    stocks = [{**r, "pools_capped": flag(r.get("pools_capped"))}
              for r in read_jsonl("data/stocks.jsonl") if r.get("ca") and r.get("ts")]
    insert(con, "stock_snapshots", stocks,
           ["ca", "symbol", "ts", "supply", "price_usd", "locked_pct_est",
            "free_float_units", "premium_pct", "chain_supply",
            "chain_supply_gap_pct", "pools_capped"])

    nfts = load_nfts()
    insert(con, "nft_snapshots", nfts,
           ["slug", "name", "ts", "floor", "vol", "num_owners", "total_supply",
            "social_presence_score", "flag_thin_floor_move",
            "flag_dressed_but_thin", "tracked"])

    avoid = [a for a in (read_json("avoid.json", []) or []) if a.get("ca")]
    insert(con, "avoid", avoid, ["ca", "symbol", "reason", "added"])

    con.commit()
    for t in ["tokens", "meme_snapshots", "stock_snapshots", "nft_snapshots", "avoid"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:<16} {n:>6}")
    con.close()


if __name__ == "__main__":
    main()
