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
  * /tokens/{hash} carries decimals, symbol and total_supply
  * /transactions/{hash} exists and its counterparty objects are
    {hash, is_contract, ens_domain_name, tags}

The live runs have now settled every field this collector reads. The
2026-10-19 15:34 run confirmed that the holders item nests its address under
`address_hash` with `hash` inside and that `creator_address_hash` is
populated (119 of 119 tokens); the 18:10 and 21:10 runs confirmed the
transaction object's `from` key, which had been an inference from the `to`
object, by resolving 70 and then 82 creator wallets through it; and
`total_supply` on the token endpoint is populated for 217 of 223 cached
tokens. Nothing here is unverified any more.

The defensive parsing below stays even where the run settled the question:
these are the PRO API's endpoints, the per-instance ones they replace are
marked for deprecation, and a response shape that has already moved once can
move again. Every parser falls back to None rather than raising, and `--probe`
prints the real keys from two or three tokens.

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
TOKEN_META_CACHE = OUT_DIR + "/token_meta.json"
HINT_PATH = OUT_DIR + "/exclusions_hint.json"

MAX_TOKENS = 120            # ordered by liquidity, DEAD and AVOID dropped
TOP_HOLDERS = 20            # stored per token, raw
MAX_CALLS_PER_RUN = 300     # hard stop, whatever the token count says
# Ceiling on the one-time lookups -- token supply and the creator's wallet --
# per run. The actual allowance is the smaller of this and whatever headroom
# the holders and deployer calls leave under MAX_CALLS_PER_RUN, computed at
# run start, so a cold cache spreads over two or three runs and can never be
# the reason a cross-section is abandoned.
BACKFILL_PER_RUN = 150
MIN_INTERVAL = 0.25         # 4 req/s, under the free tier's 5
MAX_429_IN_A_ROW = 3        # then the run gives up early
TIMEOUT = 25
# A 5xx from this API is transient and not about the request: the 2026-10-20
# 01:10, 04:10 and 07:10 runs took 32, 39 and 58 of them and the 10:10 run
# took none, with the same code against the same token set. They also skew
# toward the deepest, best-held tokens -- a holders page that must return 20
# rows is a heavier query than one returning 3 -- so dropping them silently
# cost the cross-section exactly the rows it needs most. Retried.
RETRY_STATUS = (500, 502, 503, 504)
RETRY_WAITS = (2.0, 5.0)    # one attempt, then these; so 3 attempts total
MAX_ATTEMPTS = len(RETRY_WAITS) + 1
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
        if self.gave_up:
            return None, None
        if self.calls >= self.cap:
            # A hard stop, not a quiet one. Returning empties here would let
            # the loop carry on and write a half-filled cross-section, which
            # is the one output this collector must never produce.
            self.gave_up = True
            print("WARN call cap %d reached, giving up for this run" % self.cap)
            return None, None
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_ATTEMPTS):
            if self.calls >= self.cap:
                # Checked per attempt, not just per call: a retry spends the
                # budget like any other request, so the cap has to bind here
                # too or three attempts on the last token could overrun it.
                self.gave_up = True
                print("WARN call cap %d reached mid-retry, giving up" % self.cap)
                return None, None
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
                if e.code != 429:
                    # Any status that is not a 429 says the limiter is not the
                    # thing refusing us, so the streak is broken. Without this,
                    # alternating 429/500 reaches MAX_429_IN_A_ROW and aborts a
                    # run over three 429s that were never consecutive.
                    self.streak_429 = 0
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
                # Counted per attempt, not per call: three 500s on one token
                # are three failed requests and three slots of budget, and a
                # meta file that said "1" would understate what the run cost.
                self._note("http_%d" % e.code)
                if probe:
                    print("  %s -> HTTP %d" % (path, e.code))
                if e.code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                    time.sleep(RETRY_WAITS[attempt])
                    continue
                return None, None
            except Exception as e:                      # noqa: BLE001
                # A timeout leaves the streak alone rather than resetting it:
                # it is not a response and says nothing about the limiter, so
                # it neither confirms rate limiting nor rules it out.
                self._note(type(e).__name__)
                if probe:
                    print("  %s -> %s" % (path, type(e).__name__))
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(RETRY_WAITS[attempt])
                    continue
                return None, None
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


def parse_token_meta(payload):
    """-> {'total_supply': str|None, 'decimals': int|None, 'symbol': str|None}.

    total_supply stays a string for the same reason a balance does: it is an
    integer in base units, often past 2**53, and float is not a container for
    it. decimals is stored for display only -- the registered metric divides a
    raw balance by a raw supply, so the scale cancels and decimals never
    enters the arithmetic."""
    if not isinstance(payload, dict):
        return {"total_supply": None, "decimals": None, "symbol": None}
    sup = _first(payload, "total_supply", "totalSupply", "supply")
    dec = _first(payload, "decimals", "decimal")
    try:
        dec = int(dec) if dec is not None else None
    except (TypeError, ValueError):
        dec = None
    return {"total_supply": str(sup) if sup is not None else None,
            "decimals": dec,
            "symbol": _first(payload, "symbol", "name")}


