#!/usr/bin/env python3
"""Layer 3b: forward outcomes for evidence-log rows, computed on demand.

Reads data/log/ and nothing else. It never writes to the log, never imports
collect.py, and never touches anything the pipeline depends on. See
docs/EVIDENCE_LAYER.md.

Labels are derived data and are not committed. Everything here is recomputed
from the log each time: the log is the record, the labels are a view of it, and
a view that rebuilds in seconds should not be a 684 MB/month file in git.
--out writes them to a scratch directory for inspection, gitignored.

Baselines are likewise computed at summary time rather than stored. The draw is
seeded sha256(ts|ca|pile), so it is identical on every machine and every run; a
stored copy would be bytes spent on something a seed already determines.

NOTHING HERE HAS BEEN TRADED. The net returns model a round trip, optimistically:
no sandwich risk on a public mempool, no V3 range concentration, and the quoted
liquidity is taken to be real and still there on exit.
"""
import argparse, bisect, glob, gzip, hashlib, json, os, random, statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

LOG_DIR = "data/log"
DEFAULT_OUT = ".labels"          # gitignored; only written when --out is given

# hours forward, how far either side still counts, and the thinning stride.
# The stride is why the summary can be honest about n: a 6h horizon sampled
# every 15 minutes gives 24 overlapping observations for every independent one,
# and counting those as 24 data points is the most common way to turn noise
# into a finding. One entry snapshot per stride, and the eff_n column shows
# what is left after collapsing (ca, horizon-bucket) cells.
HORIZONS = {"1h": (1.0, 0.25, 0.0), "6h": (6.0, 0.75, 1.0), "24h": (24.0, 2.0, 4.0)}

NOTIONALS = (500, 5000)   # the two round trips modelled, in USD
THIN_LIQ = 5000.0         # below this, the larger notional is not exitable
DRAWS = 20

# Used only when the row carries no fee. 1% is near the top of the observed
# range (0.01% to 1.9%), which is the right way to be wrong, but a 0.01% pool
# charged 1% is wrong a hundredfold. Every outcome records fee_source so a
# result can be re-run over known-fee rows alone and swept across the range.
#
# Only two sources exist, deliberately. A dex_constant would be defensible for
# uniswap-v2-robinhood, whose 0.30% is hard-coded as mul(997)/mul(1000) in
# UniswapV2Library, but not for pons-v2 (fees are set per launch), pons-v2-dex
# or bankr-robinhood (no published fee found), and an invented constant would
# look like knowledge in this field and propagate silently. An `onchain` source
# is not implementable: 60 of the 97 fee-less rows in the 2026-10-18T14:00
# snapshot store a 32-byte pool id rather than an address, and a v4 pool id is
# keccak256 of the PoolKey -- the fee is one of the hashed fields, so it cannot
# be recovered from the id.
FEE_FALLBACK = 0.01
FEE_SWEEP = (0.0, 0.003, 0.01, 0.019)   # the observed range, for the bound

PILES = ("tier", "scored", "dashboard")
MIN_SCORE_TXNS = 200      # mirrors collect.py; not imported, to stay uncoupled
DASHBOARD_TIERS = ("BEST", "EMERGING", "WATCH")


