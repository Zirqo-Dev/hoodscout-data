#!/usr/bin/env python3
"""Evaluate confirmation hypotheses (Layer 3d step 9) with evaluate.py's code.

Why this wrapper exists: evaluate.py routes every rule by hypothesis id
(RULES["H-0003"], decide_h0003, the `h["id"] == "H-0003"` branches), and the
2026-11-09 read-day audit checked that file AST-identical on its decision
path. A confirmation must use the SAME population, metric, thresholds and
clauses as the line it confirms. Adding "H-0009" to evaluate.py would mean
editing an audited, frozen file; copying the rule here would mean a second
implementation that could drift. Instead, this script hands evaluate.py a copy
of each confirmation line with its id set to the confirmed id, so evaluate.py's
own rule and id-specific branches run unchanged, on the confirmation line's
OWN watermark, gates and stop_by. The result is then relabelled with the
confirmation's own id.

A line is a confirmation when it carries a "confirms" field. Its frozen fields
must equal the confirmed line's; if they do not, nothing is evaluated.

Blind by default: stdout carries exactly what `evaluate.py --blind` prints for
a hypothesis -- rows, gates, eff_n, tokens, weeks and blind process metrics --
never a metric or a status. --unblind and --report are refused until 07:00Z on
the line's read_on date. --report requires --unblind.

NOTHING HERE HAS BEEN TRADED. Every return is modelled -- see label.py.
"""
import argparse, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluate as E  # noqa: E402  (the decision; this only routes to it)
import label  # noqa: E402

PILE = "scored"          # what the 2026-11-09 read used, as registered
READ_HOUR_UTC = 7        # the read pins the first commit at or after 07:00Z
FROZEN = ("uses", "population", "horizon", "metric", "baseline", "pass_if",
          "fail_if", "inconclusive_if", "min_eff_n", "min_n_tokens",
          "max_censored_pct")

EXIT_REPORT_NEEDS_UNBLIND = 2
EXIT_BEFORE_READ = 3
EXIT_BAD_REGISTRY = 4


def confirmations(hyps):
    return [h for h in hyps if h.get("confirms")]


def read_at(h):
    """The earliest moment this line may be unblinded: read_on at 07:00Z."""
    d = datetime.fromisoformat(h["read_on"]).date()
    return datetime(d.year, d.month, d.day, READ_HOUR_UTC, tzinfo=timezone.utc)


def as_confirmed(h, hyps):
    """A copy of confirmation line `h` carrying the confirmed id, so that
    evaluate.py resolves the confirmed rule. Every other field is h's own."""
    target = h["confirms"]
    orig = next((x for x in hyps if x["id"] == target), None)
    if orig is None:
        raise ValueError(f"{h['id']} confirms {target}, which is not registered")
    if target not in E.RULES:
        raise ValueError(f"{h['id']} confirms {target}, which has no rule in "
                         "evaluate.py")
    differ = [k for k in FROZEN if h.get(k) != orig.get(k)]
    if differ:
        raise ValueError(f"{h['id']} differs from {target} in frozen fields: "
                         + ", ".join(differ))
    c = dict(h)
    c["id"] = target
    return c


def evaluate_confirm(h, hyps, labels):
    """evaluate.evaluate_full() of the confirmed rule, since h's OWN watermark,
    relabelled with h's id."""
    r = E.evaluate_full(as_confirmed(h, hyps), labels, h["log_watermark"], None)
    r["id"] = h["id"]
    r["confirms"] = h["confirms"]
    return r


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------- read-day checks, report only
#
# Layer 3d step 9, H-0009 reading rule. Everything below is written only into
# the --unblind --report markdown, after the existing report text. None of it
# is read by the verdict path above: it sets no status, gate or exit code.
#
# Pool-switch definition, the same as analysis/h0003-poolswitch/poolswitch.py
# (re-implemented here; nothing is imported from analysis/): the exit row is
# the log row at (t_actual, ca); a row is a switch when that row's `pool`
# differs from the entry row's `pool`. No exit row (t_actual null, as for
# gone_unseen) is "no_exit"; a null pool on either side is "unknown" -- kept
# in every recompute, never removed.

TAIL_NET = 9.0           # +900% on ret_net_500, the field the clauses use
PRICED = ("ok", "thin")
READ_DAY_FOR = "H-0003"  # the confirmed rule this check is defined for
STEP9 = ("Layer 3d step 9 (H-0009 reading rule): if the confirmation passes "
         "but the pass does not survive removing the pool-switch rows, the "
         "person records INCONCLUSIVE (data quality: exit print from another "
         "pool). This check can only move a pass to INCONCLUSIVE; it never "
         "creates a pass or a fail.")