def parse_tx_sender(payload):
    """-> the wallet that sent the creation transaction, or None.

    `from` was an inference from the `to` object beside it -- the published
    example is truncated before it -- until the 2026-10-19 18:10 and 21:10
    runs resolved 70 and then 82 creator wallets through it. Confirmed.

    The other spellings and the bare-string form are still accepted: these
    are the PRO API's endpoints, the per-instance ones they replace are
    marked for deprecation, and a shape that has moved once can move again."""
    if not isinstance(payload, dict):
        return None
    f = _first(payload, "from", "from_address_hash", "from_address", "sender")
    if isinstance(f, dict):
        f = _first(f, "hash", "address_hash", "address")
    return f.lower() if isinstance(f, str) and f else None


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


def load_meta(path=TOKEN_META_CACHE):
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def needs_meta(ca, meta):
    """Asked once, ever. A token's supply does move -- mints and burns happen
    -- but the registered metric is a ratio of raw balances to raw supply read
    at the same snapshot, and re-reading supply every three hours would cost
    960 extra calls a day to track a number that barely moves for most of
    these. A null is retried, a value never is."""
    e = meta.get(ca)
    return not (isinstance(e, dict) and e.get("total_supply") is not None)


def needs_creator_eoa(ca, cache):
    """The creator wallet behind a creation transaction, resolved once.

    creator_address_hash is whatever sent the deploy, which on this chain is
    usually a factory contract: 119 tokens came back with 44 distinct
    creators, one of them deploying 43. The wallet that called the factory is
    the thing a person would actually recognise, and it is one hop away via
    the creation transaction's sender."""
    e = cache.get(ca)
    if not isinstance(e, dict):
        return False                      # no creation_tx known yet
    if e.get("creator_eoa"):
        return False
    return bool(e.get("creation_tx")) and not e.get("creator_eoa_asked")


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

def build_meta(ts, snapshot, planned, attempted, written, failed, empty,
               skipped, errors, calls, cap, duration_s, gave_up,
               credits=None, retroactive=False):
    """The sidecar that says how complete a snapshot is.

    H-0005 ranks tokens into quartiles WITHIN a snapshot, so a cross-section
    missing a quarter of the board does not mean what a full one means -- and
    the gaps are not random: they skew toward the deepest, best-held tokens.
    A snapshot is written whatever its coverage, and this file is what lets a
    later evaluation drop a thin one instead of ranking inside it.

    Coverage is measured against the PLANNED board, never against the tokens
    the loop happened to reach:

        planned   = written + failed + empty + skipped
        coverage  = written / (written + failed + skipped)

    `skipped` is the tail the loop never reached because the run bailed, and
    counting it is the whole point: measured against `attempted`, a run that
    gave up after four tokens and wrote all four would report 100% coverage.

    `empty` is out of the denominator deliberately. The four lists are
    disjoint and each means something different: `failed` is "the request did
    not come back", `empty` is "it came back with no holders", which is a
    fact about the token rather than about the collection. A board carrying
    dead tokens should not read as a degraded run.

    Where `failed` or `empty` is None -- a sidecar reconstructed after the
    fact, from a run that did not record them per token -- the split between
    them is unknown, so coverage is reported as the LOWER bound
    written / planned (the case where none of the missing were merely empty)
    and `coverage_bound` says "lower". A consumer applying a coverage floor
    is then conservative in the safe direction: it may discard a snapshot
    that was in fact complete, never keep one that was thin.

    Error counts are per ATTEMPT, so one token retried twice and still failing
    contributes 3 to http_500, not 1.
    """
    exact = failed is not None and empty is not None and skipped is not None
    if exact:
        denom = written + len(failed) + len(skipped)
        cov = round(written / denom, 4) if denom else None
        bound = "exact"
    else:
        cov = round(written / planned, 4) if planned else None
        bound = "lower"
    out = {
        "ts": ts,
        "snapshot": snapshot,
        "tokens_planned": planned,
        "tokens_attempted": attempted,
        "tokens_written": written,
        "tokens_failed": sorted(failed) if failed is not None else None,
        "tokens_empty": sorted(empty) if empty is not None else None,
        "tokens_skipped": sorted(skipped) if skipped is not None else None,
        "n_failed": len(failed) if failed is not None else None,
        "n_empty": len(empty) if empty is not None else None,
        "n_skipped": len(skipped) if skipped is not None else None,
        "coverage": cov,
        "coverage_bound": bound,
        "errors": dict(errors) if errors is not None else None,
        "calls": calls,
        "call_cap": cap,
        "duration_s": duration_s,
        "gave_up": gave_up,
        "credits_remaining": credits,
    }
    if retroactive:
        out["retroactive"] = True
    return out


