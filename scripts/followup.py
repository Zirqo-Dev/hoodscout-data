#!/usr/bin/env python3
"""HS-95 step 2: follow-up watch.

Keeps observing tokens for 7 days after we first see them, so a token that
falls off our lists is still watched. Raw data only: pool attributes and
hourly candles are stored verbatim, and nothing here computes a return, gap
or outcome. Reads data/history.jsonl for ts, ca and main_pool only (never
tier, scores or hypothesis arms) and writes only under data/followup/.

Each run:
  ENROLL    tokens with >= 4 distinct snapshot ts whose first_seen is within
            the last 7 days go into enrolled.json once. Entries are frozen.
  WATCH     enrolled tokens still inside first_seen + 7 d: one multi-pool
            call per 20 pools, attributes stored verbatim.
  BACKFILL  enrolled tokens past first_seen + 7 d + 1 h with no candle file:
            hourly OHLCV up to that point, oldest first, at most 8 per run.

Rate: 6 s between calls, 30 calls per run including the retry. The first 429
of a run waits 30 s and is retried once; any further 429 stops calling for
the rest of the run. Errors are counted and the run moves on; a failed
backfill is simply still due next run.

The GeckoTerminal base URL and headers are copied from scripts/collect.py,
which is deliberately not imported.

Usage: python scripts/followup.py [--dry-run]
"""
import argparse, gzip, io, json, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

NETWORK = "robinhood"
BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.2"}

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HISTORY = os.path.join(ROOT, "data", "history.jsonl")
FOLLOWUP = os.path.join(ROOT, "data", "followup")

MIN_SNAPSHOTS = 4                  # decided by Pete 2026-10-23
WINDOW = timedelta(days=7)
BACKFILL_DELAY = timedelta(hours=1)
BATCH = 20
MAX_BACKFILLS = 8
MAX_CALLS = 30
SLEEP_BETWEEN = 6
RETRY_429_WAIT = 30


class Stop(Exception):
    """No more calls this run (cap reached or a second 429)."""


def parse_ts(s):
    return datetime.fromisoformat(s)


def iso(dt):
    return dt.isoformat()


# ------------------------------------------------------------------ enroll

def scan_history(path):
    """{ca: {"first", "pool", "snaps"}} from ts, ca and main_pool only.

    pool is main_pool of the token's earliest row that has one; snaps counts
    distinct ts values."""
    seen = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            ts, ca, pool = parse_ts(r["ts"]), r["ca"], r.get("main_pool")
            t = seen.setdefault(ca, {"first": ts, "pool": None, "pool_ts": None,
                                     "tss": set()})
            t["tss"].add(ts)
            t["first"] = min(t["first"], ts)
            if pool and (t["pool_ts"] is None or ts < t["pool_ts"]):
                t["pool"], t["pool_ts"] = pool, ts
    return {ca: {"first": t["first"], "pool": t["pool"], "snaps": len(t["tss"])}
            for ca, t in seen.items()}


def enroll(enrolled, hist, now):
    """Append newly qualifying tokens; never edits or removes an entry.
    Returns the list of new entries."""
    known = {e["ca"] for e in enrolled}
    new = []
    for ca in sorted(hist, key=lambda c: (hist[c]["first"], c)):
        t = hist[ca]
        if ca in known or t["snaps"] < MIN_SNAPSHOTS or not t["pool"]:
            continue
        if now - t["first"] >= WINDOW:
            continue
        new.append({"ca": ca, "main_pool": t["pool"], "first_seen": iso(t["first"]),
                    "enrolled_at": iso(now)})
    enrolled.extend(new)
    return new


def active(enrolled, now):
    return [e for e in enrolled if now < parse_ts(e["first_seen"]) + WINDOW]


def candle_path(pool):
    return os.path.join(FOLLOWUP, "candles", f"{pool}.json")


def due_backfills(enrolled, now):
    """Past first_seen + 7 d + 1 h with no candle file, oldest first, one per pool."""
    seen, due = set(), []
    for e in sorted(enrolled, key=lambda e: (parse_ts(e["first_seen"]), e["ca"])):
        p = e["main_pool"]
        if p in seen or now < parse_ts(e["first_seen"]) + WINDOW + BACKFILL_DELAY:
            continue
        seen.add(p)
        if not os.path.exists(candle_path(p)):
            due.append(e)
    return due


def batches(pools, n=BATCH):
    return [pools[i:i + n] for i in range(0, len(pools), n)]


# ------------------------------------------------------------------ network