def exit_row(o, entry_of):
    t = o.get("t_actual")
    return entry_of.get((t, o["ca"])) if t else None


def classify(o, entry_of):
    """'switch', 'same', 'unknown' (a pool is null) or 'no_exit'."""
    ex = exit_row(o, entry_of)
    if ex is None:
        return "no_exit"
    en = entry_of.get((o["ts"], o["ca"])) or {}
    p_in, p_out = en.get("pool"), ex.get("pool")
    if not p_in or not p_out:
        return "unknown"
    return "same" if p_in == p_out else "switch"


def read_day_population(h, hyps, labels):
    """The confirmed rule's population rows, as evaluate() selects them: the
    line's horizon, ts after its own watermark, then the rule's arms. For
    H-0003 the only arm is "all" (EMERGING); the baseline is not an arm but
    each row's own `_base`/`_peers`, which are left untouched."""
    c = as_confirmed(h, hyps)
    rows = [o for o in labels
            if o["h"] == c["horizon"] and o["ts"] > c["log_watermark"]]
    return E.RULES[c["id"]]["arms"](rows)["all"], c


def _recompute(c, rows, stable):
    """decide_h0003, the verdict's own function, on population `rows`, plus
    the gates printed beside it. Only population rows were dropped; every
    remaining row keeps the baseline peers the evaluator gave it."""
    arms = {"all": rows}
    gates = E.gates_for(rows, c)
    verdict, clauses = E.decide_h0003(arms, gates)
    sweep, crosses = E.fee_sweep(c["id"], arms, gates, stable, verdict)
    kverdict, _, kmet = E.known_only(c["id"], c, arms, stable)
    return {"rows": len(rows), "verdict": verdict, "clauses": clauses,
            "holds": all(ok for _, ok, _ in clauses), "gates": gates,
            "sweep_crosses": crosses, "known_verdict": kverdict,
            "known_gate_met": kmet}


def read_day_checks(h, hyps, labels, log_rows, status, stable=False):
    pop, c = read_day_population(h, hyps, labels)
    entry_of = {(r["ts"], r["ca"]): r for r in log_rows if "ca" in r}
    cls = [(o, classify(o, entry_of)) for o in pop]
    sw = [o for o, k in cls if k == "switch"]
    sw_priced = [o for o in sw if o["status"] in PRICED]
    drop_all = {id(o) for o in sw}
    drop_priced = {id(o) for o in sw_priced}
    tail = sorted((o for o in pop if (o.get("ret_net_500") or 0) > TAIL_NET),
                  key=lambda o: o["ts"])
    full = _recompute(c, pop, stable)
    no_sw = _recompute(c, [o for o in pop if id(o) not in drop_all], stable)
    no_pr = _recompute(c, [o for o in pop if id(o) not in drop_priced], stable)
    return {"id": h["id"], "confirms": h["confirms"], "status": status,
            "population": len(pop), "entry_of": entry_of,
            "class": {id(o): k for o, k in cls},
            "switch": sorted(sw, key=lambda o: o["ts"]),
            "switch_priced": len(sw_priced),
            "unknown": sorted((o for o, k in cls if k == "unknown"),
                              key=lambda o: o["ts"]),
            "no_exit": sum(1 for _, k in cls if k == "no_exit"),
            "tail": tail, "full": full, "without_switch": no_sw,
            "without_priced_switch": no_pr,
            "survives": (no_sw["holds"] if status == "pass" else None)}


def _side(r):
    if not r:
        return "-"
    return (f"`{r['ts'][:16]}Z` pool `{r.get('pool')}` liq {r.get('liq')} "
            f"txns {r.get('txns')} price {r.get('price')}")


def _row_table(rows, x):
    if not rows:
        return ["none", ""]
    L = ["| entry | ca | status | net500 pp | class | entry row | exit row |",
         "|---|---|---|---:|---|---|---|"]
    for o in rows:
        L.append(f"| `{o['ts'][:16]}Z` | `{o['ca']}` | {o['status']} | "
                 f"{E.pp(o.get('ret_net_500')).strip()} | "
                 f"{x['class'][id(o)]} | "
                 f"{_side(x['entry_of'].get((o['ts'], o['ca'])))} | "
                 f"{_side(exit_row(o, x['entry_of']))} |")
    return L + [""]


def _recompute_row(name, deciding, v):
    g = v["gates"]
    cl = " | ".join(f"{val.strip()} {'[x]' if ok else '[ ]'}"
                    for _, ok, val in v["clauses"])
    return (f"| {name} | {deciding} | {v['rows']} | {cl} | "
            f"{g['eff_n']}/{g['min_eff_n']} | {g['tokens']}/{g['min_n_tokens']}"
            f" | {g['censored_pct']}/{g['max_censored_pct']} | "
            f"{'yes' if v['sweep_crosses'] else 'no'} | "
            f"{v['known_verdict']}{'' if v['known_gate_met'] else ' (gates not met)'}"
            f" | {v['verdict']} |")


