#!/usr/bin/env python3
"""Blockscout holder and deployer collector for Robinhood Chain.

Writes only under data/holders/. Nothing here feeds a tier, a score or the
dashboard: it exists so that holder concentration can be tested later as a
pre-registered hypothesis (H-0004), and it stores raw balances rather than a
computed concentration so the exclusion set can be chosen at evaluation time
rather than baked in now.

ENDPOINTS AND AUTH -- partially verified, 2026-10-19.
api.blockscout.com is not reachable from the authoring sandbox (DNS and the
egress proxy both refuse it), so nothing below was exercised against the live
API. What was confirmed from Blockscout's published API reference:

  * the PRO host is https://api.blockscout.com/{chain_id}/api/v2/...
  * the key goes in an `apikey` query parameter OR an `authorization` header,
    and keys begin with `proapi_`
  * every response carries x-credits-remaining, x-ratelimit-limit,
    x-ratelimit-remaining and x-ratelimit-reset
  * the default cost is 20 credits per call for any endpoint without its own
    listed price; the free tier is 100,000 credits/day and 5 requests/second
  * a 429 means the per-second limit was exceeded
  * the holders response is {"items": [...], "next_page_params": {...}} and an
    item carries a nested address object plus a raw `value` string
  * an address response carries creator_address_hash and a creation tx hash

What was NOT confirmed, and is therefore parsed defensively below: whether the
holders item's address key is `address_hash` or `address`, whether the nested
object uses `hash`, and the exact spelling of the creation-transaction field.
The parser accepts every spelling seen in the docs and examples and falls back
to None rather than raising. `--probe` prints the real keys from two or three
tokens so the first Actions run settles it.

The key is sent as a header, never in the URL, so it cannot reach a log line
through an exception's URL or through a printed path.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

CHAIN_ID = "4663"
BASE = "https://api.blockscout.com/" + CHAIN_ID + "/api/v2"
UA = "hoodscout-data/0.1 (+https://github.com/Zirqo-Dev/hoodscout-data)"

OUT_DIR = "data/holders"
DEPLOYER_CACHE = OUT_DIR + "/deployers.json"
HINT_PATH = OUT_DIR + "/exclusions_hint.json"

MAX_TOKENS = 120            # ordered by liquidity, DEAD and AVOID dropped
TOP_HOLDERS = 20            # stored per token, raw
MAX_CALLS_PER_RUN = 300     # hard stop, whatever the token count says
MIN_INTERVAL = 0.25         # 4 req/s, under the free tier's 5
MAX_429_IN_A_ROW = 3        # then the run gives up and writes nothing
TIMEOUT = 25
SKIP_TIERS = {"DEAD", "AVOID"}

# A null deployer is cached too, so a token whose creator the API does not know
# is not re-asked every three hours; it is retried once a week instead.
DEPLOYER_RETRY_DAYS = 7

# exclusions_hint.json: contracts that sit in the top 3 of this many different
# tokens are almost certainly shared infrastructure -- the Uniswap v4
# PoolManager, a router, a burn address. Reported, never applied here.
HINT_TOP_N = 3
HINT_MIN_TOKENS = 5


def iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


class Api:
    """One place for the key, the throttle, the call cap and the 429 rule."""

    def __init__(self, key, cap=MAX_CALLS_PER_RUN):
        self.key = key
        self.cap = cap
        self.calls = 0
        self.last = 0.0
        self.streak_429 = 0
        self.gave_up = False
        self.credits = None
        self.errors = {}

    def _note(self, kind):
        self.errors[kind] = self.errors.get(kind, 0) + 1

    def get(self, path, params=None, probe=False):
        """Returns (payload, headers) or (None, None). Never raises for an API
        problem: the caller counts it and carries on."""
        if self.gave_up or self.calls >= self.cap:
            return None, None
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            gap = MIN_INTERVAL - (time.time() - self.last)
            if gap > 0:
                time.sleep(gap)
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": UA,
                # header, not a query parameter: an exception's URL ends up in
                # the Actions log, and a key in the query string would go with it
                "authorization": self.key,
            })
            self.calls += 1
            self.last = time.time()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = json.load(r)
                    hdrs = dict(r.headers)
                    self.streak_429 = 0
                    rem = hdrs.get("x-credits-remaining")
                    if rem is not None:
                        self.credits = rem
                    if probe:
                        print("  %s -> %s" % (path, r.status))
                        for h in ("x-credits-remaining", "x-ratelimit-limit",
                                  "x-ratelimit-remaining", "x-ratelimit-reset"):
                            if h in hdrs:
                                print("    %s: %s" % (h, hdrs[h]))
                        print("    top-level keys: %s"
                              % (sorted(body) if isinstance(body, dict) else type(body).__name__))
                    return body, hdrs
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.streak_429 += 1
                    self._note("429")
                    if self.streak_429 >= MAX_429_IN_A_ROW:
                        self.gave_up = True
                        print("WARN %d consecutive 429s, giving up for this run"
                              % self.streak_429)
                        return None, None
                    wait = 5.0
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra:
                        try:
                            wait = max(1.0, min(60.0, float(ra)))
                        except ValueError:
                            pass
                    print("  429 on %s, waiting %.0fs" % (path, wait))
                    time.sleep(wait)
                    continue
                if e.code in (401, 403):
                    self._note("auth_%d" % e.code)
                    print("WARN %d from Blockscout: the key was rejected" % e.code)
                    self.gave_up = True
                    return None, None
                if e.code == 404:
                    self._note("404")
                    return None, None
                self._note("http_%d" % e.code)
                if probe:
                    print("  %s -> HTTP %d" % (path, e.code))
                return None, None
            except Exception as e:                      # noqa: BLE001
                self._note(type(e).__name__)
                if probe:
                    print("  %s -> %s" % (path, type(e).__name__))
                if attempt == 2:
                    return None, None
                time.sleep(2.0 * (attempt + 1))
        return None, None


# ---------------------------------------------------------------- parsing
# Written against the documented shape and tolerant of the parts that the
# published reference leaves open. Every accessor falls back to None.

def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _addr_obj(item):
    """The holders item's address. The reference example nests it under
    `address_hash`; other Blockscout versions use `address`. Either may be a
    bare string instead of an object."""
    a = _first(item, "address_hash", "address", "owner", "holder")
    if isinstance(a, str):
        return {"hash": a}
    return a if isinstance(a, dict) else {}


def parse_holders(payload, limit=TOP_HOLDERS):
    """-> list of {address, balance, is_contract, label}. balance stays the raw
    string the API sent: it is an integer in token base units and converting it
    here would need decimals we may not have."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        a = _addr_obj(it)
        h = _first(a, "hash", "address_hash", "address")
        if not isinstance(h, str) or not h:
            continue
        val = _first(it, "value", "balance", "amount")
        label = _first(a, "name", "ens_domain_name")
        if label is None:
            tags = _first(a, "public_tags", "private_tags", "watchlist_names")
            if isinstance(tags, list) and tags:
                t0 = tags[0]
                label = t0.get("display_name") if isinstance(t0, dict) else t0
        out.append({
            "address": h.lower(),
            "balance": str(val) if val is not None else None,
            "is_contract": bool(a.get("is_contract")) if "is_contract" in a else None,
            "label": label if isinstance(label, str) else None,
        })
    return out


