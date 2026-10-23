#!/usr/bin/env python3
"""HS-95 step 1b: one-off, read-only probe of GeckoTerminal hourly OHLCV for
pools whose tokens dropped off our lists after a real watch period.

Questions: do hourly candles exist for those pools, do they cover the time we
watched the token (first-seen to last-seen), do they continue after last-seen,
and does the multi-pool endpoint return them in one call. Process facts only.
No return, price change or outcome is computed, and tier and hypothesis arms
are never read. Candle prices and volumes are checked for presence and
discarded; they appear in no output. Pool liquidity (reserve_in_usd) is reduced
to a category (missing / under 1,000 / 1,000 or more); the value is neither
printed nor stored.

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
MIN_SNAPSHOTS = 8       # distinct ts values a token must appear in
N_POOLS = 15
MAX_CALLS = 25          # every HTTP request counts, retries included
SLEEP_BETWEEN = 6
MULTI_MAX = 30          # addresses per multi-pool call; UNVERIFIED (docs unreachable from here)
LIQ_THRESHOLD = 1000
LIQ_CATEGORIES = ("missing", "under_1000", "1000_or_more")
RETRY_429_WAIT = 30
WINDOW = range(4, 11)   # minutes past a quarter hour; keeps clear of collect


def parse_ts(s):
    return datetime.fromisoformat(s)


def token_history(path):
    """{ca: {"first", "last", "pool", "snaps"}}; touches only ts, ca, main_pool.
    pool is main_pool of the row with the latest ts; snaps counts distinct ts."""
    seen = {}
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            ts = parse_ts(r["ts"])
            t = seen.get(r["ca"])
            if t is None:
                seen[r["ca"]] = t = {"first": ts, "last": ts, "pool": r.get("main_pool"),
                                     "tss": set()}
            t["tss"].add(ts)
            t["first"] = min(t["first"], ts)
            if ts >= t["last"]:
                t["last"], t["pool"] = ts, r.get("main_pool")
    for t in seen.values():
        t["snaps"] = len(t.pop("tss"))
    newest = max(t["last"] for t in seen.values())
    return seen, newest


def pick_pools(hist, newest, n=N_POOLS):
    """Tokens seen in >= MIN_SNAPSHOTS distinct snapshots and last seen
    >= 48 h before the newest ts, sorted by last-seen, n evenly spaced. One
    token per pool so no pool is probed twice.
    Returns (picked, n_tokens_qualifying, n_pools_qualifying)."""
    cutoff = newest - timedelta(hours=DROPPED_HOURS)
    qualifying = [(ca, t) for ca, t in hist.items()
                  if t["snaps"] >= MIN_SNAPSHOTS and t["last"] <= cutoff]
    seen_pools, cands = set(), []
    for ca, t in sorted(qualifying, key=lambda kv: (kv[1]["last"], kv[0])):
        if not t["pool"] or t["pool"] in seen_pools:
            continue
        seen_pools.add(t["pool"])
        cands.append({"ca": ca, "pool": t["pool"], "first": t["first"],
                      "last": t["last"], "snaps": t["snaps"]})
    if len(cands) <= n:
        return cands, len(qualifying), len(cands)
    idx = [round(i * (len(cands) - 1) / (n - 1)) for i in range(n)]
    return [cands[i] for i in idx], len(qualifying), len(cands)


def wait_for_window():
    while True:
        m = datetime.now(timezone.utc).minute
        if m % 15 in WINDOW:
            return
        time.sleep(5)


def get(path, calls, tally):
    """GET with the shared call cap and a single 429 retry.
    Returns (status, parsed body or None, error)."""
    for attempt in (0, 1):
        if calls[0] >= MAX_CALLS:
            return None, None, "call cap reached"
        calls[0] += 1
        try:
            req = urllib.request.Request(BASE + path, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r), None
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


def fetch(pool, calls, tally):
    """One pool's hourly candles. Returns (status, candle list or None, error)."""
    status, body, err = get(
        f"/networks/{NETWORK}/pools/{pool}/ohlcv/hour?limit=1000", calls, tally)
    if body is None:
        return status, None, err
    try:
        return status, body["data"]["attributes"]["ohlcv_list"], None
    except (KeyError, TypeError):
        tally["errors"] += 1
        return status, None, "malformed body"