def labeler_version():
    """git hash-object of this file, computed without shelling out to git.

    Recorded in the summary and in every hypothesis: a result is reproducible
    only if you know which arithmetic produced it.
    """
    data = open(os.path.abspath(__file__), "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _open(path):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def load_log():
    """Every log row, deduplicated on (ts, ca), plus the indexes used below."""
    paths = sorted(glob.glob(f"{LOG_DIR}/*/*.jsonl") + glob.glob(f"{LOG_DIR}/*.jsonl.gz"))
    seen, rows = set(), []
    for p in paths:
        for line in _open(p):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                r["dt"] = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            key = (r["ts"], r["ca"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
    by_ca, times = defaultdict(list), {}
    for r in rows:
        by_ca[r["ca"]].append(r)
    for ca, v in by_ca.items():
        # a parallel list of timestamps per ca, so finding the print nearest a
        # target is a bisect rather than a scan: at 96 runs a day a token's
        # list reaches ~35,000 entries within a year
        v.sort(key=lambda r: r["dt"])
        times[ca] = [r["dt"] for r in v]
    at_ts = defaultdict(list)
    for r in rows:
        at_ts[r["ts"]].append(r)
    pile = {}
    for ts, rs in at_ts.items():
        tiers = defaultdict(list)
        for r in rs:
            tiers[r.get("tier")].append(r["ca"])
        for v in tiers.values():
            v.sort()
        pile[(ts, "tier")] = dict(tiers)
        pile[(ts, "scored")] = sorted(r["ca"] for r in rs
                                      if (r.get("txns") or 0) >= MIN_SCORE_TXNS)
        pile[(ts, "dashboard")] = sorted(r["ca"] for r in rs
                                         if r.get("tier") in DASHBOARD_TIERS)
    return rows, by_ca, times, pile, len(paths)


def nets(gross, liq_in, liq_out, fee):
    """gross - 2*fee - impact in - impact out, per notional, floored at -100%
    because a round trip cannot lose more than it put in."""
    out = {}
    for size in NOTIONALS:
        imp = sum(2.0 * size / liq for liq in (liq_in, liq_out) if liq and liq > 0)
        out[f"ret_net_{size}"] = round(max(-1.0, gross - 2 * fee - imp), 6)
    return out


def renet(o, fee, size=500):
    """An outcome's net return recomputed at a different fee.

    The same arithmetic nets() does with one input swapped. It reads the stored
    ret_gross, which is rounded to 6dp, where nets() used the unrounded value,
    so it agrees with the stored net to within 9.8e-7 -- 0.0001pp, measured
    across every scored outcome in the live log. Deliberately not rounded
    again: a second rounding doubles that error rather than hiding it.

    A row already floored at -100% cannot move, so a sweep leaves it alone.
    """
    if o.get("ret_gross") is None:
        return None
    imp = sum(2.0 * size / l for l in (o.get("liq_in"), o.get("liq_out"))
              if l and l > 0)
    return max(-1.0, o["ret_gross"] - 2 * fee - imp)


def net_at(o, fee, size=500):
    """net500 for an outcome under an assumed fee for unknown-fee rows.

    A row whose fee came from the pool name is already right and does not move.
    """
    if o.get("fee_source") == "name":
        return o.get(f"ret_net_{size}")
    return renet(o, fee, size)


def outcome(entry, horizon, by_ca, times, latest):
    """One (row, horizon) outcome, or None while it is still pending.

    Pending is never materialised: a row too young to label is simply absent.
    """
    hours, tol, _ = HORIZONS[horizon]
    target = entry["dt"] + timedelta(hours=hours)
    lo, hi = target - timedelta(hours=tol), target + timedelta(hours=tol)
    if latest < lo:
        return None                      # pending: the window has not opened

    rows, ts_list = by_ca.get(entry["ca"], []), times.get(entry["ca"], [])
    i, j = bisect.bisect_left(ts_list, lo), bisect.bisect_right(ts_list, hi)
    window = rows[i:j]
    hit = min(window, key=lambda r: abs(r["dt"] - target)) if window else None

    p_in, liq_in = entry.get("price"), entry.get("liq") or 0.0
    fee = entry.get("fee")
    # A row with no liquidity at entry is not a position anyone could have
    # opened, so it cannot have a return -- scoring it -100% would invent total
    # losses out of already-dead tokens and bias everything downstream. The
    # exit status is still recorded; only the number is withheld.
    enterable = liq_in > 0
    base = {"ts": entry["ts"], "ca": entry["ca"], "h": horizon,
            "tier": entry.get("tier"), "rms": entry.get("rms"),
            "enterable": enterable, "fee": fee,
            "fee_used": fee if fee is not None else FEE_FALLBACK,
            "fee_source": "name" if fee is not None else "fallback"}
    none3 = {"ret_gross": None, "ret_net_500": None, "ret_net_5000": None}

    # A matched print with no liquidity behind it is not an exit, it is a zero.
    if hit and (hit.get("liq") or 0) > 0 and hit.get("price") and p_in:
        thin = min(liq_in, hit["liq"]) < THIN_LIQ
        if enterable:
            gross = hit["price"] / p_in - 1.0
            rets = {"ret_gross": round(gross, 6),
                    **nets(gross, liq_in, hit["liq"], base["fee_used"])}
        else:
            rets = none3
        return {**base, "status": "thin" if thin else "ok", **rets,
                "t_actual": hit["ts"],
                "dt_h": round((hit["dt"] - entry["dt"]).total_seconds() / 3600, 3),
                "liq_in": liq_in, "liq_out": hit["liq"]}

    # No usable print. Was it last seen with the liquidity gone, or did it just
    # stop appearing? The first is a loss; the second is not knowledge.
    k = bisect.bisect_right(ts_list, entry["dt"])
    prior = rows[k:j]
    if hit or (prior and (prior[-1].get("liq") or 0) <= 0):
        loss = {"ret_gross": -1.0, "ret_net_500": -1.0, "ret_net_5000": -1.0}
        return {**base, "status": "gone_no_liq", **(loss if enterable else none3),
                "t_actual": (hit or prior[-1])["ts"], "dt_h": None,
                "liq_in": liq_in, "liq_out": 0.0}
    return {**base, "status": "gone_unseen", **none3,
            "t_actual": None, "dt_h": None, "liq_in": liq_in, "liq_out": None}


def thin_snapshots(all_ts, stride_h):
    """One entry snapshot per stride, earliest in each bucket.

    Snapshots, not rows: every token present in a kept snapshot is kept, so
    thinning never changes which tokens are represented, only how often.
    """
    if not stride_h:
        return set(all_ts)
    keep, seen = set(), set()
    for ts in sorted(all_ts):
        b = int(datetime.fromisoformat(ts).timestamp() // (stride_h * 3600))
        if b not in seen:
            seen.add(b)
            keep.add(ts)
    return keep


def eff_n(rows, hours):
    """Distinct (ca, horizon-bucket) cells: what is left after the overlap."""
    return len({(r["ca"], int(datetime.fromisoformat(r["ts"]).timestamp()
                              // (hours * 3600))) for r in rows})


def peers(entry, pile, which, k=DRAWS):
    """k other tokens from one pile at the same instant, seeded by the row."""
    p = pile.get((entry["ts"], which))
    if which == "tier":
        p = (p or {}).get(entry.get("tier"), [])
    members = [c for c in (p or []) if c != entry["ca"]]
    if not members:
        return []
    seed = hashlib.sha256(f"{entry['ts']}|{entry['ca']}|{which}".encode()).digest()
    return random.Random(int.from_bytes(seed[:8], "big")).sample(
        members, min(k, len(members)))


def baseline_rows(entry, horizon, which, pile, by_ca, times, latest, cache):
    """The same-pile random pick's outcomes at the same instant.

    Returned rather than reduced to a median, so a caller can re-net them at a
    swept fee. The draw is seeded, so this is reproducible and never stored.
    """
    out = []
    for ca in peers(entry, pile, which):
        key = (entry["ts"], ca, horizon)
        if key not in cache:
            rs, tl = by_ca.get(ca, []), times.get(ca, [])
            m = bisect.bisect_left(tl, entry["dt"])
            row = rs[m] if m < len(rs) and rs[m]["ts"] == entry["ts"] else None
            cache[key] = outcome(row, horizon, by_ca, times, latest) if row else None
        if cache[key]:
            out.append(cache[key])
    return out


def baseline_med(entry, horizon, which, pile, by_ca, times, latest, cache,
                 fee=None):
    """Median net-500 of a same-pile random pick, or None."""
    rows = baseline_rows(entry, horizon, which, pile, by_ca, times, latest, cache)
    vals = [v for v in (net_at(o, fee) if fee is not None else o["ret_net_500"]
                        for o in rows) if v is not None]
    return statistics.median(vals) if vals else None


def label_all(rows, by_ca, times, latest, keep=None):
    """Every outcome for every row, in memory. keep restricts entry snapshots."""
    out, pending = [], 0
    for entry in rows:
        for horizon in HORIZONS:
            if keep is not None and entry["ts"] not in keep[horizon]:
                continue
            o = outcome(entry, horizon, by_ca, times, latest)
            if o is None:
                pending += 1
            else:
                out.append(o)
    return out, pending


def run_label(out_dir=None):
    rows, by_ca, times, _, nfiles = load_log()
    if not rows:
        print(f"no log rows under {LOG_DIR}/ -- nothing to label")
        return
    latest = max(r["dt"] for r in rows)
    print(f"labeler {labeler_version()[:12]}")
    print(f"log: {len(rows)} rows from {nfiles} files, "
          f"{min(r['dt'] for r in rows).isoformat()[:19]} .. {latest.isoformat()[:19]}")
    labels, pending = label_all(rows, by_ca, times, latest)
    counts = Counter(o["status"] for o in labels)
    counts["pending (not materialised)"] = pending
    print(f"{len(labels)} outcomes computed in memory")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:30s} {v:8d}")
    if labels:
        src = Counter(o["fee_source"] for o in labels)
        for k in ("name", "fallback"):
            print(f"  {'fee_source=' + k:30s} {src[k]:8d}"
                  f"  ({100*src[k]/len(labels):.1f}%)")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        by_day = defaultdict(list)
        for o in labels:
            by_day[o["ts"][:10]].append(o)
        for day, items in sorted(by_day.items()):
            with open(f"{out_dir}/{day}.jsonl", "w") as f:
                for o in sorted(items, key=lambda d: (d["ts"], d["ca"], d["h"])):
                    f.write(json.dumps(o, separators=(",", ":")) + "\n")
        print(f"\nwrote {len(labels)} rows to {out_dir}/ "
              f"(derived, gitignored, safe to delete)")
    print("\nLabels are not committed: they are recomputed from data/log/ on "
          "demand.\nThe log is the record and this is a view of it.")


def fmt(v):
    return "      -" if v is None else f"{100*v:+7.2f}"


def run_summary(pile="scored"):
    rows, by_ca, times, pile_idx, nfiles = load_log()
    if not rows:
        print(f"no log rows under {LOG_DIR}/ -- nothing to summarise")
        return
    latest = max(r["dt"] for r in rows)
    all_ts = {r["ts"] for r in rows}
    keep = {h: thin_snapshots(all_ts, HORIZONS[h][2]) for h in HORIZONS}

    print(f"labeler {labeler_version()[:12]}   baseline pile: {pile}")
    print(f"log: {len(rows)} rows, {len(all_ts)} snapshots, from {nfiles} files")
    print(f"thinning, entry snapshots kept of {len(all_ts)}: "
          + ", ".join(f"{h} {len(keep[h])}" for h in HORIZONS))
    print("\nNOTHING HERE HAS BEEN TRADED. ret_net_* models a round trip as")
    print("  gross - 2*fee - 2*size/liq_in - 2*size/liq_out,")
    print("a constant-product approximation against half the reported pool TVL.")
    print("It is optimistic: no sandwich risk, no V3 range concentration, and it")
    print("assumes the quoted liquidity is real and still there on exit.\n")

    labels, _ = label_all(rows, by_ca, times, latest, keep=keep)
    if not labels:
        print("nothing is old enough to label yet")
        return
    cache, by = {}, defaultdict(list)
    entry_of = {(r["ts"], r["ca"]): r for r in rows}
    for o in labels:
        o["_base"] = baseline_med(entry_of[(o["ts"], o["ca"])], o["h"], pile,
                                  pile_idx, by_ca, times, latest, cache)
        by[(o["tier"], o["h"])].append(o)

    src = Counter(o["fee_source"] for o in labels)
    print(f"fee_source: name {src['name']}, fallback {src['fallback']} "
          f"({100*src['fallback']/len(labels):.1f}% at {FEE_FALLBACK:.0%}). "
          "A fee error moves net500 by exactly 2x the error, so it shifts a\n"
          "level and largely cancels in a difference between groups with "
          "similar unknown-fee shares.\n")

    hdr = (f"{'tier':12s} {'h':>4s} {'n':>6s} {'eff_n':>6s} {'scored':>6s} "
           f"{'cens%':>6s} {'noliq%':>6s} {'gross~':>8s} {'net500~':>8s} "
           f"{'net5k~':>8s} {'net500 med':>10s} {'base med':>9s}  status histogram")
    print(hdr); print("-" * len(hdr))
    order = ["BEST", "EMERGING", "WATCH", "SPECULATIVE", "DEAD", "AVOID"]
    for tier in order + sorted({k[0] for k in by} - set(order) - {None}):
        for h in HORIZONS:
            rs = by.get((tier, h))
            if not rs:
                continue
            hist = Counter(o["status"] for o in rs)
            unent = sum(1 for o in rs if not o["enterable"])
            scored = [o for o in rs if o["ret_net_500"] is not None]
            g = [o["ret_gross"] for o in scored]
            n5 = [o["ret_net_500"] for o in scored]
            n5k = [o["ret_net_5000"] for o in scored]
            bm = [o["_base"] for o in rs if o["_base"] is not None]
            print(f"{str(tier):12s} {h:>4s} {len(rs):6d} "
                  f"{eff_n(scored, HORIZONS[h][0]):6d} {len(scored):6d} "
                  f"{100*hist.get('gone_unseen',0)/len(rs):5.1f}% "
                  f"{100*unent/len(rs):5.1f}% "
                  f"{fmt(statistics.mean(g) if g else None)} "
                  f"{fmt(statistics.mean(n5) if n5 else None)} "
                  f"{fmt(statistics.mean(n5k) if n5k else None)} "
                  f"{fmt(statistics.median(n5) if n5 else None):>10s} "
                  f"{fmt(statistics.median(bm) if bm else None):>9s}  "
                  + " ".join(f"{k}={v}" for k, v in sorted(hist.items())))
    print("\nn is rows after thinning; eff_n collapses (ca, horizon-bucket)")
    print("cells and is the number any claim should be read against. '~' is a")
    print("mean, 'med' a median. cens% is gone_unseen, censored and never a")
    print("number. noliq% is rows with no liquidity at entry: not a position")
    print(f"anyone could open. base is {DRAWS} draws from pile='{pile}' at the")
    print("same instant, seeded sha256(ts|ca|pile), computed here, never stored.")
    if pile == "tier":
        print("\nNOTE: pile='tier' draws from the SAME tier, so this compares each")
        print("token against its tier-mates. It cannot test whether a tier is any")
        print("good -- for that, use --pile scored.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", action="store_true",
                    help="print per tier and horizon, thinned, with baselines")
    ap.add_argument("--pile", choices=PILES, default="scored",
                    help="which baseline to draw in --summary (default: scored)")
    ap.add_argument("--out", nargs="?", const=DEFAULT_OUT, default=None,
                    metavar="DIR",
                    help=f"also dump labels to DIR (default {DEFAULT_OUT}, "
                         "gitignored derived data)")
    ap.add_argument("--version", action="store_true",
                    help="print this file's git hash and exit")
    a = ap.parse_args()
    if a.version:
        print(labeler_version())
    elif a.summary:
        run_summary(a.pile)
    else:
        run_label(a.out)