def parse_deployer(payload):
    """-> {'deployer': str|None, 'creation_tx': str|None}."""
    if not isinstance(payload, dict):
        return {"deployer": None, "creation_tx": None}
    c = _first(payload, "creator_address_hash", "creator_address", "creator")
    if isinstance(c, dict):
        c = _first(c, "hash", "address_hash")
    tx = _first(payload, "creation_tx_hash", "creation_transaction_hash",
                "creation_tx", "creation_transaction")
    if isinstance(tx, dict):
        tx = _first(tx, "hash")
    return {"deployer": c.lower() if isinstance(c, str) else None,
            "creation_tx": tx if isinstance(tx, str) else None}


# ---------------------------------------------------------------- inputs

def load_tokens(path="data/latest.json"):
    """The scored board minus the tiers we would never research: ordered by
    liquidity so the cap keeps the part of the list anyone would look at."""
    try:
        d = json.load(open(path))
    except (OSError, ValueError) as e:
        print("WARN cannot read %s: %s" % (path, e))
        return []
    rows = [t for t in (d.get("tokens") or [])
            if t.get("ca") and t.get("tier") not in SKIP_TIERS]
    rows.sort(key=lambda t: -(t.get("liq_usd") or 0))
    return rows[:MAX_TOKENS]


def load_supplies(path="data/stocks_latest.json"):
    """total_supply where the pipeline already has it. stocks.py reads
    totalSupply() over the public RPC for the five stock tokens; no other token
    has an on-chain supply in this repo, so the rest store null rather than
    spending a Blockscout call per token per run to learn it."""
    out = {}
    try:
        d = json.load(open(path))
    except (OSError, ValueError):
        return out
    for t in (d.get("tokens") or []):
        ca, sup = (t.get("ca") or "").lower(), t.get("supply")
        if ca and sup is not None:
            out[ca] = sup
    return out


