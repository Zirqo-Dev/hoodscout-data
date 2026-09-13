#!/usr/bin/env python3
"""Verify Robinhood stock token contracts before they enter the alerting pipeline.

Blockscout's verified badge and "Stock" tag sit behind a Cloudflare managed
challenge that no scripted client can pass, so authenticity is established from
the chain instead: the on-chain name must carry the Robinhood Token suffix, and
the deployed bytecode must match the known-good reference contracts. A ticker
squatter can copy a name and a logo; matching the deployed runtime bytecode of
the real token is a considerably higher bar.

Usage: python scripts/verify_stocks.py NVDA SPCX MSTR GME
"""
import hashlib, json, sys, time, urllib.error, urllib.parse, urllib.request

BASE = "https://api.geckoterminal.com/api/v2"
RPC = "https://rpc.mainnet.chain.robinhood.com"
NETWORK = "robinhood"
HEADERS = {"Accept": "application/json;version=20230302",
           "User-Agent": "hoodscout-data/0.3"}

# already in STOCKS and confirmed against Blockscout when it was reachable
REFERENCE = {
    "HIMS": "0xccee82fe024c36fa15e1005ede3e9e4787e23d09",
    "AMC": "0x05a3d1cd21d0c88145e82600e62e7e496e0f222b",
}

# Quote assets: what tokens are priced *against*, not trading partners worth
# surfacing. stocks.REFERENCE holds USDG and the zero address (native), so a
# pool quoted in the wrapped-ETH ERC-20 was being reported as an untracked
# counterparty of interest on every sweep. Kept here rather than added to
# stocks.REFERENCE because that set also drives the locked/reference split in
# stocks.measure(), and moving this depth would shift locked_pct_est and the
# alert that reads it.
QUOTE_ASSETS = {
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH, name()/symbol() 'WETH', 2202 bytes
}

NAME_SUFFIX = "robinhood token"
SEL_NAME, SEL_SYMBOL = "0x06fdde03", "0x95d89b41"
SEL_DECIMALS, SEL_SUPPLY = "0x313ce567", "0x18160ddd"


def err(e):
    """One-line exception summary. An HTTPError carries the status and the
    first of the body, which is what distinguishes a rate limit, a WAF
    interstitial and a real absence from each other."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read(200)
        except Exception:
            body = b""
        return f"HTTP {e.code} {e.reason} {body!r}"
    return f"{type(e).__name__}: {e}"


def gt(path, tries=4):
    """GeckoTerminal rate-limits the free tier; back off rather than reporting
    a 429 as an absence, which would read as 'no such token'."""
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            wait = 20 * (i + 1)
            print(f"    429, retrying in {wait}s")
            time.sleep(wait)


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if d.get("error"):
        raise RuntimeError(f"rpc {d['error']}")
    return d.get("result")


def call(to, selector):
    return rpc("eth_call", [{"to": to, "data": selector}, "latest"])


def decode_string(hexstr):
    if not hexstr or hexstr == "0x":
        return None
    raw = bytes.fromhex(hexstr[2:])
    if len(raw) >= 64:
        off = int.from_bytes(raw[0:32], "big")
        if off + 32 <= len(raw):
            ln = int.from_bytes(raw[off:off + 32], "big")
            if off + 32 + ln <= len(raw):
                return raw[off + 32:off + 32 + ln].decode("utf-8", "replace")
    return raw.rstrip(b"\x00").decode("utf-8", "replace") or None


def decode_uint(hexstr):
    if not hexstr or hexstr == "0x":
        return None
    return int(hexstr, 16)


def code_of(addr):
    c = rpc("eth_getCode", [addr, "latest"])
    return "" if not c or c == "0x" else c[2:]


def similarity(a, b):
    """Fraction of bytes equal at the same offset; immutables differ, structure does not."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    same = sum(1 for i in range(0, n, 2) if a[i:i + 2] == b[i:i + 2])
    return round(100.0 * same / (max(len(a), len(b)) / 2), 2)


