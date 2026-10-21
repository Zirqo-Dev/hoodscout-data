#!/usr/bin/env python3
"""Blind labeler equivalence check for the read-day audit.

docs/EVIDENCE_LAYER.md, Layer 3d, step 3d: if scripts/label.py is no longer
the labeler H-0001..H-0003 were registered against, both versions are run on
the same rows and only the number of differences is printed.

The registered labeler is read from git by its blob hash and executed into a
module of its own; the current one is imported. The log is loaded once, with
the current module, and both versions label the same 6h rows after the
watermark with identical arguments, no thinning. Comparison is exact: no
tolerance, because a verdict-neutral change produces identical numbers.

BLIND BY DESIGN. Output is hashes and counts only -- never a return, a median
or a share. Exit code is the verdict: 0 when every diff count is zero, 1
otherwise, 2 when the check could not run.
"""
import ast, hashlib, importlib.util, os, subprocess, sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTERED_BLOB = "17e925eec190a3830969ccd9252266ec472f6ba1"
REGISTERED_COMMIT = "418b0c4"            # label.py there is that blob
WATERMARK = datetime.fromisoformat("2026-10-18T13:30:25.020204+00:00")
HORIZON = "6h"
PILE = "scored"
RETURN_FIELDS = ("enterable", "ret_gross", "ret_net_500", "ret_net_5000")

# What load_log() and peers() read. The data is built once with the current
# module, so if these differed the registered labeler would be judged on rows
# it would not have loaded itself: stop rather than compare.
SHARED = ("_open", "load_log", "peers",
          "LOG_DIR", "MIN_SCORE_TXNS", "DASHBOARD_TIERS", "DRAWS")

KEYS = ("rows_compared", "pending_skipped", "outcome_diffs_status",
        "outcome_diffs_returns", "baseline_compared", "baseline_diffs")


def git_blob(data):
    """git hash-object of some bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def registered_source():
    """The registered label.py, by blob hash, or from its commit if the blob
    is not reachable by hash alone."""
    for cmd in (["git", "cat-file", "-p", REGISTERED_BLOB],
                ["git", "show", f"{REGISTERED_COMMIT}:scripts/label.py"]):
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    return None


def module_from_source(src, name):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    return mod


def shared_defs(src):
    """ast dumps of the top-level SHARED functions and constants."""
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in SHARED:
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in SHARED:
                    out[t.id] = ast.dump(node.value)
    return out


def compare(reg, cur, rows, by_ca, times, pile_idx, latest):
    """Diff counts between two labeler modules over the same rows."""
    c = Counter({k: 0 for k in KEYS})
    reg_cache, cur_cache = {}, {}
    for e in rows:
        if e["dt"] <= WATERMARK:
            continue
        a = reg.outcome(e, HORIZON, by_ca, times, latest)
        b = cur.outcome(e, HORIZON, by_ca, times, latest)
        if a is None and b is None:
            c["pending_skipped"] += 1
            continue
        c["rows_compared"] += 1
        if a is None or b is None:
            c["outcome_diffs_status"] += 1      # pending on one side only
        else:
            c["outcome_diffs_status"] += a["status"] != b["status"]
            c["outcome_diffs_returns"] += any(a[k] != b[k] for k in RETURN_FIELDS)
        c["baseline_compared"] += 1
        ma = reg.baseline_med(e, HORIZON, PILE, pile_idx, by_ca, times, latest,
                              reg_cache)
        mb = cur.baseline_med(e, HORIZON, PILE, pile_idx, by_ca, times, latest,
                              cur_cache)
        c["baseline_diffs"] += ma != mb
    return c


def run(reg_src, cur_src, cur):
    """Compare reg_src against the loaded module cur (whose source is cur_src).
    Prints the report and returns the exit code."""
    a, b = shared_defs(reg_src), shared_defs(cur_src)
    differ = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    if differ:
        print(f"stop: log-loading helpers differ between versions: "
              f"{', '.join(differ)}", file=sys.stderr)
        return 2
    reg = module_from_source(reg_src, "label_registered")
    rows, by_ca, times, pile_idx, _ = cur.load_log()
    if not rows:
        print(f"stop: no log rows under {cur.LOG_DIR}/", file=sys.stderr)
        return 2
    latest = max(r["dt"] for r in rows)
    c = compare(reg, cur, rows, by_ca, times, pile_idx, latest)
    verdict = int(any(c[k] for k in KEYS if "diffs" in k))
    print(f"registered_blob: {git_blob(reg_src)[:12]}")
    print(f"current_blob: {git_blob(cur_src)[:12]}")
    for k in KEYS:
        print(f"{k}: {c[k]}")
    print(f"verdict: {verdict}")
    return verdict


def main():
    reg_src = registered_source()
    if reg_src is None:
        print("stop: registered labeler not found in git", file=sys.stderr)
        return 2
    if not git_blob(reg_src).startswith(REGISTERED_BLOB[:12]):
        print("stop: registered labeler source does not hash to "
              f"{REGISTERED_BLOB[:12]}", file=sys.stderr)
        return 2
    with open(label.__file__, "rb") as fh:
        cur_src = fh.read()
    return run(reg_src, cur_src, label)


if __name__ == "__main__":
    sys.exit(main())
