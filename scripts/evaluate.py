#!/usr/bin/env python3
"""Layer 3c: decide registered hypotheses against the evidence log.

Reads data/hypotheses.jsonl and data/log/, labels through scripts/label.py, and
writes a report. It changes nothing: not the log, not the labels, not the
registry. A status only moves in a commit a person makes, naming the report
that justified it -- see docs/EVIDENCE_LAYER.md.

Why the rules live here and not in the registry: hypotheses.jsonl is
append-only, and H-0001..H-0003 were registered with their rules written in
prose. Adding a machine-readable field to those lines now would be an edit to
a registered hypothesis, which is the one thing the registry forbids. So each
registered id gets a function below, and the report prints the prose beside the
computed number so the two can be checked against each other by eye. Future
hypotheses should carry a structured rule from the start; this per-id table is
a consequence of the first three not having one, not a design to copy.

NOTHING HERE HAS BEEN TRADED. Every return is modelled -- see label.py.
"""
import argparse, hashlib, json, os, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label  # noqa: E402  (the labeler is the arithmetic; this only decides)

REGISTRY = "data/hypotheses.jsonl"
DEFAULT_REPORT_DIR = "reports"
SUBPERIODS = 4          # the clauses say "3 of 4 weekly subperiods"
MIN_SUBPERIOD_ROWS = 30  # below this a week says nothing and is not counted


def version(path):
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def pp(v):
    return "     -" if v is None else f"{100*v:+6.2f}"


# ---------------------------------------------------------------- populations

def pop_tier(tier):
    return lambda rows: [o for o in rows if o["tier"] == tier]


def split_rms(rows):
    hi = [o for o in rows if (o["rms"] or 0) >= 4]
    lo = [o for o in rows if (o["rms"] or 0) <= -4]
    return {"rms>=+4": hi, "rms<=-4": lo}


# -------------------------------------------------------------------- metrics

def med_net(rows):
    v = [o["ret_net_500"] for o in rows if o["ret_net_500"] is not None]
    return statistics.median(v) if v else None


def mean_net(rows):
    v = [o["ret_net_500"] for o in rows if o["ret_net_500"] is not None]
    return statistics.mean(v) if v else None


def med_base(rows):
    v = [o["_base"] for o in rows if o.get("_base") is not None]
    return statistics.median(v) if v else None


def gap_vs_baseline(arms):
    """median(population) - median(per-row baseline medians)."""
    rows = arms["all"]
    m, b = med_net(rows), med_base(rows)
    return None if m is None or b is None else m - b


def gap_between_arms(arms):
    hi, lo = med_net(arms["rms>=+4"]), med_net(arms["rms<=-4"])
    return None if hi is None or lo is None else hi - lo


# ---------------------------------------------------------------------- rules

RULES = {
    "H-0001": {
        "arms": lambda rows: {"all": pop_tier("BEST")(rows)},
        "gate_on": ["all"],
        "metric": gap_vs_baseline,
        "label": "median(BEST net500) - median(baseline)",
        # pass when the null stands; fail needs size AND stability; size
        # without stability is the shape the earlier finding had, and is
        # neither
        "decide": lambda g, stable, _: (
            "pass" if g < 0.01 else ("fail" if stable else "inconclusive")),
    },
    "H-0002": {
        "arms": split_rms,
        "gate_on": ["rms>=+4", "rms<=-4"],
        "metric": gap_between_arms,
        "label": "median(rms>=+4) - median(rms<=-4)",
        "decide": lambda g, stable, _: (
            "fail" if g > -0.01 else ("pass" if stable else "inconclusive")),
    },
    "H-0003": {
        "arms": lambda rows: {"all": pop_tier("EMERGING")(rows)},
        "gate_on": ["all"],
        "metric": gap_vs_baseline,
        "label": "median(EMERGING net500) - median(baseline)",
        # three joint clauses; stability is not one of them, so it is reported
        # but not decided on
        "decide": None,          # see decide_h0003
    },
}


