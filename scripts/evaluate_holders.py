#!/usr/bin/env python3
"""Evaluate H-0007 (holder concentration vs 6h net return): the evaluator join.

WHY A SEPARATE SCRIPT. evaluate.py routes rules by id and its decision path was
audited AST-identical for the 2026-11-09 read. H-0007 needs a join that nothing
in evaluate.py does: label rows against data/holders/ snapshots, their
coverage sidecars and data/holders/contract_probe.json. Adding it there would
edit an audited, frozen file. So the join and H-0007's rule live here, and
only the shared arithmetic is imported unchanged: label.py for the outcomes,
evaluate.prepare_labels / gates_for / med_net / MIN_SUBPERIOD_ROWS for the
rows, gates and medians. holders.py is NOT imported: the collector can change,
and a collector edit must never move this verdict, so every constant the rule
needs is written out below.

FREEZE (docs/EVIDENCE_LAYER.md, Layer 3d step 8). Once this file is committed,
H-0007's rule, exclusions, thresholds, population and deciding statistic may
not change. Any such change is a new hypothesis the owner approves. Bug fixes
must be shown verdict-neutral (AST-identical decision path, or identical blind
outputs on the pinned data). The interpretation choices below were approved by
the owner on 2026-11-10, before any H-0007 outcome was computed; the plan and
its table are in the commit that adds this file.

READ DATE. Mon 2027-02-15 (H-0007's stop_by), decided by the owner on
2026-11-10. --unblind and --report are refused before 2027-02-15T07:00:00Z,
checked before anything is loaded. Read day follows Layer 3d.

WEEKS (the owner's D3 rule, 2026-11-10; the same rule as Layer 3d step 5b).
A usable week is an ISO week (of the entry ts) with at least
MIN_SUBPERIOD_ROWS joined rows, both arms together, whose gap is computable.
The sign test uses the 4 usable weeks with the most joined rows (on a tie, the
later week): at least 3 of those 4 must be strictly below zero, the pooled
gap's required sign. A zero week holds no sign. Fewer than 4 usable weeks, for
a gap that clears the bar, is inconclusive (instability), never waiting.

THE JOIN, in short. A holders snapshot's time is its COMPLETION time, sidecar
ts + duration_s, so no holder data collected after an entry can join it. A
snapshot counts only if its sidecar reports coverage >= 0.90 and a usable
completion time; otherwise it was never collected. A label row joins the
nearest counted snapshot that completed at or before its entry, at most 3.5 h
earlier, and contains its ca (lower-cased on both sides). If that holders row
fails supply sanity, probe completeness, balance parsing or conc10 <= 1, the
label row is dropped, never re-joined to an older snapshot, and counts against
the 60% join-coverage clause.

Blind by default: stdout carries counts and fractions only. --report requires
--unblind and writes to reports/.

NOTHING HERE HAS BEEN TRADED. Every return is modelled -- see label.py.
"""
import argparse, bisect, glob, hashlib, json, math, os, re, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluate as E  # noqa: E402  (rows, gates and medians, unchanged)
import label  # noqa: E402

# ------------------------------------------------------------ registration

HYP_ID = "H-0007"
HYP_LINE = 7                 # 1-based line in data/hypotheses.jsonl
# sha256 of line 7's bytes without its newline, from HEAD b881da7 (2026-11-10)
HYP_SHA256 = "b5a0627a44878344fbdeae2ba85626f81a94b0c22e0e1266aa065dc08b1438ad"
REGISTRY = E.REGISTRY
PILE = "scored"              # feeds only baseline peers, which H-0007 never reads
READ_AT = datetime(2027, 2, 15, 7, 0, 0, tzinfo=timezone.utc)
DEFAULT_REPORT_DIR = E.DEFAULT_REPORT_DIR

# ------------------------------------------------------------ the rule