def liq_category(v):
    """reserve_in_usd reduced to a category; the value never leaves this function."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "missing"
    return "under_1000" if x < LIQ_THRESHOLD else "1000_or_more"


def fetch_multi(pools, calls, tally):
    """Batching test: the multi-pool endpoint, split at MULTI_MAX addresses.
    Returns ({lowercase pool: category} for returned pools, worked bool, [errors])."""
    found, errs, ok = {}, [], True
    for i in range(0, len(pools), MULTI_MAX):
        if i:
            time.sleep(SLEEP_BETWEEN)
        chunk = pools[i:i + MULTI_MAX]
        status, body, err = get(
            f"/networks/{NETWORK}/pools/multi/{','.join(chunk)}", calls, tally)
        if body is None:
            ok = False
            errs.append(err)
            continue
        try:
            for item in body["data"]:
                a = item["attributes"]
                found[a["address"].lower()] = liq_category(a.get("reserve_in_usd"))
        except (KeyError, TypeError, AttributeError):
            tally["errors"] += 1
            ok = False
            errs.append("malformed body")
    return found, ok, errs


def describe(candles, first_seen, last_seen):
    """Process facts about a candle list: count, span, coverage while watched
    (hour of first-seen through last-seen) and after last-seen, gaps."""
    times = sorted(c[0] for c in candles)
    fs = first_seen.replace(minute=0, second=0, microsecond=0).timestamp()
    ls = last_seen.timestamp()
    gaps = [(b - a) / 3600 for a, b in zip(times, times[1:])]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    return {
        "candles": len(times),
        "first_candle": iso(times[0]) if times else None,
        "last_candle": iso(times[-1]) if times else None,
        "candles_while_watched": sum(1 for t in times if fs <= t <= ls),
        "candles_after_last_seen": sum(1 for t in times if t > ls),
        "max_gap_hours": max(gaps) if gaps else None,
        "incomplete_candles": sum(
            1 for c in candles
            if len(c) < 6 or any(v is None for v in c[:6])),
    }


def summary_md(results, totals):
    lines = ["## HS-95 OHLCV probe (1b)", "",
             "| pool | snaps | last seen (UTC) | HTTP | candles | first candle | last candle "
             "| while watched | after last-seen | max gap (h) | incomplete | multi | liquidity |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| {pool} | {sn} | {ls} | {st} | {n} | {f} | {l} | {w} | {a} | {g} | {i} | {m} | {q} |".format(
            pool=r["pool"], sn=r["snapshots"], ls=r["last_seen"][:16],
            st=r["status"] or r["error"],
            n=r.get("candles", ""), f=(r.get("first_candle") or "")[:16],
            l=(r.get("last_candle") or "")[:16], w=r.get("candles_while_watched", ""),
            a=r.get("candles_after_last_seen", ""), g=r.get("max_gap_hours", ""),
            i=r.get("incomplete_candles", ""),
            m="yes" if r["multi_returned"] else "no", q=r["liquidity"]))
    lines += ["", "```"] + totals_lines(totals) + ["```"]
    return "\n".join(lines) + "\n"


def totals_lines(t):
    c = t["liquidity_categories"]
    return [f"tokens qualifying:          {t['tokens_qualifying']} (pools {t['pools_qualifying']})",
            f"pools probed:               {t['pools_probed']}",
            f"pools with candles:         {t['pools_with_candles']}",
            f"median candles while watched (HTTP 200 pools):    {t['median_candles_while_watched']}",
            f"median candles after last-seen (HTTP 200 pools):  {t['median_candles_after_last_seen']}",
            f"oldest candle seen:         {t['oldest_candle']}",
            f"multi-pool call worked:     {'yes' if t['multi_call_worked'] else 'no'}",
            "liquidity categories:       " + ", ".join(f"{k} {c[k]}" for k in LIQ_CATEGORIES),
            f"429 responses:              {t['http_429']}",
            f"errors (non-429):           {t['errors']}",
            f"HTTP calls made:            {t['calls']}"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="ohlcv_probe.json")
    args = ap.parse_args()

    hist, newest = token_history(HISTORY)
    picked, n_tok, n_pools = pick_pools(hist, newest)
    print(f"newest ts {newest.isoformat()}; {len(hist)} tokens; {n_tok} qualify "
          f"(>= {MIN_SNAPSHOTS} snapshots, last seen >= {DROPPED_HOURS} h ago; "
          f"{n_pools} distinct pools); picked {len(picked)}")
    for p in picked:
        print(f"  {p['pool']}  snapshots {p['snaps']}  last seen {p['last'].isoformat()}")
    if args.dry_run:
        return 0

    wait_for_window()
    calls, tally, results = [0], {"429": 0, "errors": 0}, []
    for i, p in enumerate(picked):
        if i:
            time.sleep(SLEEP_BETWEEN)
        status, candles, err = fetch(p["pool"], calls, tally)
        row = {"ca": p["ca"], "pool": p["pool"], "snapshots": p["snaps"],
               "first_seen": p["first"].isoformat(), "last_seen": p["last"].isoformat(),
               "status": status, "error": err}
        if candles is not None:
            row.update(describe(candles, p["first"], p["last"]))
        results.append(row)
        print(f"  {p['pool'][:12]}… HTTP {status} candles {row.get('candles', '-')}")

    time.sleep(SLEEP_BETWEEN)
    found, multi_ok, multi_errs = fetch_multi([p["pool"] for p in picked], calls, tally)
    for row in results:
        cat = found.get(row["pool"].lower())
        row["multi_returned"] = cat is not None
        row["liquidity"] = cat or "missing"
    print(f"  multi-pool call worked: {multi_ok} {multi_errs or ''}")

    ok = [r for r in results if r["status"] == 200]
    firsts = [r["first_candle"] for r in ok if r.get("first_candle")]
    med = lambda k: statistics.median(r[k] for r in ok) if ok else None
    totals = {
        "tokens_qualifying": n_tok, "pools_qualifying": n_pools,
        "pools_probed": len(results),
        "pools_with_candles": sum(1 for r in ok if r["candles"] > 0),
        "median_candles_while_watched": med("candles_while_watched"),
        "median_candles_after_last_seen": med("candles_after_last_seen"),
        "oldest_candle": min(firsts) if firsts else None,
        "multi_call_worked": multi_ok,
        "liquidity_categories": {k: sum(1 for r in results if r["liquidity"] == k)
                                 for k in LIQ_CATEGORIES},
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