def decide_h0003(arms, gates):
    rows = arms["all"]
    a, b, c = med_net(rows), gap_vs_baseline(arms), None
    mn = mean_net(rows)
    if mn is not None and a is not None:
        c = mn - a
    clauses = [("(a) median <= -5.0pp", a is not None and a <= -0.05, pp(a)),
               ("(b) <= -3.0pp vs baseline", b is not None and b <= -0.03, pp(b)),
               ("(c) mean - median >= +5.0pp", c is not None and c >= 0.05, pp(c))]
    if gates["censored_pct"] > gates["max_censored_pct"]:
        return "inconclusive", clauses
    return ("pass" if all(ok for _, ok, _ in clauses) else "fail"), clauses


# ----------------------------------------------------------------- evaluation

def gates_for(rows, h):
    hours = label.HORIZONS[h["horizon"]][0]
    scored = [o for o in rows if o["ret_net_500"] is not None]
    cens = sum(1 for o in rows if o["status"] == "gone_unseen")
    return {"n": len(rows), "scored": len(scored),
            "eff_n": label.eff_n(scored, hours),
            "tokens": len({o["ca"] for o in scored}),
            "censored_pct": round(100 * cens / len(rows), 1) if rows else 0.0,
            "min_eff_n": h["min_eff_n"], "min_n_tokens": h["min_n_tokens"],
            "max_censored_pct": h["max_censored_pct"]}


def weekly(rows, metric, arms_fn):
    """The metric per ISO week, and how many weeks share the overall sign."""
    by_week = defaultdict(list)
    for o in rows:
        y, w, _ = datetime.fromisoformat(o["ts"]).isocalendar()
        by_week[f"{y}-W{w:02d}"].append(o)
    out = {}
    for wk, rs in sorted(by_week.items()):
        if len(rs) < MIN_SUBPERIOD_ROWS:
            out[wk] = None
            continue
        out[wk] = metric(arms_fn(rs))
    return out


def evaluate(h, labels, since):
    rows = [o for o in labels
            if o["h"] == h["horizon"] and o["ts"] > since]
    rule = RULES.get(h["id"])
    if rule is None:
        return {"id": h["id"], "status": "no rule implemented", "rows": len(rows)}
    arms = rule["arms"](rows)
    gates = {k: gates_for(arms[k], h) for k in rule["gate_on"]}
    worst = min(gates.values(), key=lambda g: g["eff_n"])
    met = all(g["eff_n"] >= h["min_eff_n"] and g["tokens"] >= h["min_n_tokens"]
              for g in gates.values())

    wk = weekly(rows, rule["metric"], rule["arms"])
    usable = [v for v in wk.values() if v is not None]
    g = rule["metric"](arms)
    stable = (len(usable) >= SUBPERIODS and g is not None
              and sum(1 for v in usable if (v > 0) == (g > 0)) >= 3)

    res = {"id": h["id"], "rows": len(rows), "gates": gates, "worst": worst,
           "gates_met": met, "metric": g, "metric_label": rule["label"],
           "weekly": wk, "usable_weeks": len(usable), "stable": stable,
           "clauses": None}
    now = datetime.now(timezone.utc).isoformat()
    if not met:
        res["status"] = "expired" if now > h["stop_by"] else "waiting"
    elif h["id"] == "H-0003":
        res["status"], res["clauses"] = decide_h0003(arms, worst)
    elif g is None:
        res["status"] = "waiting"
    elif len(usable) < SUBPERIODS and rule["decide"](g, True, worst) != \
            rule["decide"](g, False, worst):
        # the decision would turn on stability and there are not yet four
        # usable weeks to judge it by
        res["status"] = "waiting"
    else:
        res["status"] = rule["decide"](g, stable, worst)
    return res


# -------------------------------------------------------------------- report