HORIZON = "6h"
WINDOW = timedelta(hours=3.5)
COVERAGE_FLOOR = 0.90
TOP_N = 10                   # conc10: the 10 largest surviving holders
JOIN_FLOOR = Fraction(60, 100)
TIE_CAP = Fraction(25, 100)
GAP_BAR = -0.01              # -1.0pp
SELECT_WEEKS = 4
MIN_MATCHING_WEEKS = 3
MIN_WEEK_SNAPSHOTS = 3
MIN_SUBPERIOD_ROWS = E.MIN_SUBPERIOD_ROWS

ZERO_ADDRESS = "0x" + "0" * 40
FIXED_INFRA = ("0x8366a39cc670b4001a1121b8f6a443a643e40951",   # Uniswap v4 PoolManager
               "0x267444d099b10fb5ed7c3cc7b7c767adca574952")   # PonsV2LaunchLocker
F_LABELS = ("RehypeDopplerHookInitializer", "DopplerHookInitializer",
            "PonsV2BondingCurve", "PonsV2MemeHook", "StandardLaunchFactory",
            "DLMM Token", "Morpho", "TibbirBridgeRobinhood")
PROBE_SETTLED = ("ok", "revert")
DIGITS = re.compile(r"[0-9]+")

HOLDERS_DIR = "data/holders"
PROBE_PATH = HOLDERS_DIR + "/contract_probe.json"
# data/holders/exclusions_hint.json is deliberately never read (line 7).

DROP_ORDER = ("sanity", "probe", "balance", "units")
# sanity: total_supply is not a digit string > 0; probe: an unsettled contract
# holder; balance: a surviving holder's balance is not a digit string (not in
# line 7; none exists in the data at build time); units: conc10 > 1.

EXIT_REPORT_NEEDS_UNBLIND = 2
EXIT_BEFORE_READ = 3
EXIT_REGISTRY_CHANGED = 4

REPORT_NEEDS_UNBLIND = E.REPORT_NEEDS_UNBLIND


class RegistryChanged(Exception):
    pass


def version(path=None):
    return E.version(path or os.path.abspath(__file__))


def lc(s):
    return s.lower() if isinstance(s, str) else ""


# ------------------------------------------------------------ registry

def load_registered(path=None):
    """H-0007's line, refused unless its bytes hash to HYP_SHA256."""
    path = path or REGISTRY
    with open(path, "rb") as f:
        lines = f.read().split(b"\n")
    if len(lines) < HYP_LINE:
        raise RegistryChanged(f"{path} has fewer than {HYP_LINE} lines")
    raw = lines[HYP_LINE - 1]
    got = hashlib.sha256(raw).hexdigest()
    if got != HYP_SHA256:
        raise RegistryChanged(f"line {HYP_LINE} of {path} hashes to {got[:16]}, "
                              f"expected {HYP_SHA256[:16]}")
    h = json.loads(raw)
    if h.get("id") != HYP_ID:
        raise RegistryChanged(f"line {HYP_LINE} is {h.get('id')}, not {HYP_ID}")
    return h


# ------------------------------------------------------------ snapshots

def parse_ts(s):
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError("naive timestamp")
    return d.astimezone(timezone.utc)


def snapshot_state(meta):
    """(state, completion time). state is counted, no_sidecar, no_time or
    below_floor; only counted snapshots exist for the join."""
    if not isinstance(meta, dict):
        return "no_sidecar", None
    try:
        ts = parse_ts(meta["ts"])
        dur = meta["duration_s"]
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) \
                or not math.isfinite(dur) or dur < 0:
            raise ValueError("duration_s")
        done = ts + timedelta(seconds=dur)
    except (KeyError, TypeError, ValueError, OverflowError):
        return "no_time", None
    cov = meta.get("coverage")
    if isinstance(cov, bool) or not isinstance(cov, (int, float)) \
            or not cov >= COVERAGE_FLOOR:
        return "below_floor", done
    return "counted", done


