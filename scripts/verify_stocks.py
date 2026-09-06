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

NAME_SUFFIX = "robinhood token"
SEL_NAME, SEL_SYMBOL = "0x06fdde03", "0x95d89b41"
SEL_DECIMALS, SEL_SUPPLY = "0x313ce567", "0x18160ddd"


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


def main():
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