def render_read_day(checks):
    L = ["", "## Read-day checks (report only)", "",
         "Nothing in this section changes a status, a gate, an exit code or "
         "the text above. " + STEP9, "",
         "Exit row = the log row at (t_actual, ca); a pool switch is an exit "
         "row whose `pool` differs from the entry row's (the definition in "
         "analysis/h0003-poolswitch/). Rows with a null pool are counted as "
         "unknown and kept in every recompute.", ""]
    for x in checks:
        L += [f"### {x['id']} (confirms {x['confirms']})", ""]
        if x.get("not_applicable"):
            L += [x["not_applicable"], ""]
            continue
        L += [f"- population rows: {x['population']}",
              f"- pool-switch rows: {len(x['switch'])} all statuses, "
              f"{x['switch_priced']} priced (ok/thin)",
              f"- unknown-pool rows (kept, not removed): {len(x['unknown'])}",
              f"- no_exit rows: {x['no_exit']}", "",
              "#### Pool-switch rows", ""]
        L += _row_table(x["switch"], x)
        L += ["#### Unknown-pool rows", ""]
        L += _row_table(x["unknown"], x)
        L += [f"#### Rows above {TAIL_NET:+.0%} net500", ""]
        L += _row_table(x["tail"], x)
        L += ["#### Clauses without the pool-switch rows", "",
              "`evaluate.decide_h0003` on the reduced population. Only "
              "population rows are removed; every remaining row keeps the "
              "baseline peers the evaluator gave it (baseline peers "
              "unchanged). Gates, censoring, fee sweep and the known-only "
              "re-run are printed on each set but do not decide the survive "
              "line: switch rows always have an exit row, so removing them "
              "mechanically raises the censored share. `[x]` = registered bar "
              "met.", "",
              "| set | decides survive line | rows | (a) | (b) | (c) | eff_n | "
              "tokens | censored % | fee sweep crosses | known-only | "
              "decide_h0003 |",
              "|---|---|---:|---|---|---|---|---|---|---|---|---|",
              _recompute_row("all rows, as evaluated", "reference",
                             x["full"]),
              _recompute_row("without all pool-switch rows", "**yes**",
                             x["without_switch"]),
              _recompute_row("without priced (ok/thin) switch rows only",
                             "no, report only", x["without_priced_switch"]),
              ""]
        if x["survives"] is None:
            L += [f"**Survive line:** status is `{x['status']}`, not pass; "
                  "the survive line does not apply (this check can only move "
                  "a pass to INCONCLUSIVE).", ""]
        else:
            L += [f"**Survive line:** the pass "
                  f"{'SURVIVES' if x['survives'] else 'DOES NOT SURVIVE'} "
                  "removing all pool-switch rows (clauses (a), (b) and (c) "
                  f"{'all still hold' if x['survives'] else 'do not all hold'}"
                  " on the reduced set)."
                  + ("" if x["survives"] else
                     " Per step 9 the person records INCONCLUSIVE (data "
                     "quality: exit print from another pool)."), ""]
    return "\n".join(L)


def read_day_section(lines, hyps, labels, log_rows, results):
    """The section's markdown, or one visible FAILED line: a failure here must
    never cost the report or change the exit code."""
    try:
        by_id = {r["id"]: r for r in results}
        checks = []
        for h in lines:
            r = by_id.get(h["id"], {})
            if h["confirms"] != READ_DAY_FOR:
                checks.append({"id": h["id"], "confirms": h["confirms"],
                               "not_applicable": "Not applicable: the "
                               f"read-day check is defined for {READ_DAY_FOR}"
                               " confirmations only."})
                continue
            checks.append(read_day_checks(h, hyps, labels, log_rows,
                                          r.get("status"),
                                          bool(r.get("stable"))))
        return render_read_day(checks)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"WARNING: read-day checks failed ({msg}); the report was "
              "written without them", file=sys.stderr)
        return f"\nRead-day checks FAILED: {msg}\n"


