#!/usr/bin/env python3
"""H-0003 post-read check: exits priced from a different pool than the entry.

Run ONLY from the root of a git worktree checked out at the read pin:

    git worktree add <dir> a782e6e5
    cd <dir> && python /path/to/analysis/h0003-poolswitch/poolswitch.py

It imports the worktree's own scripts/label.py and scripts/evaluate.py,
unchanged, and refuses to run unless the worktree is at the pin, both scripts
hash to the versions the 2026-11-09 read used, and the newest data/log ts is
before H-0009's log_watermark -- so it can never see an H-0009 row.

H-0003 was read on 2026-11-09, so printing its returns is allowed. This is
not evaluate.py --unblind/--report and must never be pointed at current data.

How the exit row is found: label.outcome() stores the ts of the print it used
in t_actual -- the row nearest entry+6h within +-0.75h, or, for a gone_no_liq
row with no print in that window, the last row seen before it. The exit row is
the log row at (t_actual, ca). A row is a pool switch when that row's `pool`
differs from the entry row's `pool`. gone_unseen rows have no exit row; a row
where either pool is null is counted as unknown, not as a switch.

Variants drop population rows only. Every remaining row keeps the baseline
peers the read gave it (baseline peers unchanged).

NOTHING HERE HAS BEEN TRADED. Every return is modelled -- see label.py.
"""
import hashlib, json, os, subprocess, sys
from collections import Counter
from datetime import datetime

PIN = "a782e6e59f867d62d9a5b7ab20d89c89574927ab"
EVALUATOR = "76a3c450d4268dce6b126ff380e1f38e9025bde6"
LABELER = "3ba1b3524df9d1412ad530e5c35768a9ee92c0ef"
# data/hypotheses.jsonl, H-0009 line, at main after 2026-11-10. The pin's
# registry has no H-0009 line, so the value is fixed here.
H0009_WATERMARK = "2026-11-10T12:30:29.042146+00:00"
HID = "H-0003"
PILE = "scored"                     # the read's default pile
TOKEN = "0x8fc98b871678f9749d17d968a476386f2da6f831"
TOKEN_TS = "2026-10-27T20:00"       # entry ts prefix of the reviewed row
TAIL_NET = 9.0                      # +900% net500
AS_READ = {"a": -13.12, "b": -10.59, "c": 3294.38}   # pp, reports/eval-2026-11-09
PRICED = ("ok", "thin")


class Refused(Exception):
    pass


def blob_hash(path):
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def check_before_watermark(rows, watermark=H0009_WATERMARK):
    """Refuse if any log row is at or after the watermark."""
    if not rows:
        raise Refused("no log rows")
    newest = max(r["dt"] for r in rows)
    if newest >= datetime.fromisoformat(watermark):
        raise Refused(f"newest log ts {newest.isoformat()} is not before "
                      f"H-0009's log_watermark {watermark}")
    return newest


def exit_row(o, entry_of):
    t = o.get("t_actual")
    return entry_of.get((t, o["ca"])) if t else None


def classify(o, entry_of):
    """'switch', 'same', 'unknown' (a pool is null) or 'no_exit'."""
    ex = exit_row(o, entry_of)
    if ex is None:
        return "no_exit"
    p_in, p_out = entry_of[(o["ts"], o["ca"])].get("pool"), ex.get("pool")
    if not p_in or not p_out:
        return "unknown"
    return "same" if p_in == p_out else "switch"


def is_token_row(o):
    return o["ca"] == TOKEN and o["ts"].startswith(TOKEN_TS)


def values(E, rows):
    """Clause values (fractions) and the gross median, through evaluate.py."""
    arms = {"all": rows}
    a = E.med_net(rows)
    b = E.gap_vs_baseline(arms)
    mn = E.mean_net(rows)
    c = None if mn is None or a is None else mn - a
    return {"a": a, "b": b, "c": c, "gross": E.med_v(rows, E.v_gross)}


def holds(v):
    return {"a": v["a"] is not None and v["a"] <= -0.05,
            "b": v["b"] is not None and v["b"] <= -0.03,
            "c": v["c"] is not None and v["c"] >= 0.05}


def pp(x):
    return "-" if x is None else f"{100 * x:+.2f}"


