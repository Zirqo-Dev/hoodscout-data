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
        open(path, "w").write("\n".join(head) + "\n\n" + E.render(results, meta))
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
