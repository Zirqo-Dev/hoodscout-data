#!/usr/bin/env python3
"""Weekly blind status: run the three blind evaluators, write their counts.

HS-107 + HS-121. Run by .github/workflows/blind-status.yml. Writes
status/blind_latest.txt (overwritten each run):

  1. an HS-121 block: for each running hypothesis, every predictor-side clause
     value that a blind output prints, next to its registered floor or cap.
     Values are copied from the blind outputs below, never computed here; a
     clause no blind output prints is written "not available". Floors are the
     numeric fields of data/hypotheses.jsonl, or numbers pulled out of its
     inconclusive_if text; no registry text is ever copied into the file.
  2. the UTC time and the HEAD commit.
  3. for each evaluator in COMMANDS, its exit code and full stdout.

COMMANDS is the whole list of what this script runs. Each runs in its default
blind mode; no other mode is reachable from here.

TRIPWIRE. No "%" and no "pp" (any case, inside words too) may reach the file.
Order per evaluator: run, then the one substitution below, then the tripwire.
An evaluator whose stdout still trips is written as "withheld: tripwire
(<script>)". The whole file is checked again before it is written. A trip
makes the script exit 3 AFTER the file is written; an evaluator's own
non-zero exit is recorded and never fails this script.

THE ONE SUBSTITUTION (owner decision D1, 2026-11-12): in the stdout of
scripts/evaluate_confirm.py only, the whole word "wrapper" becomes "wrap". Its
header names its own version as "wrapper <sha>", and that word contains the
forbidden letter pair. Nothing else is rewritten, in any output.

Standard library only. No network. Counts and fractions only.
"""
import json, os, re, subprocess, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "data", "hypotheses.jsonl")
OUT = os.path.join(ROOT, "status", "blind_latest.txt")
TIMEOUT_S = 1500

EV, CONFIRM, HOLDERS = ("scripts/evaluate.py", "scripts/evaluate_confirm.py",
                        "scripts/evaluate_holders.py")
# (script, arguments). The only commands this script ever runs.
COMMANDS = ((EV, ("--blind",)), (CONFIRM, ()), (HOLDERS, ()))

NA = "not available"
EXIT_TRIPPED = 3


# ------------------------------------------------------------ tripwire

def tripped(text):
    return "%" in text or "pp" in text.lower()


def substitute(script, out):
    """D1: the single hard-coded rewrite. Only evaluate_confirm.py's stdout."""
    if script == CONFIRM:
        return re.sub(r"\bwrapper\b", "wrap", out)
    return out


# ------------------------------------------------------------ running

def run_one(script):
    """Run one evaluator from COMMANDS; return (exit code as text, stdout,
    stderr line count)."""
    args = dict(COMMANDS)[script]
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, script), *args],
                           cwd=ROOT, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return f"timeout after {TIMEOUT_S} s", out, 0
    return str(p.returncode), p.stdout, len(p.stderr.splitlines())


def head_sha(root=ROOT):
    """HEAD's commit, read from .git directly so no extra command is run."""
    try:
        g = os.path.join(root, ".git")
        h = open(os.path.join(g, "HEAD")).read().strip()
        if not h.startswith("ref: "):
            return h
        ref = h[5:]
        p = os.path.join(g, ref)
        if os.path.exists(p):
            return open(p).read().strip()
        for line in open(os.path.join(g, "packed-refs")):
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return "unknown"


# ------------------------------------------------------------ registry

def load_registry(path=REGISTRY):
    hyps = {}
    for line in open(path):
        if line.strip():
            h = json.loads(line)
            hyps[h["id"]] = h      # a later line for an id wins
    return hyps


def _num(pattern, text):
    m = re.search(pattern, text or "")
    return int(m.group(1)) if m else None


def floors(hyps):
    """Numbers only. Text-derived ones come from H-0007's inconclusive_if."""
    out = {}
    for hid in ("H-0007", "H-0009", "H-0010"):
        h = hyps.get(hid, {})
        f = {k: h.get(k) for k in ("min_eff_n", "min_n_tokens",
                                   "max_censored_pct")}
        if hid == "H-0007":
            t = h.get("inconclusive_if", "")
            f["join_cov_pct"] = _num(r"fewer than (\d+)% of the population's "
                                     r"rows find", t)
            f["tie_pct"] = _num(r"more than (\d+)% of the top arm entered", t)
            f["snaps_week"] = _num(r"fewer than (\d+) counted snapshots fall",
                                   t)
        out[hid] = f
    return out


def share(pct):
    return NA if pct is None else f"{pct / 100:.2f}"


def val(v):
    return NA if v is None else str(v)