def main(a, now=None):
    if a.report is not None and not a.unblind:
        print(E.REPORT_NEEDS_UNBLIND, file=sys.stderr)
        return EXIT_REPORT_NEEDS_UNBLIND
    hyps = [json.loads(l) for l in open(E.REGISTRY) if l.strip()]
    lines = confirmations(hyps)
    if a.id:
        lines = [h for h in lines if h["id"] == a.id]
        if not lines:
            print(f"{a.id} is not a confirmation line in {E.REGISTRY}",
                  file=sys.stderr)
            return EXIT_BAD_REGISTRY
    if not lines:
        print(f"no confirmation lines in {E.REGISTRY}")
        return 0
    try:
        for h in lines:
            as_confirmed(h, hyps)
    except ValueError as e:
        print(f"{e}. Nothing was run.", file=sys.stderr)
        return EXIT_BAD_REGISTRY

    # the clock is checked before anything is loaded, so a refused run
    # computes nothing
    if a.unblind:
        t = (now or _now)()
        early = [h for h in lines if t < read_at(h)]
        if early:
            print("refused: " + "; ".join(
                f"{h['id']} may not be unblinded before "
                f"{read_at(h).isoformat()}" for h in early)
                + f" (now {t.isoformat()}). Nothing was run.", file=sys.stderr)
            return EXIT_BEFORE_READ

    rows, by_ca, times, pile_idx, nfiles = label.load_log()
    if not rows:
        print(f"no log rows under {label.LOG_DIR}/ -- nothing to evaluate")
        return 1
    labels, all_ts = E.prepare_labels(rows, by_ca, times, pile_idx, PILE)

    lv = label.labeler_version()
    registered = {h["labeler_version"] for h in lines}
    mismatch = None
    if lv not in registered:
        mismatch = (f"scripts/label.py is `{lv[:12]}`; the confirmation lines "
                    f"were written against "
                    f"{', '.join(sorted(v[:12] for v in registered))}. The "
                    "arithmetic has changed since registration.")

    results = [evaluate_confirm(h, hyps, labels) for h in lines]
    evaluator = E.version(os.path.abspath(E.__file__))
    wrapper = E.version(os.path.abspath(__file__))
    if mismatch:
        print(f"WARNING: {mismatch}\n")
    print(f"evaluator {evaluator[:12]}  wrapper {wrapper[:12]}  "
          f"labeler {lv[:12]}  log {len(rows)} rows  pile {PILE}")
    print("confirming: " + ", ".join(
        f"{h['id']} -> {h['confirms']} since {h['log_watermark'][:19]}Z"
        for h in lines))
    if not a.unblind:
        # worded without the words it withholds, so a test can grep for them
        print("blind run: counts and coverage only; --unblind prints the "
              "decision outputs")
        E.print_blind(results)
        return 0

    for r in results:
        g = r.get("worst", {})
        print(f"  {r['id']}  {r['status']:14s} "
              f"metric {E.pp(r.get('metric')):>7s}pp  "
              f"eff_n {g.get('eff_n', 0)}/{g.get('min_eff_n', '?')}  "
              f"tokens {g.get('tokens', 0)}/{g.get('min_n_tokens', '?')}")
    if a.report is not None:
        run_at = (now or _now)().isoformat()
        meta = {"run_at": run_at, "evaluator": evaluator, "labeler": lv,
                "rows": len(rows), "snaps": len(all_ts),
                "span": f"{min(all_ts)[:19]} .. {max(all_ts)[:19]}",
                "pile": PILE, "n_hyp": len(lines),
                "n_open": sum(1 for h in lines
                              if h["status"] in ("registered", "waiting")),
                "n_unstarted": 0, "mismatch": mismatch, "since_override": None,
                "counts": None, "by_id": {h["id"]: h for h in lines}}
        head = ["# Confirmation evaluation", "",
                f"Run by `scripts/evaluate_confirm.py` (`{wrapper[:12]}`), "
                f"which hands `scripts/evaluate.py` (`{evaluator[:12]}`) a copy "
                "of each confirmation line carrying the confirmed id, so the "
                "confirmed rule runs unchanged on the confirmation line's own "
                "watermark, gates and stop_by.", ""]
        head += [f"- {h['id']} confirms {h['confirms']}: since "
                 f"`{h['log_watermark']}`, read_on {h['read_on']}"
                 for h in lines]
        path = a.report or os.path.join(
            E.DEFAULT_REPORT_DIR,
            f"eval-confirm-{run_at[:10]}-{wrapper[:8]}.md")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "w").write("\n".join(head) + "\n\n" + E.render(results, meta)
                              + read_day_section(lines, hyps, labels, rows,
                                                 results))
        print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", default=None, metavar="H-NNNN",
                    help="evaluate one confirmation line only")
    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help=f"write a markdown report (default {E.DEFAULT_REPORT_DIR}/"
                         "eval-confirm-<date>-<wrapper>.md). Requires --unblind.")
    blind = ap.add_mutually_exclusive_group()
    blind.add_argument("--blind", action="store_true",
                       help="the default: rows, gates and week counts only")
    blind.add_argument("--unblind", action="store_true",
                       help="print metrics and statuses; refused before "
                            "read_on 07:00Z")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(E.version(os.path.abspath(__file__)))
    else:
        sys.exit(main(a))