def meta_path_for(snapshot_path):
    """`.../HHMMSS.jsonl` -> `.../HHMMSS.meta.json`.

    Deliberately not `.jsonl`: every reader of this directory globs
    `*/*.jsonl`, and a sidecar that matched would be parsed as a snapshot.
    """
    assert snapshot_path.endswith(".jsonl"), snapshot_path
    return snapshot_path[:-len(".jsonl")] + ".meta.json"


def do_dry_run():
    toks = load_tokens()
    cache = load_cache()
    meta = load_meta()
    now = datetime.now(timezone.utc)
    new = [t for t in toks if needs_deployer(t["ca"], cache, now)]
    want_meta = [t for t in toks if needs_meta(t["ca"], meta)]
    want_eoa = [t for t in toks if needs_creator_eoa(t["ca"], cache)]
    holder_calls = len(toks)
    allow = max(0, min(BACKFILL_PER_RUN,
                       MAX_CALLS_PER_RUN - holder_calls - len(new) - 10))
    backfill = min(allow, len(want_meta) + len(want_eoa))
    total = holder_calls + len(new) + backfill
    print("tokens on the board, excluding %s: %d (cap %d)"
          % ("/".join(sorted(SKIP_TIERS)), len(toks), MAX_TOKENS))
    print("deployers already cached: %d" % len(cache))
    print("token meta already cached: %d" % len(meta))
    print("creator wallets resolved: %d"
          % sum(1 for e in cache.values()
                if isinstance(e, dict) and e.get("creator_eoa")))
    print()
    print("planned calls this run")
    print("  holders    %4d  (1 per token, top %d)" % (holder_calls, TOP_HOLDERS))
    print("  deployers  %4d  (uncached or a null older than %d days)"
          % (len(new), DEPLOYER_RETRY_DAYS))
    print("  token meta %4d  one-time, wanted" % len(want_meta))
    print("  creator eoa%4d  one-time, wanted" % len(want_eoa))
    print("  backfill   %4d  this run (allowance %d of a %d cap; the rest "
          "waits for the next run)" % (backfill, allow, BACKFILL_PER_RUN))
    print("  total      %4d   (hard cap %d)" % (total, MAX_CALLS_PER_RUN))
    print("  with retries %3d  if every request needs all %d attempts%s"
          % (total * MAX_ATTEMPTS, MAX_ATTEMPTS,
             " -- over the cap, so a 5xx storm bails early and the sidecar"
             " records the short coverage"
             if total * MAX_ATTEMPTS > MAX_CALLS_PER_RUN else ""))
    print()
    steady = holder_calls * 8
    print("at every 3 hours, 8 runs a day")
    print("  steady state %5d calls/day = %6d credits/day at 20 each"
          % (steady, steady * 20))
    print("  worst case   %5d calls/day = %6d credits/day (every run at the cap)"
          % (MAX_CALLS_PER_RUN * 8, MAX_CALLS_PER_RUN * 8 * 20))
    print("  this run     %5d calls     = %6d credits" % (total, total * 20))
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
    meta = load_meta()
    cache_before, meta_before = len(cache), len(meta)
    eoa_before = sum(1 for e in cache.values()
                     if isinstance(e, dict) and e.get("creator_eoa"))
    # Backfill only in the headroom the holders and deployer calls leave, so a
    # cold cache slows the catch-up instead of aborting the run. 10 calls of
    # margin for retries.
    want_dep = sum(1 for t in toks if needs_deployer(t["ca"], cache, now))
    allow = max(0, min(BACKFILL_PER_RUN,
                       api.cap - len(toks) - want_dep - 10))
    backfill = 0            # one-time lookups spent this run
    print("backfill allowance this run: %d (cap %d, headroom %d)"
          % (allow, BACKFILL_PER_RUN, api.cap - len(toks) - want_dep))

    started = time.monotonic()
    rows, ok = [], 0
    attempted, failed, empty, skipped = 0, [], [], []
    for idx, t in enumerate(toks):
        if api.gave_up:
            # The tail the run never reached. Counted against coverage, so a
            # bail cannot look like a complete cross-section.
            skipped = [x["ca"] for x in toks[idx:]]
            break
        ca = t["ca"]
        attempted += 1
        body, _ = api.get("/tokens/%s/holders" % ca, {"limit": TOP_HOLDERS})
        if body is None:
            # The request did not come back. Kept apart from a token that
            # genuinely has no holders: one is an API problem and the other
            # is a fact about the token.
            failed.append(ca)
            continue
        holders = parse_holders(body)
        if not holders:
            empty.append(ca)
            continue
        if needs_deployer(ca, cache, now) and not api.gave_up:
            d, _ = api.get("/addresses/%s" % ca)
            info = parse_deployer(d)
            cache[ca] = {"deployer": info["deployer"],
                         "creation_tx": info["creation_tx"],
                         "asked_at": iso(now)}
        # One-time backfills, capped per run so a cold cache cannot eat the
        # call budget in a single pass. Both are cached forever on success.
        if (backfill < allow and needs_meta(ca, meta)
                and not api.gave_up):
            m, _ = api.get("/tokens/%s" % ca)
            backfill += 1
            info = parse_token_meta(m)
            info["asked_at"] = iso(now)
            meta[ca] = info
        if (backfill < allow and needs_creator_eoa(ca, cache)
                and not api.gave_up):
            tx = cache[ca]["creation_tx"]
            txb, _ = api.get("/transactions/%s" % tx)
            backfill += 1
            cache[ca]["creator_eoa"] = parse_tx_sender(txb)
            cache[ca]["creator_eoa_asked"] = iso(now)
        ent = cache.get(ca) or {}
        mt = meta.get(ca) or {}
        rows.append({
            "ts": iso(now),
            "ca": ca,
            "symbol": t.get("symbol"),
            "pool": t.get("main_pool"),
            "tier": t.get("tier"),
            "n_holders_returned": len(holders),
            # from the token endpoint for every token now, not just the five
            # the RPC already had. supplies is kept as a fallback for a token
            # whose /tokens lookup has not landed yet.
            "total_supply": mt.get("total_supply") or supplies.get(ca),
            "decimals": mt.get("decimals"),
            "holders": holders,
            "deployer": ent.get("deployer"),
            "creator_eoa": ent.get("creator_eoa"),
        })
        ok += 1

    # A partial cross-section still changes what "top quartile" means, which
    # is why this used to write nothing. It now writes the rows and the
    # coverage beside them instead: discarding the run threw away good rows
    # to avoid a problem that a number in a sidecar solves, and a cap bail
    # under the new retry policy would have thrown away whole runs. Nothing
    # reads this log yet, so the evaluator join can require a coverage floor
    # from the start -- see build_meta.
    if not rows:
        print("no rows collected (gave_up=%s); writing nothing" % api.gave_up)
        print("calls %d, errors %s" % (api.calls, api.errors or "none"))
        return 0

    day = now.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(OUT_DIR, day), exist_ok=True)
    path = os.path.join(OUT_DIR, day, now.strftime("%H%M%S") + ".jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")

    json.dump(cache, open(DEPLOYER_CACHE, "w"), indent=1, sort_keys=True)
    json.dump(meta, open(TOKEN_META_CACHE, "w"), indent=1, sort_keys=True)

    # `cover`, not `meta`: `meta` is the token_supply cache above and the
    # print block below still reports its size.
    cover = build_meta(
        ts=iso(now), snapshot=os.path.basename(path), planned=len(toks),
        attempted=attempted, written=len(rows), failed=failed, empty=empty,
        skipped=skipped, errors=api.errors, calls=api.calls, cap=api.cap,
        duration_s=round(time.monotonic() - started, 1),
        gave_up=api.gave_up, credits=api.credits)
    json.dump(cover, open(meta_path_for(path), "w"), indent=1)

    hint = build_hint(rows)
    json.dump({"generated_at": iso(now),
               "rule": "contract, and a top-%d holder in >= %d tokens"
                       % (HINT_TOP_N, HINT_MIN_TOKENS),
               "applied": False,
               "addresses": hint},
              open(HINT_PATH, "w"), indent=1)

    sup = sum(1 for r in rows if r["total_supply"] is not None)
    eoa_now = sum(1 for e in cache.values()
                  if isinstance(e, dict) and e.get("creator_eoa"))
    print("holders: %d/%d tokens -> %s" % (ok, len(toks), path))
    print("coverage: %s of %d planned (%d failed, %d empty, %d skipped) -> %s"
          % (cover["coverage"], len(toks), len(failed), len(empty),
             len(skipped), os.path.basename(meta_path_for(path))))
    print("deployers: %d cached (+%d this run)" % (len(cache),
                                                   len(cache) - cache_before))
    print("creator wallets: %d resolved (+%d this run)"
          % (eoa_now, eoa_now - eoa_before))
    print("token meta: %d cached (+%d this run); %d/%d rows carry a supply"
          % (len(meta), len(meta) - meta_before, sup, len(rows)))
    print("backfill calls this run: %d/%d allowed" % (backfill, allow))
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