def load_cache(path=DEPLOYER_CACHE):
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def needs_deployer(ca, cache, now):
    """Never re-ask for a known creator. A null is retried weekly, because the
    API not knowing today is not proof it will never know."""
    e = cache.get(ca)
    if not isinstance(e, dict):
        return True
    if e.get("deployer"):
        return False
    try:
        asked = datetime.fromisoformat(e.get("asked_at"))
    except (TypeError, ValueError):
        return True
    return (now - asked).total_seconds() > DEPLOYER_RETRY_DAYS * 86400


# ---------------------------------------------------------------- hint

def build_hint(rows):
    """Addresses that are contracts and sit in the top HINT_TOP_N of at least
    HINT_MIN_TOKENS different tokens. Shared infrastructure looks exactly like
    this. Written out for a human to read; nothing applies it."""
    seen = {}
    for r in rows:
        for h in (r.get("holders") or [])[:HINT_TOP_N]:
            a = h.get("address")
            if not a:
                continue
            e = seen.setdefault(a, {"tokens": set(), "is_contract": False,
                                    "labels": set()})
            e["tokens"].add(r["ca"])
            if h.get("is_contract"):
                e["is_contract"] = True
            if h.get("label"):
                e["labels"].add(h["label"])
    out = []
    for a, e in seen.items():
        if e["is_contract"] and len(e["tokens"]) >= HINT_MIN_TOKENS:
            out.append({"address": a, "tokens": len(e["tokens"]),
                        "labels": sorted(e["labels"])})
    out.sort(key=lambda x: -x["tokens"])
    return out


# ---------------------------------------------------------------- modes

def do_dry_run():
    toks = load_tokens()
    cache = load_cache()
    now = datetime.now(timezone.utc)
    new = [t for t in toks if needs_deployer(t["ca"], cache, now)]
    holder_calls = len(toks)
    print("tokens on the board, excluding %s: %d (cap %d)"
          % ("/".join(sorted(SKIP_TIERS)), len(toks), MAX_TOKENS))
    print("deployers already cached: %d" % len(cache))
    print()
    print("planned calls this run")
    print("  holders   %4d  (1 per token, top %d)" % (holder_calls, TOP_HOLDERS))
    print("  deployers %4d  (uncached or a null older than %d days)"
          % (len(new), DEPLOYER_RETRY_DAYS))
    print("  total     %4d   (hard cap %d)" % (holder_calls + len(new),
                                               MAX_CALLS_PER_RUN))
    print()
    steady = holder_calls * 8
    print("at every 3 hours, 8 runs a day")
    print("  steady state %5d calls/day = %6d credits/day at 20 each"
          % (steady, steady * 20))
    print("  this run     %5d calls     = %6d credits"
          % (holder_calls + len(new), (holder_calls + len(new)) * 20))
    print("  free tier is 100,000 credits/day and 5 req/s; this throttles to "
          "%.0f req/s" % (1.0 / MIN_INTERVAL))
    print()
    print("first 10 by liquidity:")
    for t in toks[:10]:
        print("  %-12s %-44s liq %s"
              % (t.get("symbol"), t["ca"], t.get("liq_usd")))
    return 0


def do_probe(api, n=3):
    toks = load_tokens()[:n]
    if not toks:
        print("no tokens to probe")
        return 0
    print("probing %d tokens against %s" % (len(toks), BASE))
    print("(the key is sent as a header and is never printed)")
    for t in toks:
        ca = t["ca"]
        print("\n%s  %s" % (t.get("symbol"), ca))
        body, _ = api.get("/tokens/%s/holders" % ca,
                          {"limit": TOP_HOLDERS}, probe=True)
        if isinstance(body, dict):
            items = body.get("items")
            if isinstance(items, list) and items:
                print("    item keys:    %s" % sorted(items[0]))
                a = _addr_obj(items[0])
                print("    address keys: %s" % sorted(a) if a else "    address: -")
            parsed = parse_holders(body)
            print("    parsed %d holders; first: %s"
                  % (len(parsed), json.dumps(parsed[0]) if parsed else "-"))
        body, _ = api.get("/addresses/%s" % ca, probe=True)
        if isinstance(body, dict):
            print("    parsed deployer: %s" % json.dumps(parse_deployer(body)))
    print("\ncalls used: %d" % api.calls)
    if api.credits is not None:
        print("credits remaining: %s" % api.credits)
    return 0


