# HS-91 gate 3: power check for the DEAD_72H candidate

This is a report-only analysis of a candidate hypothesis that is NOT registered. Nothing here decides a hypothesis, a threshold or a sample size. The result is in `power_report.md`.

Data: `data/history.jsonl` as of commit d9f8047 (2026-09-04 to 2026-10-02). That is pre-watermark data, already used in the in-sample backtest. `power.py` reads it from a file path, as `analysis/hs77/build_pool.py` does, and asserts that every row's ts is before the 2026-10-18T13:30:25Z watermark. Nothing under data/log, data/followup, data/holders or the current history.jsonl is read.

Reproduce:

    git show d9f8047:data/history.jsonl > /tmp/history_sep.jsonl
    python analysis/hs91/power.py /tmp/history_sep.jsonl analysis/hs91/power_report.md

Standard library only. The simulation is seeded (20261028, 5000 reps per cell), so the same Python version gives identical output.

## What the script does

- Entry is a token's 4th distinct snapshot ts, mirroring `scripts/followup.py` MIN_SNAPSHOTS = 4. Duplicate rows at one ts count once. Entry liq and entry price must be > 0.
- The grid is entry + 3 h, + 6 h, ... + 72 h (24 points). Each point takes the nearest print within +/-45 min; ties go to the earlier print. A point with no print is unknown.
- A print is dead if liq < max(1000, 0.10 x entry liq) or price <= 0.10 x entry price. A missing liq counts as 0, as in build_pool.py. A missing price cannot trigger the price leg.
- DEAD needs two consecutive known dead points; an unknown point between them breaks the run. A token with fewer than 12 of 24 known points is censored, whether or not it shows a death.
- The 24 h rate is this script's own cut: the first 8 points, censored below 4 known. It is not part of the candidate.
- Tokens whose grid runs past the last snapshot in the file are "pending" and left out.
- The simulation counts n as evaluable (uncensored) tokens per arm. (a) draws both arms from the pooled tokens, so the true gap is zero. (b) draws each arm from its own tokens, so it assumes the in-sample gap is the truth.

## Not simulated

- The migration clause (data/log must agree) cannot be simulated here. history.jsonl has one liq series per token, so there is nothing to disagree with.
- Censoring is not random in this file. history.jsonl only lists a token while it is on the collector's lists, so a dying token tends to stop printing and lands in "censored". The death rates here are mostly survivors' rates, and the report says how many censored tokens show a death signal anyway.

## Option B: one pooled base rate (`pooled_rate.py`)

Report-only. The result is in `pooled_rate.md`. It prints pooled totals only: no outcome is split, grouped or sorted by entry liquidity, tier or anything else, and no per-token row appears. The only per-arm figures are arm sizes. This keeps a future liquidity-split hypothesis blind.

Reads `data/followup/enrolled.json`, `data/followup/watch/`, and only ts, ca, liq and price from `data/log` for enrolled tokens. Never data/holders.

    python analysis/hs91/pooled_rate.py analysis/hs91/pooled_rate.md

The output depends on the run time (enrolled_at before now) and on the data at that commit.

Choices made by the script, beyond the rule in its docstring:
- Watch points are the token's watch rows for its frozen main_pool, deduplicated on ts. A run that did not return the pool leaves no row. An unknown row (reserve not numeric) cannot be dead and breaks a run of two.
- A missing or non-numeric price cannot trigger (P).
- Deaths by clause: "L only" = only an L-L pair exists, "P only" = only a P-P pair, "both" = both exist, "mixed" = the only dead pairs mix one L point and one P point.
- Sizing puts the arms at p -/+ |gap|/2 around a centre p. The suggestion uses the conservative centre (pooled + 10 pp), because the pooled rate rests on few deaths.
- Pace projection: per-day enrolments since 2026-10-24 x the share of those passing the entry, liq and 96 h rules x (1 - pooled censored share), counted up to read date - 72 h, split by the eligible arm-size share.
