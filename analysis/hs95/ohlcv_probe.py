#!/usr/bin/env python3
"""HS-95 step 1: one-off, read-only probe of GeckoTerminal hourly OHLCV for
pools whose tokens dropped off our lists.

Question: do hourly candles exist for those pools, how far back do they go,
and do they continue after the token's last-seen time? Process facts only.
No return, price change or outcome is computed, and tier and hypothesis arms
are never read. Candle prices and volumes are checked for presence and
discarded; they appear in no output.

GeckoTerminal is not reachable from the authoring environment, so this is run
by .github/workflows/hs95-ohlcv-probe.yml. --dry-run skips the wait and every
network call and only prints the pool selection.

Usage: python analysis/hs95/ohlcv_probe.py [--dry-run] [--out PATH]
"""
import argparse, json, os, statistics, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

# Same base URL and headers as scripts/collect.py (read there, not imported).
NETWORK = "robinhood"
BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.2"}

HISTORY = os.path.join(os.path.dirname(__file__), "..", "..", "data", "history.jsonl")
DROPPED_HOURS = 48
N_POOLS = 20
MAX_CALLS = 25          # every HTTP request counts, retries included
SLEEP_BETWEEN = 3
RETRY_429_WAIT = 30
WINDOW = range(4, 11)   # minutes past a quarter hour; keeps clear of collect


def parse_ts(s):
    return datetime.fromisoformat(s)


def last_seen_per_token(path):
    """{ca: (last_seen_dt, main_pool of the last row)}; touches only ts, ca, main_pool."""
    last = {}
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            ts = parse_ts(r["ts"])
            cur = last.get(r["ca"])
            if cur is None or ts >= cur[0]:
                last[r["ca"]] = (ts, r.get("main_pool"))
    newest = max(v[0] for v in last.values())
    return last, newest


def pick_pools(last, newest, n=N_POOLS):
    """Dropped-off tokens (last seen >= 48 h before the newest ts), sorted by
    last-seen, n evenly spaced. One token per pool so no pool is probed twice."""
    cutoff = newest - timedelta(hours=DROPPED_HOURS)
    seen_pools, dropped = set(), []
    for ca, (ts, pool) in sorted(last.items(), key=lambda kv: (kv[1][0], kv[0])):
        if ts > cutoff or not pool or pool in seen_pools:
            continue
        seen_pools.add(pool)
        dropped.append((ts, ca, pool))
    if len(dropped) <= n:
        return dropped, len(dropped)
    idx = [round(i * (len(dropped) - 1) / (n - 1)) for i in range(n)]
    return [dropped[i] for i in idx], len(dropped)


def wait_for_window():
    while True:
        m = datetime.now(timezone.utc).minute
        if m % 15 in WINDOW:
            return
        time.sleep(5)


def fetch(pool, calls, tally):
    """One pool's hourly candles. Returns (status, candle list or None, error)."""
    path = f"/networks/{NETWORK}/pools/{pool}/ohlcv/hour?limit=1000"
    for attempt in (0, 1):
        if calls[0] >= MAX_CALLS:
            return None, None, "call cap reached"
        calls[0] += 1
        try:
            req = urllib.request.Request(BASE + path, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
            return r.status, body["data"]["attributes"]["ohlcv_list"], None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                tally["429"] += 1
                if attempt == 0:
                    time.sleep(RETRY_429_WAIT)
                    continue
            return e.code, None, f"HTTP {e.code}"
        except Exception as e:   # network, timeout, malformed body
            tally["errors"] += 1
            return None, None, type(e).__name__
    return 429, None, "HTTP 429"


def describe(candles, last_seen):
    """Process facts about a candle list: count, span, coverage after last-seen, gaps."""
    times = sorted(c[0] for c in candles)
    ls = last_seen.timestamp()
    gaps = [(b - a) / 3600 for a, b in zip(times, times[1:])]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    return {
        "candles": len(times),
        "first_candle": iso(times[0]) if times else None,
        "last_candle": iso(times[-1]) if times else None,
        "candles_after_last_seen": sum(1 for t in times if t > ls),
        "max_gap_hours": max(gaps) if gaps else None,
        "incomplete_candles": sum(
            1 for c in candles
            if len(c) < 6 or any(v is None for v in c[:6])),
    }


def summary_md(results, totals):
    lines = ["## HS-95 OHLCV probe", "",
             "| pool | last seen (UTC) | HTTP | candles | first candle | last candle "
             "| after last-seen (h) | max gap (h) | incomplete |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| {pool} | {ls} | {st} | {n} | {f} | {l} | {a} | {g} | {i} |".format(
            pool=r["pool"], ls=r["last_seen"][:16], st=r["status"] or r["error"],
            n=r.get("candles", ""), f=(r.get("first_candle") or "")[:16],
            l=(r.get("last_candle") or "")[:16], a=r.get("candles_after_last_seen", ""),
            g=r.get("max_gap_hours", ""), i=r.get("incomplete_candles", "")))
    lines += ["", "```"] + totals_lines(totals) + ["```"]
    return "\n".join(lines) + "\n"


def totals_lines(t):
    return [f"pools probed:               {t['pools_probed']}",
            f"pools with candles:         {t['pools_with_candles']}",
            f"median candles after last-seen (HTTP 200 pools): {t['median_candles_after_last_seen']}",
            f"oldest candle seen:         {t['oldest_candle']}",
            f"429 responses:              {t['http_429']}",
            f"errors (non-429):           {t['errors']}",
            f"HTTP calls made:            {t['calls']}"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="ohlcv_probe.json")
    args = ap.parse_args()

    last, newest = last_seen_per_token(HISTORY)
    picked, n_dropped = pick_pools(last, newest)
    print(f"newest ts {newest.isoformat()}; {len(last)} tokens, "
          f"{n_dropped} dropped off (>= {DROPPED_HOURS} h); picked {len(picked)}")
    for ts, ca, pool in picked:
        print(f"  {pool}  last seen {ts.isoformat()}")
    if args.dry_run:
        return 0

    wait_for_window()
    calls, tally, results = [0], {"429": 0, "errors": 0}, []
    for i, (ts, ca, pool) in enumerate(picked):
        if i:
            time.sleep(SLEEP_BETWEEN)
        status, candles, err = fetch(pool, calls, tally)
        row = {"ca": ca, "pool": pool, "last_seen": ts.isoformat(),
               "status": status, "error": err}
        if candles is not None:
            row.update(describe(candles, ts))
        results.append(row)
        print(f"  {pool[:12]}… HTTP {status} candles {row.get('candles', '-')}")

    ok = [r for r in results if r["status"] == 200]
    firsts = [r["first_candle"] for r in ok if r.get("first_candle")]
    totals = {
        "pools_probed": len(results),
        "pools_with_candles": sum(1 for r in ok if r["candles"] > 0),
        "median_candles_after_last_seen":
            statistics.median(r["candles_after_last_seen"] for r in ok) if ok else None,
        "oldest_candle": min(firsts) if firsts else None,
        "http_429": tally["429"], "errors": tally["errors"], "calls": calls[0],
    }
    print()
    print("\n".join(totals_lines(totals)))
    with open(args.out, "w") as f:
        json.dump({"newest_history_ts": newest.isoformat(), "totals": totals,
                   "pools": results}, f, indent=2)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(summary_md(results, totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