def load_snapshots(root=None):
    """Every holders snapshot with its state; rows are read for counted ones."""
    root = root or HOLDERS_DIR
    out = []
    for p in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
        meta = None
        try:
            with open(p[:-len(".jsonl")] + ".meta.json") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None
        state, done = snapshot_state(meta)
        rows = []
        if state == "counted":
            with open(p) as f:
                for line in f:
                    if line.strip():
                        rows.append(json.loads(line))
        out.append({"path": p, "state": state, "done": done, "rows": rows})
    return out


def load_probe(path=None):
    with open(path or PROBE_PATH) as f:
        c = json.load(f)
    return {lc(k): v for k, v in c.items()} if isinstance(c, dict) else {}


# ------------------------------------------------------------ rules (a)-(f)

def is_burn(a):                                       # (a)
    a = lc(a)
    return a == ZERO_ADDRESS or a.endswith("dead")


def removed_a_to_d(a, row):
    a = lc(a)
    return (is_burn(a) or a == lc(row.get("ca"))          # (a), (b)
            or (a != "" and a == lc(row.get("pool")))     # (c)
            or a in FIXED_INFRA)                          # (d)


def removed_e(a, row, probe):
    e = probe.get(lc(a))
    ca = lc(row.get("ca"))
    return (isinstance(e, dict) and e.get("result") == "ok"
            and ca != "" and ca in (lc(e.get("token0")), lc(e.get("token1"))))


def removed_f(h):
    return h.get("is_contract") is True and h.get("label") in F_LABELS


def survives(h, row, probe):
    a = h.get("address")
    return not (removed_a_to_d(a, row) or removed_e(a, row, probe)
                or removed_f(h))


def probe_complete(row, probe):
    """Every is_contract holder that (a)-(d) leave must be ok or revert."""
    for h in row.get("holders") or []:
        a = h.get("address")
        if h.get("is_contract") is True and not removed_a_to_d(a, row):
            e = probe.get(lc(a))
            if not (isinstance(e, dict) and e.get("result") in PROBE_SETTLED):
                return False
    return True


def sane_supply(v):
    """total_supply as an int if it is a JSON string of digits > 0, else None."""
    if isinstance(v, str) and DIGITS.fullmatch(v):
        n = int(v)
        return n if n > 0 else None
    return None


def unlabelled(h):
    lab = h.get("label")
    return h.get("is_contract") is True and (
        lab is None or (isinstance(lab, str) and not lab.strip()))


def assess(row, probe):
    """One holders row -> (drop reason or None, info)."""
    supply = sane_supply(row.get("total_supply"))
    if supply is None:
        return "sanity", None
    if not probe_complete(row, probe):
        return "probe", None
    surv = [h for h in row.get("holders") or [] if survives(h, row, probe)]
    bals = []
    for h in surv:
        b = h.get("balance")
        if not (isinstance(b, str) and DIGITS.fullmatch(b)):
            return "balance", None
        bals.append((int(b), h))
    bals.sort(key=lambda x: -x[0])
    top = bals[:TOP_N]
    num = sum(b for b, _ in top)
    c = Fraction(num, supply)
    if c > 1:
        return "units", None
    unl = sum(b for b, h in top if unlabelled(h))
    return None, {"conc10": c, "supply": supply,
                  "unl_half": num > 0 and 2 * unl >= num}


def split_snapshot(snap, probe):
    """Assess every row of one counted snapshot and split it into the top
    quartile and the rest. Returns {ca: entry}, with entry["drop"] set for a
    dropped row and entry["top"], entry["tie"] for a kept one."""
    out, dup = {}, 0
    for row in snap["rows"]:
        ca = lc(row.get("ca"))
        if not ca:
            continue
        if ca in out:
            dup += 1
            continue
        reason, info = assess(row, probe)
        out[ca] = {"drop": reason, **(info or {})}
    kept = sorted((ca for ca, e in out.items() if e["drop"] is None),
                  key=lambda ca: (-out[ca]["conc10"], -out[ca]["supply"], ca))
    n = len(kept)
    k = math.ceil(n / 4)
    first_out = out[kept[k]] if k < n else None
    for i, ca in enumerate(kept):
        e = out[ca]
        e["top"] = i < k
        e["tie"] = None
        if e["top"] and first_out is not None \
                and e["conc10"] == first_out["conc10"]:
            e["tie"] = "second" if e["supply"] != first_out["supply"] else "third"
    snap["dup_ca"] = dup
    return out