class Client:
    """Every request goes through here: spacing, call cap, 429 rule."""

    def __init__(self, max_calls=MAX_CALLS):
        self.max_calls = max_calls
        self.calls = 0
        self.http_429 = 0
        self.errors = 0
        self.retried = False
        self.stopped = False

    def _open(self, path):
        req = urllib.request.Request(BASE + path, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def get(self, path):
        """Parsed body, or None on a counted error. Raises Stop when no more
        calls may be made this run."""
        while True:
            if self.stopped or self.calls >= self.max_calls:
                self.stopped = True
                raise Stop()
            if self.calls:
                time.sleep(SLEEP_BETWEEN)
            self.calls += 1
            try:
                return self._open(path)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    self.errors += 1
                    return None
                self.http_429 += 1
                if self.retried:
                    self.stopped = True
                    raise Stop()
                self.retried = True
                time.sleep(RETRY_429_WAIT)
            except Exception:   # network, timeout, malformed JSON
                self.errors += 1
                return None


# ------------------------------------------------------------------ run

def atomic_write(path, data):
    """Write bytes to a temp file in the same directory, then os.replace, so
    a crash mid-write leaves the previous file (or none) in place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def json_bytes(obj, **kw):
    return (json.dumps(obj, **kw) + "\n").encode()


def load_enrolled():
    p = os.path.join(FOLLOWUP, "enrolled.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_enrolled(enrolled):
    atomic_write(os.path.join(FOLLOWUP, "enrolled.json"), json_bytes(enrolled, indent=1))


def watch(client, act, run_ts, lines, meta):
    """Multi-pool calls for active pools; appends verbatim rows to lines."""
    by_pool = {}
    for e in act:
        by_pool.setdefault(e["main_pool"], []).append(e["ca"])
    pools = sorted(by_pool)
    meta["pools_requested"] = len(pools)
    returned = set()
    try:
        for chunk in batches(pools):
            body = client.get(f"/networks/{NETWORK}/pools/multi/{','.join(chunk)}")
            if body is None:
                continue
            wanted = {p.lower(): p for p in chunk}
            for item in body.get("data") or []:
                attrs = (item or {}).get("attributes") or {}
                pool = wanted.get(str(attrs.get("address", "")).lower())
                if pool is None:
                    continue
                returned.add(pool)
                for ca in by_pool[pool]:
                    lines.append({"ts": run_ts, "ca": ca, "pool": pool,
                                  "attributes": attrs})
    finally:
        meta["pools_returned"] = len(returned)
        meta["pools_not_returned"] = [p for p in pools if p not in returned]


def backfill(client, due, now, meta):
    for e in due[:MAX_BACKFILLS]:
        pool = e["main_pool"]
        before = int((parse_ts(e["first_seen"]) + WINDOW + BACKFILL_DELAY).timestamp())
        try:
            body = client.get(f"/networks/{NETWORK}/pools/{pool}/ohlcv/hour"
                              f"?limit=200&before_timestamp={before}")
        except Stop:
            break
        try:
            ohlcv = body["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError):
            meta["backfills_failed"] += 1
            continue
        atomic_write(candle_path(pool), json_bytes(
            {"ca": e["ca"], "pool": pool, "first_seen": e["first_seen"],
             "fetched_at": iso(datetime.now(timezone.utc)),
             "before_timestamp": before, "ohlcv_list": ohlcv}))
        meta["backfills_done"] += 1


def write_watch(lines, meta, now):
    stem = os.path.join(FOLLOWUP, "watch", now.strftime("%Y-%m-%d"), now.strftime("%H%M%S"))
    if lines:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            for row in lines:
                gz.write(json_bytes(row, separators=(",", ":")))
        atomic_write(stem + ".jsonl.gz", buf.getvalue())
    atomic_write(stem + ".meta.json", json_bytes(meta, indent=1))
    return stem


def plan(now=None):
    """What a run would do, with no network and no writes."""
    now = now or datetime.now(timezone.utc)
    enrolled = load_enrolled()
    new = enroll(enrolled, scan_history(HISTORY), now)
    act = active(enrolled, now)
    due = due_backfills(enrolled, now)
    n_watch = len(batches(sorted({e["main_pool"] for e in act})))
    n_back = min(len(due), MAX_BACKFILLS, max(0, MAX_CALLS - n_watch))
    return {"enrolled_total": len(enrolled), "newly_enrolled": len(new),
            "active_in_window": len(act), "active_pools": len({e["main_pool"] for e in act}),
            "backfills_due": len(due), "planned_calls": n_watch + n_back}


def run(now=None, client=None):
    """One follow-up run. Returns the exit code."""
    t0 = time.monotonic()
    now = now or datetime.now(timezone.utc)
    client = client or Client()
    lines = []
    meta = {"run_ts": iso(now), "enrolled_total": 0, "active_in_window": 0,
            "newly_enrolled": 0, "calls": 0, "http_429": 0, "errors": 0,
            "pools_requested": 0, "pools_returned": 0, "pools_not_returned": [],
            "backfills_done": 0, "backfills_failed": 0, "backfills_pending": 0,
            "stopped_early": False, "crashed": None, "runtime_s": None}
    code = 0
    try:
        enrolled = load_enrolled()
        new = enroll(enrolled, scan_history(HISTORY), now)
        if new:
            save_enrolled(enrolled)
        act = active(enrolled, now)
        meta.update(enrolled_total=len(enrolled), newly_enrolled=len(new),
                    active_in_window=len(act))
        due = due_backfills(enrolled, now)
        try:
            watch(client, act, iso(now), lines, meta)
        except Stop:
            pass
        backfill(client, due, now, meta)
        meta["backfills_pending"] = len(due_backfills(enrolled, now))
    except Exception as e:
        meta["crashed"] = f"{type(e).__name__}: {e}"
        code = 1
    finally:
        meta.update(calls=client.calls, http_429=client.http_429,
                    errors=client.errors, stopped_early=client.stopped,
                    runtime_s=round(time.monotonic() - t0, 1))
        stem = write_watch(lines, meta, now)
    print(json.dumps({k: v for k, v in meta.items() if k != "pools_not_returned"}))
    print(f"wrote {os.path.relpath(stem, ROOT)}.*")
    return code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; no network calls, no writes")
    args = ap.parse_args()
    if args.dry_run:
        print(json.dumps(plan(), indent=1))
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