def candidates(ticker):
    """Base tokens of any pool GeckoTerminal returns for this ticker."""
    q = urllib.parse.quote(ticker)
    d = gt(f"/search/pools?query={q}&network={NETWORK}&page=1")
    found = {}
    for item in d.get("data", []):
        a = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        tid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        ca = tid.split("_", 1)[1].lower() if "_" in tid else None
        if not ca:
            continue
        e = found.setdefault(ca, {"pools": 0, "reserve": 0.0, "created": [],
                                  "names": set()})
        e["pools"] += 1
        e["reserve"] += float(a.get("reserve_in_usd") or 0)
        e["names"].add(a.get("name") or "")
        if a.get("pool_created_at"):
            e["created"].append(a["pool_created_at"])
    return found


def pool_depth(ca):
    try:
        d = gt(f"/networks/{NETWORK}/tokens/{ca}/pools")
    except Exception as e:
        return {"error": str(e)}
    pools = []
    for item in d.get("data", []):
        a = item.get("attributes") or {}
        pools.append({"name": a.get("name"),
                      "reserve": round(float(a.get("reserve_in_usd") or 0), 2),
                      "created": a.get("pool_created_at")})
    pools.sort(key=lambda p: -p["reserve"])
    return {"count": len(pools), "top": pools[:4],
            "created": sorted(p["created"] for p in pools if p["created"])}


def counterparties(ca, limit=12):
    """Pools this token trades in, with the address of the other side.

    Symbol search is not enough to find a counterparty: /search/pools?query=BONER
    returns only small USDG pools and misses the AI/BONER pool holding $3.3M.
    Walking a known token's own pools reaches the deep ones directly.
    """
    d = gt(f"/networks/{NETWORK}/tokens/{ca}/pools")
    rows = []
    for item in d.get("data", []):
        a, rel = item.get("attributes") or {}, item.get("relationships") or {}

        def addr(side):
            tid = ((rel.get(side) or {}).get("data") or {}).get("id", "")
            return tid.split("_", 1)[1].lower() if "_" in tid else None

        base, quote = addr("base_token"), addr("quote_token")
        rows.append({"pool": a.get("name"),
                     "reserve": round(float(a.get("reserve_in_usd") or 0), 2),
                     "created": a.get("pool_created_at"),
                     "counterparty": quote if base == ca.lower() else base})
    rows.sort(key=lambda r: -r["reserve"])
    return rows[:limit]


def describe(ca, ref_codes):
    out = {"ca": ca}
    try:
        out["name"] = decode_string(call(ca, SEL_NAME))
        out["symbol"] = decode_string(call(ca, SEL_SYMBOL))
        dec = decode_uint(call(ca, SEL_DECIMALS))
        sup = decode_uint(call(ca, SEL_SUPPLY))
        out["decimals"], out["total_supply_raw"] = dec, sup
        out["total_supply"] = sup / 10 ** dec if (sup is not None and dec) else None
    except Exception as e:
        out["rpc_error"] = str(e)
        return out

    code = code_of(ca)
    out["code_len"] = len(code) // 2
    out["code_sha256"] = hashlib.sha256(code.encode()).hexdigest()[:16] if code else None
    out["bytecode"] = {sym: {"exact": code == ref, "similarity_pct": similarity(code, ref),
                             "len_delta": out["code_len"] - len(ref) // 2}
                       for sym, ref in ref_codes.items()}
    name = (out.get("name") or "").lower()
    out["name_suffix_ok"] = name.endswith(NAME_SUFFIX)
    return out


SWEEP_MIN_RESERVE = 500_000


def tracked_cas(path="data/latest.json"):
    """Tokens collect.py already sees, so the sweep only reports what is missing."""
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"WARN {path}: {e}")
        return set()
    return {(t.get("ca") or "").lower() for t in d.get("tokens") or []}