# ------------------------------------------------------------ the join

def iso_week(d):
    y, w, _ = d.astimezone(timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def join(labels, snaps, probe, watermark):
    """Label rows (6h, ts > watermark) against counted snapshots.

    Returns a dict with: base (label rows considered), pop (population: a
    counted snapshot containing the ca completed in the window), joined
    [(label row, entry)], drops Counter, snaps_by_state Counter.
    """
    counted = [s for s in snaps if s["state"] == "counted"]
    counted.sort(key=lambda s: (s["done"], s["path"]))
    split = [split_snapshot(s, probe) for s in counted]
    by_ca = defaultdict(list)                 # ca -> [(done, i)], ascending
    for i, s in enumerate(counted):
        for ca in split[i]:
            by_ca[ca].append((s["done"], i))
    done_lists = {ca: [d for d, _ in v] for ca, v in by_ca.items()}

    base = [o for o in labels if o["h"] == HORIZON and o["ts"] > watermark]
    joined, drops, pop = [], Counter(), 0
    for o in base:
        ca = lc(o["ca"])
        lst = done_lists.get(ca)
        if not lst:
            continue
        t = parse_ts(o["ts"])
        j = bisect.bisect_right(lst, t) - 1   # last completed at or before t
        if j < 0 or t - lst[j] > WINDOW:
            continue
        pop += 1
        e = split[by_ca[ca][j][1]][ca]
        if e["drop"] is not None:
            drops[e["drop"]] += 1
            continue
        joined.append((o, e))
    return {"base": len(base), "pop": pop, "joined": joined, "drops": drops,
            "snaps": Counter(s["state"] for s in snaps),
            "counted_weeks": Counter(iso_week(s["done"]) for s in counted),
            "dup_ca": sum(s.get("dup_ca", 0) for s in counted)}


# ------------------------------------------------------------ the decision

def gap_of(top, rest):
    a, b = E.med_net(top), E.med_net(rest)
    return None if a is None or b is None else a - b


def weeks(joined):
    """Per ISO week of the entry: joined rows, the week gap, usable."""
    by = defaultdict(lambda: ([], []))
    for o, e in joined:
        by[iso_week(parse_ts(o["ts"]))][0 if e["top"] else 1].append(o)
    out = {}
    for wk, (top, rest) in sorted(by.items()):
        g = gap_of(top, rest)
        n = len(top) + len(rest)
        out[wk] = {"rows": n, "top": len(top), "rest": len(rest), "gap": g,
                   "usable": n >= MIN_SUBPERIOD_ROWS and g is not None}
    return out


def select_weeks(wk):
    """The SELECT_WEEKS usable weeks with the most rows; a tie takes the later."""
    usable = [w for w, v in wk.items() if v["usable"]]
    usable.sort(key=lambda w: (wk[w]["rows"], w), reverse=True)
    return sorted(usable[:SELECT_WEEKS])


def frac(num, den):
    return Fraction(num, den) if den else None


def decide(gates_met, gap, coverage, tie_share, n_usable, week_snaps,
           week_gaps, now_iso, stop_by):
    """(status, reason). The order is the one approved on 2026-11-10:
    gates; join coverage; tie-break; fail; counted snapshots in the selected
    weeks; fewer than 4 usable weeks; 3 of 4 strictly negative."""
    if not gates_met:
        return ("expired" if now_iso > stop_by else "waiting"), "gates not met"
    if gap is None:      # unreachable: met gates mean scored rows in both arms
        raise ValueError("gates met but the gap is not computable")
    if coverage is None or coverage < JOIN_FLOOR:
        return "inconclusive", "join coverage below 60%"
    if tie_share is not None and tie_share > TIE_CAP:
        return "inconclusive", "more than 25% of the top arm entered on a tie-break"
    if gap > GAP_BAR:
        return "fail", "the gap is above -1.0pp with min_eff_n met in both arms"
    if any(n < MIN_WEEK_SNAPSHOTS for n in week_snaps):
        return "inconclusive", "fewer than 3 counted snapshots in a selected week"
    if n_usable < SELECT_WEEKS:
        return "inconclusive", "instability: fewer than 4 usable weeks"
    if sum(1 for g in week_gaps if g < 0) >= MIN_MATCHING_WEEKS:
        return "pass", "the gap is at or below -1.0pp and holds in 3 of 4 weeks"
    return "inconclusive", "instability: the sign holds in fewer than 3 of 4 weeks"


def censored(rows):
    return frac(sum(1 for o in rows if o.get("status") == "gone_unseen"),
                len(rows))


def evaluate(h, labels, snaps, probe, now_iso):
    j = join(labels, snaps, probe, h["log_watermark"])
    top = [o for o, e in j["joined"] if e["top"]]
    rest = [o for o, e in j["joined"] if not e["top"]]
    gates = {"top": E.gates_for(top, h), "rest": E.gates_for(rest, h)}
    met = all(g["eff_n"] >= h["min_eff_n"] and g["tokens"] >= h["min_n_tokens"]
              for g in gates.values())
    ties = Counter(e["tie"] for o, e in j["joined"] if e["top"] and e["tie"])
    coverage = frac(len(j["joined"]), j["pop"])
    tie_share = frac(sum(ties.values()), len(top))
    wk = weeks(j["joined"])
    sel = select_weeks(wk)
    n_usable = sum(1 for v in wk.values() if v["usable"])
    week_snaps = [j["counted_weeks"].get(w, 0) for w in sel]
    gap = gap_of(top, rest)
    status, reason = decide(met, gap, coverage, tie_share, n_usable,
                            week_snaps, [wk[w]["gap"] for w in sel],
                            now_iso, h["stop_by"])
    unl = {k: frac(sum(1 for o, e in j["joined"]
                       if e["top"] == (k == "top") and e["unl_half"]), len(rs))
           for k, rs in (("top", top), ("rest", rest))}
    return {"join": j, "gates": gates, "gates_met": met, "ties": ties,
            "coverage": coverage, "tie_share": tie_share, "weeks": wk,
            "selected": sel, "n_usable": n_usable, "week_snaps": week_snaps,
            "gap": gap, "status": status, "reason": reason,
            "censored": {"top": censored(top), "rest": censored(rest),
                         "pooled": censored(top + rest)},
            "unlabelled_half": unl,
            "medians": {"top": E.med_net(top), "rest": E.med_net(rest)}}


# ------------------------------------------------------------ output

def f3(x):
    return "-" if x is None else f"{float(x):.3f}"


def print_blind(r):
    """Counts and fractions only: no outcome figure and no decision word."""
    j = r["join"]
    print(f"  {HYP_ID}  label rows {j['base']}  population {j['pop']}  "
          f"joined {len(j['joined'])}  join coverage {len(j['joined'])}/"
          f"{j['pop']} = {f3(r['coverage'])}")
    print("          drops by reason: " + ", ".join(
        f"{k} {j['drops'].get(k, 0)}" for k in DROP_ORDER)
        + f"  (duplicate ca rows ignored {j['dup_ca']})")
    s = j["snaps"]
    print(f"          snapshots: counted {s.get('counted', 0)}, below floor "
          f"{s.get('below_floor', 0)}, no sidecar {s.get('no_sidecar', 0)}, "
          f"no completion time {s.get('no_time', 0)}")
    for k in ("top", "rest"):
        g = r["gates"][k]
        print(f"          arm {k:4s} rows {g['n']}  eff_n {g['eff_n']}/"
              f"{g['min_eff_n']}  tokens {g['tokens']}/{g['min_n_tokens']}")
    print(f"          top-arm rows entered on a tie-break: second key "
          f"{r['ties'].get('second', 0)}, third key {r['ties'].get('third', 0)}"
          f", of {r['gates']['top']['n']}")
    print(f"          usable weeks {r['n_usable']}; selected: " + (", ".join(
        f"{w} ({r['weeks'][w]['rows']} rows, {n} counted snapshots)"
        for w, n in zip(r["selected"], r["week_snaps"])) or "none"))
    print(f"          gone_unseen share, both arms together "
          f"{f3(r['censored']['pooled'])}")


def pp(v):
    return E.pp(v).strip()


def render(r, h, meta):
    j, g = r["join"], r["gates"]
    L = [f"# {HYP_ID} evaluation — {meta['run_at'][:19]}Z", "",
         "| | |", "|---|---|",
         f"| evaluator | `scripts/evaluate_holders.py` `{meta['evaluator'][:12]}` |",
         f"| labeler | `{meta['labeler'][:12]}` (registered "
         f"`{h['labeler_version'][:12]}`) |",
         f"| registry line {HYP_LINE} sha256 | `{HYP_SHA256[:16]}` (checked) |",
         f"| log | {meta['rows']} rows |", ""]
    if meta.get("mismatch"):
        L += [f"> **Labeler mismatch.** {meta['mismatch']}", ""]
    L += ["Nothing here has been traded. Every return is modelled; see "
          "`scripts/label.py`.", "",
          f"> {h['statement']}", "",
          f"## Status — `{r['status']}`", "", f"Clause: {r['reason']}.", "",
          f"**Gap** — MEDIAN(ret_net_500 | top quartile) minus "
          f"MEDIAN(ret_net_500 | rest): **{pp(r['gap'])}pp** "
          f"(top {pp(r['medians']['top'])}pp, rest {pp(r['medians']['rest'])}pp).",
          "",
          "| arm | rows | scored | eff_n | min | tokens | min | gone_unseen |",
          "|---|---|---|---|---|---|---|---|"]
    for k in ("top", "rest"):
        x = g[k]
        L += [f"| {k} | {x['n']} | {x['scored']} | {x['eff_n']} | {x['min_eff_n']}"
              f" | {x['tokens']} | {x['min_n_tokens']} | "
              f"{f3(r['censored'][k])} |"]
    L += ["", f"Censoring, both arms together: {f3(r['censored']['pooled'])} "
          f"against max_censored_pct {h['max_censored_pct']} (as a share, "
          f"{h['max_censored_pct'] / 100:.2f}). This script does not gate on "
          "it; Layer 3d step 5 (D1) is applied by the person reading.", "",
          f"**Join coverage** — {len(j['joined'])} of {j['pop']} population "
          f"rows = {f3(r['coverage'])} (floor 0.60). Label rows considered "
          f"{j['base']}. Drops: " + ", ".join(
              f"{k} {j['drops'].get(k, 0)}" for k in DROP_ORDER) + ".", "",
          f"**Snapshots** — " + ", ".join(f"{k} {v}" for k, v in
                                          sorted(j["snaps"].items())) + ".", "",
          f"**Tie-break** — top-arm rows that entered on the second key "
          f"{r['ties'].get('second', 0)}, third key {r['ties'].get('third', 0)},"
          f" of {g['top']['n']}: {f3(r['tie_share'])} (cap 0.25).", "",
          f"**Weeks** — {r['n_usable']} usable; selected "
          f"{', '.join(r['selected']) or 'none'}.", "",
          "| week | rows | top | rest | gap | usable | selected | counted snapshots |",
          "|---|---|---|---|---|---|---|---|"]
    for w, v in r["weeks"].items():
        L += [f"| {w} | {v['rows']} | {v['top']} | {v['rest']} | "
              f"{pp(v['gap'])}pp | {'✓' if v['usable'] else '✗'} | "
              f"{'✓' if w in r['selected'] else ''} | "
              f"{j['counted_weeks'].get(w, 0)} |"]
    u = r["unlabelled_half"]
    L += ["", "### Reporting only: does not affect the status", "",
          f"Share of rows where unlabelled contracts are at least half of "
          f"conc10: top {f3(u['top'])}, rest {f3(u['rest'])}.", "",
          "<details><summary>registered clauses</summary>", "",
          f"- **pass_if** {h['pass_if']}", f"- **fail_if** {h['fail_if']}",
          f"- **inconclusive_if** {h['inconclusive_if']}", "", "</details>", ""]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------ main

def _now():
    return datetime.now(timezone.utc)


def load_inputs():
    rows, by_ca, times, pile_idx, _ = label.load_log()
    if not rows:
        return None
    labels, _ = E.prepare_labels(rows, by_ca, times, pile_idx, PILE)
    return {"rows": len(rows), "labels": labels,
            "snaps": load_snapshots(), "probe": load_probe()}


def main(a, now=None):
    if a.report is not None and not a.unblind:
        print(REPORT_NEEDS_UNBLIND, file=sys.stderr)
        return EXIT_REPORT_NEEDS_UNBLIND
    clock = now or _now
    # the clock is checked before anything is loaded, so a refused run
    # computes nothing
    if a.unblind:
        t = clock()
        if t < READ_AT:
            print(f"refused: {HYP_ID} may not be unblinded before "
                  f"{READ_AT.isoformat()} (now {t.isoformat()}). Nothing was "
                  "run.", file=sys.stderr)
            return EXIT_BEFORE_READ
    try:
        h = load_registered()
    except (OSError, ValueError, RegistryChanged) as e:
        print(f"refused: {e}. Nothing was run.", file=sys.stderr)
        return EXIT_REGISTRY_CHANGED
    x = load_inputs()
    if x is None:
        print(f"no log rows under {label.LOG_DIR}/ -- nothing to evaluate")
        return 1
    lv = label.labeler_version()
    mismatch = None
    if lv != h["labeler_version"]:
        mismatch = (f"scripts/label.py is `{lv[:12]}`; {HYP_ID} was written "
                    f"against {h['labeler_version'][:12]}. The arithmetic has "
                    "changed since registration, so these numbers are not what "
                    "the hypothesis was written about.")
        print(f"WARNING: {mismatch}\n")
    run_at = clock().isoformat()
    r = evaluate(h, x["labels"], x["snaps"], x["probe"], run_at)
    ev = version()
    print(f"evaluator {ev[:12]}  labeler {lv[:12]}  log {x['rows']} rows  "
          f"pile {PILE}")
    if not a.unblind:
        # worded without the words it withholds, so a test can grep for them
        print("blind run: counts and coverage only; --unblind prints the "
              "decision outputs")
        print_blind(r)
        return 0
    print(f"  {HYP_ID}  {r['status']:14s} gap {E.pp(r['gap'])}pp  "
          f"coverage {f3(r['coverage'])}  ({r['reason']})")
    if a.report is not None:
        meta = {"run_at": run_at, "evaluator": ev, "labeler": lv,
                "rows": x["rows"], "mismatch": mismatch}
        path = a.report or os.path.join(
            DEFAULT_REPORT_DIR, f"eval-holders-{run_at[:10]}-{ev[:8]}.md")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(render(r, h, meta))
        print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help=f"write a markdown report (default {DEFAULT_REPORT_DIR}/"
                         "eval-holders-<date>-<evaluator>.md). Requires --unblind.")
    blind = ap.add_mutually_exclusive_group()
    blind.add_argument("--blind", action="store_true",
                       help="the default: counts and coverage only")
    blind.add_argument("--unblind", action="store_true",
                       help="print the decision; refused before "
                            "2027-02-15T07:00:00Z")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(version())
    else:
        sys.exit(main(a))