def do_collect(api):
    now = datetime.now(timezone.utc)
    toks = load_tokens()
    if not toks:
        print("no tokens to collect; writing nothing")
        return 0
    supplies = load_supplies()
    cache = load_cache()
    cache_before = len(cache)

    rows, ok = [], 0
    for t in toks:
        if api.gave_up:
            break
        ca = t["ca"]
        body, _ = api.get("/tokens/%s/holders" % ca, {"limit": TOP_HOLDERS})
        holders = parse_holders(body)
        if not holders:
            continue
        if needs_deployer(ca, cache, now) and not api.gave_up:
            d, _ = api.get("/addresses/%s" % ca)
            info = parse_deployer(d)
            cache[ca] = {"deployer": info["deployer"],
                         "creation_tx": info["creation_tx"],
                         "asked_at": iso(now)}
        dep = (cache.get(ca) or {}).get("deployer")
        rows.append({
            "ts": iso(now),
            "ca": ca,
            "symbol": t.get("symbol"),
            "pool": t.get("main_pool"),
            "tier": t.get("tier"),
            "n_holders_returned": len(holders),
            "total_supply": supplies.get(ca),
            "holders": holders,
            "deployer": dep,
        })
        ok += 1

    # A run that bailed is a partial cross-section, and a partial
    # cross-section is worse than none: the hypothesis ranks tokens against
    # each other within a snapshot, so a half-filled snapshot would quietly
    # change what "top quartile" means.
    if api.gave_up or not rows:
        print("run incomplete (gave_up=%s, rows=%d); writing nothing"
              % (api.gave_up, len(rows)))
        print("calls %d, errors %s" % (api.calls, api.errors or "none"))
        return 0

    day = now.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(OUT_DIR, day), exist_ok=True)
    path = os.path.join(OUT_DIR, day, now.strftime("%H%M%S") + ".jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")

    json.dump(cache, open(DEPLOYER_CACHE, "w"), indent=1, sort_keys=True)

    hint = build_hint(rows)
    json.dump({"generated_at": iso(now),
               "rule": "contract, and a top-%d holder in >= %d tokens"
                       % (HINT_TOP_N, HINT_MIN_TOKENS),
               "applied": False,
               "addresses": hint},
              open(HINT_PATH, "w"), indent=1)

    print("holders: %d/%d tokens -> %s" % (ok, len(toks), path))
    print("deployers: %d cached (+%d this run)" % (len(cache),
                                                   len(cache) - cache_before))
    print("exclusion hints: %d address(es)" % len(hint))
    for h in hint[:8]:
        print("  %s  in %d tokens  %s" % (h["address"], h["tokens"],
                                          ", ".join(h["labels"]) or "-"))
    print("calls %d/%d, errors %s" % (api.calls, api.cap, api.errors or "none"))
    if api.credits is not None:
        print("credits remaining: %s" % api.credits)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the token list and planned calls, touch no network")
    ap.add_argument("--probe", action="store_true",
                    help="hit a few known tokens and print status, keys and credits")
    a = ap.parse_args()

    if a.dry_run:
        return do_dry_run()

    key = os.environ.get("BLOCKSCOUT_API_KEY", "").strip()
    if not key:
        print("BLOCKSCOUT_API_KEY is not set. Add it as a repository secret; "
              "a Blockscout PRO key starts with proapi_.", file=sys.stderr)
        return 1

    api = Api(key)
    try:
        return do_probe(api) if a.probe else do_collect(api)
    except Exception as e:                              # noqa: BLE001
        # Nothing downstream reads this file, so a bug here must cost nothing
        # more than the run. Exit 0: a red workflow would be a false alarm.
        print("WARN holders run failed: %s: %s" % (type(e).__name__, e))
        return 0


if __name__ == "__main__":
    sys.exit(main())