def render(results, meta):
    L = [f"# Hypothesis evaluation — {meta['run_at'][:19]}Z", ""]
    L += ["| | |", "|---|---|",
          f"| evaluator | `{meta['evaluator'][:12]}` |",
          f"| labeler | `{meta['labeler'][:12]}` |",
          f"| log | {meta['rows']} rows, {meta['snaps']} snapshots, "
          f"{meta['span']} |",
          f"| baseline pile | `{meta['pile']}` |",
          f"| registry | {meta['n_hyp']} hypotheses, "
          f"{meta['n_open']} not yet decided |", ""]
    if meta["mismatch"]:
        L += ["> **Labeler mismatch.** " + meta["mismatch"], ""]
    if meta["since_override"]:
        L += ["> **NOT VALID FOR THE REGISTRY.** The watermark was overridden "
              f"with `--since {meta['since_override']}`, so these numbers were "
              "computed on rows a registered hypothesis may not be evaluated "
              "on. Exercise only.", ""]
    L += ["Nothing here has been traded. Every return is a modelled round trip:",
          "gross less two fees and constant-product impact against half the",
          "reported pool TVL, which is optimistic in every direction that",
          "matters. See `docs/EVIDENCE_LAYER.md`.", ""]
    L += [f"**{meta['n_hyp']} hypotheses are registered.** At a 5% threshold, "
          f"the chance at least one of {meta['n_hyp']} passes on noise alone is "
          f"{100*(1-0.95**meta['n_hyp']):.0f}%. Read any pass against that.", ""]

    for r in results:
        h = meta["by_id"][r["id"]]
        L += [f"## {r['id']} — `{r['status']}`", "",
              f"> {h['statement']}", ""]
        if "gates" not in r:
            L += [f"No rule implemented in `scripts/evaluate.py` for {r['id']}.",
                  ""]
            continue
        L += [f"**Metric** — {r['metric_label']}: "
              f"**{pp(r['metric'])}pp**" if r["metric"] is not None
              else "**Metric** — not computable yet", ""]
        L += ["| arm | rows | scored | eff_n | min_eff_n | tokens | min | cens% | max |",
              "|---|---|---|---|---|---|---|---|---|"]
        for arm, g in r["gates"].items():
            ok = "✓" if g["eff_n"] >= g["min_eff_n"] else "✗"
            okt = "✓" if g["tokens"] >= g["min_n_tokens"] else "✗"
            okc = "✓" if g["censored_pct"] <= g["max_censored_pct"] else "✗"
            L += [f"| {arm} | {g['n']} | {g['scored']} | **{g['eff_n']}** {ok} | "
                  f"{g['min_eff_n']} | {g['tokens']} {okt} | {g['min_n_tokens']} | "
                  f"{g['censored_pct']} {okc} | {g['max_censored_pct']} |"]
        L += [""]
        if r.get("clauses"):
            L += ["| clause | value | holds |", "|---|---|---|"]
            for name, ok, val in r["clauses"]:
                L += [f"| {name} | {val}pp | {'✓' if ok else '✗'} |"]
            L += [""]
        decides = RULES.get(r["id"], {}).get("decide") is not None
        L += [f"**Weekly subperiods** — {r['usable_weeks']} usable of "
              f"{SUBPERIODS} needed (a week under {MIN_SUBPERIOD_ROWS} rows "
              f"says nothing and is not counted); "
              f"sign {'holds' if r['stable'] else 'does not hold'} in 3 of 4."
              + ("" if decides else " This hypothesis's registered clauses do "
                 "not turn on stability, so it is reported here and not "
                 "decided on."), ""]
        if r["weekly"]:
            L += ["| week | metric |", "|---|---|"]
            for wk, v in r["weekly"].items():
                L += [f"| {wk} | {pp(v)}pp |" if v is not None
                      else f"| {wk} | too few rows |"]
            L += [""]
        L += ["<details><summary>registered clauses</summary>", "",
              f"- **pass_if** {h['pass_if']}",
              f"- **fail_if** {h['fail_if']}",
              f"- **inconclusive_if** {h['inconclusive_if']}",
              f"- registered `{h['registered'][:19]}Z`, watermark "
              f"`{h['log_watermark'][:19]}Z`, labeler `{h['labeler_version'][:12]}`",
              "", "</details>", ""]
    L += ["---", "",
          "A status in `data/hypotheses.jsonl` moves only in a commit a person",
          "makes, naming this report. This script never edits the registry."]
    return "\n".join(L) + "\n"