# ------------------------------------------------------------ HS-121 block

def _m(pattern, text):
    return re.search(pattern, text or "")


def block_h0007(out, f):
    L = ["H-0007 (from scripts/evaluate_holders.py):"]
    m = _m(r"join coverage (\d+)/(\d+) = ([\d.]+)", out)
    L.append("  join coverage: " + (f"{m.group(1)}/{m.group(2)} = {m.group(3)}"
                                    if m else NA)
             + f"  | floor {share(f['join_cov_pct'])}")
    m = _m(r"tie-break: second key (\d+), third key (\d+), of (\d+)", out)
    L.append("  top-arm tie-break entries: "
             + (f"second key {m.group(1)}, third key {m.group(2)}, of "
                f"{m.group(3)} top-arm rows" if m else NA)
             + f"  | cap {share(f['tie_pct'])} of the top arm")
    weeks = re.findall(r"(\d{4}-W\d{2}) \(\d+ rows, (\d+) counted snapshots\)",
                       out or "")
    L.append("  counted snapshots per selected week: "
             + (", ".join(f"{w} {n}" for w, n in weeks) if weeks else NA)
             + f"  | floor {val(f['snaps_week'])} per week")
    for arm in ("top", "rest"):
        m = _m(rf"arm {arm}\s+rows \d+\s+eff_n (\d+)/\d+\s+tokens (\d+)/\d+", out)
        L.append(f"  arm {arm} eff_n: {m.group(1) if m else NA}"
                 f"  | min_eff_n {val(f['min_eff_n'])}")
        L.append(f"  arm {arm} tokens: {m.group(2) if m else NA}"
                 f"  | min_n_tokens {val(f['min_n_tokens'])}")
    m = _m(r"gone_unseen share, both arms together ([\d.]+)", out)
    L.append(f"  gone_unseen share, both arms: {m.group(1) if m else NA}"
             f"  | max_censored {share(f['max_censored_pct'])}")
    L.append(f"  gone_unseen share, each arm: {NA}"
             f"  | max_censored {share(f['max_censored_pct'])}")
    return L


def block_h0009(out, f):
    L = ["H-0009 (from scripts/evaluate_confirm.py):"]
    m = _m(r"H-0009\s+rows \d+\s+eff_n (\d+)/\d+\s+tokens (\d+)/\d+", out)
    L.append(f"  eff_n: {m.group(1) if m else NA}"
             f"  | min_eff_n {val(f['min_eff_n'])}")
    L.append(f"  tokens: {m.group(2) if m else NA}"
             f"  | min_n_tokens {val(f['min_n_tokens'])}")
    m = _m(r"H-0009\s+rows[^\n]*\n\s+population rows \d+\s+gone_unseen share "
           r"([\d.]+)", out)
    L.append(f"  gone_unseen share: {m.group(1) if m else NA}"
             f"  | max_censored {share(f['max_censored_pct'])}")
    return L


def hs121_block(outs, fl):
    return (["HS-121 clause shares against registered floors and caps "
             "(counts and fractions only)", ""]
            + block_h0007(outs.get(HOLDERS), fl["H-0007"]) + [""]
            + block_h0009(outs.get(CONFIRM), fl["H-0009"]) + [""]
            + ["H-0010: evaluator not built (HS-91)"])


# ------------------------------------------------------------ main

def build(runner=run_one, hyps=None, now=None, sha=None):
    """Return (file text, tripped?)."""
    trip = False
    outs, sections = {}, []
    for script, args in COMMANDS:
        code, out, nerr = runner(script)
        out = substitute(script, out)
        if tripped(out):
            out, trip = f"withheld: tripwire ({script})\n", True
        outs[script] = out
        cmd = " ".join(("python", script) + tuple(args))
        sections += ["", f"===== {cmd}", f"exit code: {code}",
                     f"stderr: {nerr} lines (not shown)", "----- stdout",
                     out.rstrip("\n")]
    fl = floors(hyps if hyps is not None else load_registry())
    t = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = hs121_block(outs, fl) + ["", f"generated: {t}",
                                    f"HEAD: {sha or head_sha()}"]
    text = "\n".join(head + sections) + "\n"
    if tripped(text):
        trip = True
        text = (f"generated: {t}\nHEAD: {sha or head_sha()}\n"
                "withheld: tripwire (whole file)\n")
    return text, trip


def main(out_path=OUT, **kw):
    text, trip = build(**kw)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(out_path, ROOT)}"
          + ("; tripwire hit, exit 3" if trip else ""))
    return EXIT_TRIPPED if trip else 0


if __name__ == "__main__":
    sys.exit(main())