def sweep(cas, floor=SWEEP_MIN_RESERVE):
    """Counterparties holding real depth against the given tokens that are not
    already tracked. Quote assets and the source tokens themselves are skipped:
    the question is which trading partners the pipeline cannot currently see."""
    sys.path.insert(0, "scripts")
    import stocks

    known = tracked_cas()
    sources = {c.lower() for c in cas}
    skip = sources | known | {a.lower() for a in stocks.REFERENCE} \
        | {c.lower() for c in stocks.STOCKS.values()} | QUOTE_ASSETS
    print(f"already tracked in latest.json: {len(known)} tokens")
    print(f"reserve floor: ${floor:,}")

    found = {}
    for ca in cas:
        try:
            rows = counterparties(ca, limit=20)
        except Exception as e:
            print(f"WARN {ca}: {err(e)}")
            continue
        for r in rows:
            cp = (r["counterparty"] or "").lower()
            if not cp or cp in skip or r["reserve"] < floor:
                continue
            f = found.setdefault(cp, {"best": 0.0, "via": []})
            f["best"] = max(f["best"], r["reserve"])
            f["via"].append(f"{r['pool']} ${r['reserve']:,.0f}")
        time.sleep(2.5)

    print()
    print(f"=== {len(found)} untracked counterparties over the floor ===")
    for cp, f in sorted(found.items(), key=lambda kv: -kv[1]["best"]):
        print()
        print(f"--- {cp}")
        for v in f["via"]:
            print(f"    via          : {v}")
        try:
            print(f"    name()       : {decode_string(call(cp, SEL_NAME))!r}")
            print(f"    symbol()     : {decode_string(call(cp, SEL_SYMBOL))!r}")
            dec = decode_uint(call(cp, SEL_DECIMALS))
            sup = decode_uint(call(cp, SEL_SUPPLY))
            human = sup / 10 ** dec if (sup is not None and dec) else sup
            shape = "ROUND (single-mint shape)" if human and float(human).is_integer() \
                and human >= 1_000_000 else "organic"
            print(f"    totalSupply  : {human} -> {shape}")
        except Exception as e:
            print(f"    RPC FAILED   : {err(e)}")
        try:
            d = pool_depth(cp)
            created = d.get("created") or []
            span = f"{created[0]} .. {created[-1]}" if created else "-"
            print(f"    pools        : {d.get('count')}   created span: {span}")
            for p in (d.get("top") or [])[:3]:
                print(f"        {p['reserve']:>14,.2f}  {p['name']}  {p['created']}")
        except Exception as e:
            print(f"    pools        : {err(e)}")
        time.sleep(2.5)


def inspect(cas):
    """Raw identity and pool evidence for arbitrary contracts, for checking a
    flagged address before it is written into avoid.json."""
    for ca in cas:
        print()
        print(f"--- {ca}")
        try:
            print(f"    name()       : {decode_string(call(ca, SEL_NAME))!r}")
            print(f"    symbol()     : {decode_string(call(ca, SEL_SYMBOL))!r}")
            dec = decode_uint(call(ca, SEL_DECIMALS))
            sup = decode_uint(call(ca, SEL_SUPPLY))
            print(f"    decimals     : {dec}")
            print(f"    totalSupply  : {sup / 10 ** dec if sup is not None and dec else sup}")
            code = code_of(ca)
            print(f"    code         : {len(code)//2} bytes "
                  f"sha256 {hashlib.sha256(code.encode()).hexdigest()[:16]}")
        except Exception as e:
            print(f"    RPC FAILED   : {err(e)}")
        try:
            a = (gt(f"/networks/{NETWORK}/tokens/{ca}").get("data") or {}).get("attributes") or {}
            print(f"    GT name/sym  : {a.get('name')!r} / {a.get('symbol')!r}")
            print(f"    GT price/fdv : {a.get('price_usd')} / {a.get('fdv_usd')}")
        except Exception as e:
            print(f"    GT token     : {err(e)}")
        time.sleep(2.5)
        try:
            print(f"    pools        : {json.dumps(pool_depth(ca))[:700]}")
        except Exception as e:
            print(f"    pools        : {err(e)}")
        time.sleep(2.5)


def measure_preview(pairs):
    """Run the live stocks.py measurement over SYM:CA pairs without touching
    STOCKS, so the numbers can be eyeballed before a contract joins alerting."""
    sys.path.insert(0, "scripts")
    import stocks
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    hist = stocks.history()
    print(f"anchor {stocks.ANCHOR_UTC}   history rows {len(hist)}")
    print(f"{'sym':<7}{'locked_pct_est':>15}{'free_float_units':>18}"
          f"{'premium_pct':>13}{'supply':>14}{'price_usd':>12}")
    for pair in pairs:
        sym, _, ca = pair.partition(":")
        try:
            m = stocks.measure(sym, ca.lower())
        except Exception as e:
            print(f"{sym:<7} measure failed: {e}")
            continue
        if not m:
            print(f"{sym:<7} no price/fdv from GeckoTerminal")
            continue
        m["ts"] = now.isoformat()
        fired = stocks.alerts([m], hist, now)
        print(f"{sym:<7}{str(m.get('locked_pct_est')):>15}"
              f"{str(m.get('free_float_units')):>18}"
              f"{str(m.get('premium_pct')):>13}"
              f"{m.get('supply'):>14,.0f}{m.get('price_usd'):>12}")
        for f in fired:
            print(f"        alert: {f}")
        time.sleep(2.5)