def main(a):
    hyps = [json.loads(l) for l in open(REGISTRY) if l.strip()]
    rows, by_ca, times, pile_idx, nfiles = label.load_log()
    if not rows:
        print(f"no log rows under {label.LOG_DIR}/ -- nothing to evaluate")
        return 1
    latest = max(r["dt"] for r in rows)
    all_ts = {r["ts"] for r in rows}
    keep = {h: label.thin_snapshots(all_ts, label.HORIZONS[h][2])
            for h in label.HORIZONS}
    labels, _ = label.label_all(rows, by_ca, times, latest, keep=keep)
    entry_of = {(r["ts"], r["ca"]): r for r in rows}
    cache = {}
    for o in labels:
        o["_base"] = label.baseline_med(entry_of[(o["ts"], o["ca"])], o["h"],
                                        a.pile, pile_idx, by_ca, times,
                                        latest, cache)

    lv = label.labeler_version()
    registered = {h["labeler_version"] for h in hyps}
    mismatch = None
    if registered and lv not in registered:
        mismatch = (f"scripts/label.py is `{lv[:12]}`; the registry was written "
                    f"against {', '.join(sorted(v[:12] for v in registered))}. "
                    "The arithmetic has changed since registration, so these "
                    "numbers are not what the hypotheses were written about.")

    results = [evaluate(h, labels, a.since or h["log_watermark"]) for h in hyps]
    meta = {"run_at": datetime.now(timezone.utc).isoformat(),
            "evaluator": version(os.path.abspath(__file__)), "labeler": lv,
            "rows": len(rows), "snaps": len(all_ts),
            "span": f"{min(all_ts)[:19]} .. {max(all_ts)[:19]}",
            "pile": a.pile, "n_hyp": len(hyps),
            "n_open": sum(1 for h in hyps if h["status"] == "registered"),
            "mismatch": mismatch, "since_override": a.since,
            "by_id": {h["id"]: h for h in hyps}}

    if mismatch:
        print(f"WARNING: {mismatch}\n")
    print(f"evaluator {meta['evaluator'][:12]}  labeler {lv[:12]}  "
          f"log {len(rows)} rows  pile {a.pile}")
    for r in results:
        g = r.get("worst", {})
        print(f"  {r['id']}  {r['status']:14s} metric {pp(r.get('metric')):>7s}pp  "
              f"eff_n {g.get('eff_n', 0)}/{g.get('min_eff_n', '?')}  "
              f"tokens {g.get('tokens', 0)}/{g.get('min_n_tokens', '?')}  "
              f"weeks {r.get('usable_weeks', 0)}/{SUBPERIODS}")

    if a.report is not None:
        path = a.report or os.path.join(
            DEFAULT_REPORT_DIR,
            f"eval-{meta['run_at'][:10]}-{meta['evaluator'][:8]}.md")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "w").write(render(results, meta))
        print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pile", choices=label.PILES, default="scored")
    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help=f"write a markdown report (default {DEFAULT_REPORT_DIR}/"
                         "eval-<date>-<evaluator>.md)")
    ap.add_argument("--since", metavar="TS", default=None,
                    help="override every watermark. For exercising the "
                         "arithmetic only; the report is marked invalid.")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(version(os.path.abspath(__file__)))
    else:
        sys.exit(main(a))