def run(L, E, h):
    rows, by_ca, times, pile_idx, _ = L.load_log()
    newest = check_before_watermark(rows)
    labels, _ = E.prepare_labels(rows, by_ca, times, pile_idx, PILE)
    since = h["log_watermark"]
    sel = [o for o in labels if o["h"] == h["horizon"] and o["ts"] > since]
    pop = E.RULES[HID]["arms"](sel)["all"]
    entry_of = {(r["ts"], r["ca"]): r for r in rows}
    cls = {id(o): classify(o, entry_of) for o in pop}

    out = []
    p = out.append
    p(f"pin {PIN[:12]}  evaluator {EVALUATOR[:12]}  labeler {LABELER[:12]}  "
      f"pile {PILE}")
    p(f"log {len(rows)} rows, newest ts {newest.isoformat()} "
      f"< H-0009 watermark {H0009_WATERMARK}")
    p(f"{HID} watermark {since}, horizon {h['horizon']}: "
      f"population {len(pop)} rows")
    p("")
    by_status = Counter((o["status"], cls[id(o)]) for o in pop)
    p("exit pool vs entry pool, by status (exit row = log row at t_actual):")
    for st in sorted({s for s, _ in by_status}):
        p(f"  {st:12s} " + "  ".join(f"{k} {by_status[(st, k)]}"
                                     for k in ("switch", "same", "unknown",
                                               "no_exit")))
    sw_all = [o for o in pop if cls[id(o)] == "switch"]
    sw_priced = [o for o in sw_all if o["status"] in PRICED]
    p(f"pool-switch rows: {len(sw_all)} all statuses, "
      f"{len(sw_priced)} priced exits (ok/thin)")
    p("")

    p(f"rows above +900% net500 ({TAIL_NET:+.0%}):")
    tail = sorted((o for o in pop if (o.get("ret_net_500") or 0) > TAIL_NET),
                  key=lambda o: o["ts"])
    for o in tail:
        en, ex = entry_of[(o["ts"], o["ca"])], exit_row(o, entry_of)
        p(f"  {o['ts'][:16]}Z {o['ca']}  net500 {pp(o['ret_net_500'])}pp  "
          f"status {o['status']}  {cls[id(o)]}")
        p(f"    entry {en['ts'][:16]}Z pool {en.get('pool')} liq {en.get('liq')} "
          f"txns {en.get('txns')} price {en.get('price')}")
        if ex:
            p(f"    exit  {ex['ts'][:16]}Z pool {ex.get('pool')} liq {ex.get('liq')} "
              f"txns {ex.get('txns')} price {ex.get('price')}")
    if not tail:
        p("  none")
    p("")

    variants = [
        ("all rows", pop),
        ("without the reviewed token row", [o for o in pop if not is_token_row(o)]),
        ("without priced pool-switch rows (ok/thin)",
         [o for o in pop if id(o) not in {id(x) for x in sw_priced}]),
        ("without all pool-switch rows (all statuses)",
         [o for o in pop if cls[id(o)] != "switch"]),
    ]
    p("clauses in pp; baseline peers unchanged; [x] = registered bar met")
    p(f"  {'variant':46s} {'rows':>5s} {'eff_n':>5s} {'tok':>4s} "
      f"{'(a)':>10s} {'(b)':>10s} {'(c)':>11s} {'gross med':>10s}")
    res = {}
    for name, rs in variants:
        v, ok = values(E, rs), None
        ok = holds(v)
        g = E.gates_for(rs, h)
        res[name] = v
        mark = lambda k: ("[x]" if ok[k] else "[ ]")
        p(f"  {name:46s} {len(rs):5d} {g['eff_n']:5d} {g['tokens']:4d} "
          f"{pp(v['a']):>7s}{mark('a')} {pp(v['b']):>7s}{mark('b')} "
          f"{pp(v['c']):>8s}{mark('c')} {pp(v['gross']):>10s}")
    base = res["all rows"]
    repro = all(round(100 * base[k], 2) == AS_READ[k] for k in AS_READ)
    p("")
    p(f"reproduces the read ({AS_READ['a']:+.2f} / {AS_READ['b']:+.2f} / "
      f"{AS_READ['c']:+.2f}): {'yes' if repro else 'NO'}")
    return out, repro


def main():
    root = os.getcwd()
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        if head != PIN:
            raise Refused(f"HEAD is {head[:12]}, not the read pin {PIN[:12]}")
        for path, want in (("scripts/evaluate.py", EVALUATOR),
                           ("scripts/label.py", LABELER)):
            got = blob_hash(os.path.join(root, path))
            if got != want:
                raise Refused(f"{path} is {got[:12]}, read used {want[:12]}")
        sys.path.insert(0, os.path.join(root, "scripts"))
        import label as L
        import evaluate as E
        if os.path.dirname(os.path.abspath(E.__file__)) != \
                os.path.join(root, "scripts"):
            raise Refused("evaluate.py was not imported from the worktree")
        hyps = [json.loads(l) for l in open(E.REGISTRY) if l.strip()]
        h = next(x for x in hyps if x["id"] == HID)
        out, repro = run(L, E, h)
    except (Refused, subprocess.CalledProcessError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print("\n".join(out))
    return 0 if repro else 1


if __name__ == "__main__":
    sys.exit(main())