def main():
    if "--counterparties" in sys.argv:
        for ca in sys.argv[sys.argv.index("--counterparties") + 1:]:
            print(f"--- pools of {ca}")
            try:
                for r in counterparties(ca):
                    print(f"    {r['reserve']:>15,.2f}  {(r['pool'] or '')[:30]:<30} "
                          f"{r['created']}  {r['counterparty']}")
            except Exception as e:
                print(f"    FAILED: {err(e)}")
            time.sleep(2.5)
        return

    if "--sweep" in sys.argv:
        sweep(sys.argv[sys.argv.index("--sweep") + 1:])
        return

    if "--inspect" in sys.argv:
        inspect(sys.argv[sys.argv.index("--inspect") + 1:])
        return

    if "--measure" in sys.argv:
        measure_preview(sys.argv[sys.argv.index("--measure") + 1:])
        return

    tickers = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not tickers:
        print("usage: verify_stocks.py TICKER [TICKER ...]")
        return

    print("=== reference contracts ===")
    ref_codes = {}
    for sym, ca in REFERENCE.items():
        code = code_of(ca)
        ref_codes[sym] = code
        print(f"  {sym:<6} {ca}  code {len(code)//2} bytes  "
              f"sha256 {hashlib.sha256(code.encode()).hexdigest()[:16]}  "
              f"name={decode_string(call(ca, SEL_NAME))!r}")
    syms = list(ref_codes)
    print(f"  CONTROL {syms[0]} vs {syms[1]}: exact={ref_codes[syms[0]] == ref_codes[syms[1]]} "
          f"similarity={similarity(ref_codes[syms[0]], ref_codes[syms[1]])}%")
    print("  (if the two known-good contracts do not match each other, the")
    print("   bytecode test cannot distinguish anything and must be discarded)")

    for ticker in tickers:
        print()
        print("=" * 70)
        print(f"TICKER {ticker}")
        print("=" * 70)
        try:
            found = candidates(ticker)
        except Exception as e:
            # not the same thing as an absence, and must not be read as one
            print(f"  SEARCH FAILED, ticker NOT TESTED: {e}")
            continue
        if not found:
            print("  no GeckoTerminal pools matched -> SKIP (not found)")
            time.sleep(2.5)
            continue
        print(f"  {len(found)} candidate contract(s) from pool search")
        for ca, meta in sorted(found.items(), key=lambda kv: -kv[1]["reserve"]):
            print()
            print(f"  --- {ca}")
            print(f"      pool names       : {sorted(meta['names'])[:4]}")
            print(f"      search reserve   : ${meta['reserve']:,.2f} across {meta['pools']} pool(s)")
            d = describe(ca, ref_codes)
            if d.get("rpc_error"):
                print(f"      RPC FAILED       : {d['rpc_error']}")
                continue
            print(f"      name()           : {d['name']!r}")
            print(f"      symbol()         : {d['symbol']!r}")
            print(f"      name suffix ok   : {d['name_suffix_ok']}")
            print(f"      decimals/supply  : {d['decimals']} / {d['total_supply']}")
            print(f"      code             : {d['code_len']} bytes sha256 {d['code_sha256']}")
            passes = d["name_suffix_ok"] and any(b["exact"] for b in d["bytecode"].values())
            for sym, b in d["bytecode"].items():
                print(f"      vs {sym:<5}        : exact={b['exact']} "
                      f"similarity={b['similarity_pct']}% len_delta={b['len_delta']}")
            print(f"      VERDICT          : {'PASS' if passes else 'REJECT'}")
            # only the candidate that passed is worth spending a request on
            if passes:
                print(f"      pool depth       : {json.dumps(pool_depth(ca))[:500]}")
            time.sleep(2.5)
        time.sleep(2.5)


if __name__ == "__main__":
    main()
