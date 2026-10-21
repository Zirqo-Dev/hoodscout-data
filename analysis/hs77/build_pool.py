"""Pre-watermark return pool for the HS-77 power simulation.

Reads ONLY data/history.jsonl as of commit d9f8047 (2026-09-04 to 2026-10-02),
i.e. data from before the H-0001..3 watermark (2026-10-18T13:30Z) that was
already used by the in-sample backtest. Mirrors label.py's 6h outcome logic
(window [5.25h, 6.75h], gone_no_liq = -100%, gone_unseen censored, 1% fallback
fee, constant-product impact for $500) and its same-pile baseline (20 seeded
peers, median). "Scored" pile here = rows carrying a tier (history.jsonl has no
txns24 field) -- an approximation, stated in the report.
"""
import bisect, hashlib, json, random, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta

SRC, OUT = sys.argv[1], sys.argv[2]
WATERMARK = datetime.fromisoformat("2026-10-18T13:30:25+00:00")
H, TOL, FEE, SIZE, DRAWS = 6.0, 0.75, 0.01, 500, 20

rows = []
for line in open(SRC):
    d = json.loads(line)
    d["dt"] = datetime.fromisoformat(d["ts"])
    assert d["dt"] + timedelta(hours=H + TOL) < WATERMARK, "post-watermark row"
    d["rms"] = d.get("recent_move_score", d.get("momentum_score"))
    rows.append(d)

by_ca = defaultdict(list)
for r in rows:
    by_ca[r["ca"]].append(r)
for v in by_ca.values():
    v.sort(key=lambda r: r["dt"])
times = {ca: [r["dt"] for r in v] for ca, v in by_ca.items()}
latest = max(r["dt"] for r in rows)

# thin entry snapshots hourly, earliest per bucket (label.thin_snapshots)
keep, seen = set(), set()
for ts in sorted({r["ts"] for r in rows}):
    b = int(datetime.fromisoformat(ts).timestamp() // 3600)
    if b not in seen:
        seen.add(b); keep.add(ts)

pile = defaultdict(list)
for r in rows:
    if r.get("tier"):
        pile[r["ts"]].append(r["ca"])
row_at = {(r["ts"], r["ca"]): r for r in rows}


def outcome(e):
    target = e["dt"] + timedelta(hours=H)
    lo, hi = target - timedelta(hours=TOL), target + timedelta(hours=TOL)
    if latest < hi:
        return None, "pending"
    rs, tl = by_ca[e["ca"]], times[e["ca"]]
    i, j = bisect.bisect_left(tl, lo), bisect.bisect_right(tl, hi)
    win = rs[i:j]
    hit = min(win, key=lambda r: abs(r["dt"] - target)) if win else None
    p_in, liq_in = e.get("price"), e.get("liq") or 0.0
    if liq_in <= 0:
        return None, "not_enterable"
    if hit and (hit.get("liq") or 0) > 0 and hit.get("price") and p_in:
        gross = hit["price"] / p_in - 1.0
        imp = 2.0 * SIZE / liq_in + 2.0 * SIZE / hit["liq"]
        return max(-1.0, gross - 2 * FEE - imp), "ok"
    k = bisect.bisect_right(tl, e["dt"])
    prior = rs[k:j]
    if hit or (prior and (prior[-1].get("liq") or 0) <= 0):
        return -1.0, "gone_no_liq"
    return None, "gone_unseen"


cache = {}
def out_of(ts, ca):
    if (ts, ca) not in cache:
        cache[(ts, ca)] = outcome(row_at[(ts, ca)])
    return cache[(ts, ca)]


pool = []
for r in rows:
    if r["ts"] not in keep or not r.get("tier"):
        continue
    net, status = out_of(r["ts"], r["ca"])
    if status in ("pending", "not_enterable"):
        continue
    members = [c for c in pile[r["ts"]] if c != r["ca"]]
    seed = hashlib.sha256(f"{r['ts']}|{r['ca']}|scored".encode()).digest()
    peers = random.Random(int.from_bytes(seed[:8], "big")).sample(
        members, min(DRAWS, len(members)))
    vals = [v for v in (out_of(r["ts"], c)[0] for c in peers) if v is not None]
    y, w, _ = r["dt"].isocalendar()
    pool.append({"ts": r["ts"], "ca": r["ca"], "week": f"{y}-W{w:02d}",
                 "tier": r["tier"], "rms": r["rms"], "status": status,
                 "net": net, "base": statistics.median(vals) if vals else None,
                 "cell": int(r["dt"].timestamp() // (H * 3600))})

json.dump(pool, open(OUT, "w"))
print("pool rows", len(pool))
