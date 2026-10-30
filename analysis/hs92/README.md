# HS-92 gate 3: power check for the HIT/STOP candidate

This is a report-only analysis of a candidate hypothesis that is NOT registered. Nothing here decides a hypothesis, a threshold or a sample size.

Run on commit f5504ab (collector data to 2026-10-30T21:48Z; clock = latest follow-up watch run, 2026-10-30T21:05:37Z):

    python analysis/hs92/power.py

It reads `data/followup/enrolled.json`, `data/log` (same files and the same first-kept `(ts, ca)` dedup as `scripts/label.py`) and `data/followup/candles/*.json`, and writes nothing. It imports `scripts/label.py` for the cost model only. The simulation is seeded (20261102, 10,000 draws per cell).

## Candidate (decided by the owner, not registered)

- Population: tokens in `enrolled.json` with `enrolled_at - first_seen <= 24 h`.
- Entry: the token's last data/log row with `enrolled_at - 30 min <= ts <= enrolled_at`. No row, or entry price not > 0: dropped and counted.
- Arms: A = entry tier `BEST`; B = every other tier.
- Window: entry ts to `first_seen + 7 d`, the end of the follow-up watch (so 6 to 7 days). Hourly candles that open at or after entry and close by the window end.
- HIT: a high >= 2.0 x entry price before a low <= 0.5 x entry price. STOP: the low first; one candle touching both is STOP. TIMEOUT: neither, window covered; exit at the last close at or before `first_seen + 7 d`. CENSORED: neither, window not covered.
- IMMATURE (`first_seen + 7 d` not yet passed, or no candle file yet) is excluded and counted, not censored.

## Sizing sample and guards

- Outcomes are computed only for tokens with `enrolled_at < 2026-10-28T17:30:26.797920Z` (H-0008's watermark). `outcome()` asserts it.
- One pooled outcome split per sample. No outcome is split by tier, liquidity or any other predictor. Arm sizes (tier at entry) are printed for the whole enrolled set.
- Wrong-token check, entry side only: entry log price / close of the last candle that closed at or before entry. Outside [0.5, 2] is dropped and counted.
- Coverage: the newest candle in the file closes at or after `first_seen + 7 d`, and the file's start reaches entry (fewer than 200 candles, or the oldest opens at or before entry). Missing hourly candles inside the window are counted, and treated as hours with no trades, not as censoring.
- The primary sample has fewer than 25 tokens, so a lag <= 96 h line is printed as a sensitivity only (window 3 to 7 days).

## Cost model (unchanged)

`scripts/label.py:121-127`, `nets()`: `gross - 2*fee - sum(2*size/liq for liq in (liq_in, liq_out) if liq > 0)`, floored at -100%. Size 500 USD (`label.py:36`). Unknown fee = `FEE_FALLBACK = 0.01` (`label.py:54`). c = `-nets(0, liq, liq, fee)`.

KNOWN APPROXIMATION: exit liquidity is unknown at entry, so `liq_out = liq_in`. A token that hits STOP has usually lost depth, so this understates the cost on stops.

Break-even hit share `b = (0.5 + median c) / 1.5` (HIT pays +100%, STOP -50%; TIMEOUT treated as zero).

## Power

Both arms drawn from the pooled four-way shares. Arm A n = 40..120, arm B = 4 x A. Censored tokens are excluded per draw. Pass = arm A hit share >= b AND > arm B hit share. Detection runs raise arm A's evaluable (non-censored) hit share by +5/+10/+15 pp, taking the extra from STOP and TIMEOUT in proportion; the censored share is unchanged.

## Findings

1. Arm A is structurally near empty in this population. `scripts/collect.py:69` `BEST_MIN_AGE = 2.0` (days of pool age) means a token enrolled within 24 h of first_seen is BEST at entry only if its pool was already 2 days old when first seen. Since 2026-10-24: 47 enrolments with lag <= 24 h, 0 BEST at entry. All 21 BEST-at-entry tokens are from the 2026-10-23 launch batch. As specified, no arm A n is ever reached.
2. The pass rule is dominated by b. b is about 35% while the pooled evaluable hit share is 0% (n = 8) or 12.5% (n = 19, sensitivity). Detection stays under 15% for every n and shift tested; a pass needs arm A's true hit share near b, not near arm B.
3. The sizing sample is tiny: 8 tokens (primary), 19 (sensitivity). The pooled shares feeding the simulation are therefore very uncertain.
4. 84 of 180 enrolled tokens have no data/log row in the 30 min before enrolment, mostly launch-batch tokens enrolled from older history; 9 of the 47 recent lag <= 24 h enrolments also have none.
5. The unit check is clean: median entry/candle price ratio 1.0012; 1 token (primary) / 2 (sensitivity) dropped outside [0.5, 2].

## Not verified

- GeckoTerminal's ohlcv currency and token side are not read from the API response; the ratio check is the only guard.
- Whether a missing hourly candle always means no trades.
- The pace dates (none computable) assume the 2026-10-24..30 rate holds.

## Full output

```
HS-92 power check, candidate NOT registered. Sizing sample: enrolled_at < 2026-10-28T17:30:26.797920+00:00 (H-0008 watermark), mature at the clock.
Cost model: label.py nets(), fee fallback 0.01, liq_out = liq_in, 500 USD.

=== SIZING SAMPLE: lag <= 24 h ===
  pre-watermark enrolments                          153
  excluded: lag > limit                             123
  dropped: entry/candle ratio outside [0.5, 2]        1
  immature (excluded, not censored)                  20
  dropped: no entry row                               1
  unit check: median entry/candle price ratio 1.0012 over 9 tokens
  tokens with an outcome: 8
  coverage: window covered 6, not covered 2; hour slots 1263, slots with no candle 164 ( 13.0%)
  window length: 145.1 to 166.9 h (first_seen + 7 d minus entry, approx.)
  POOLED OUTCOME SPLIT (never split by any predictor):
    HIT          0    0.0%
    STOP         2   25.0%
    TIMEOUT      5   62.5%
    CENSORED     1   12.5%
    evaluable hit share (censored excluded):   0.0%
  round-trip cost c at 500 USD: median   3.2%, IQR   2.9% to   8.8%
  break-even hit share b = (0.5 + 0.0318) / 1.5 =  35.5%
  POWER: pass = A hit share >= b AND > B hit share; censored excluded; B = 4x A; 10000 draws, seed 20261102
    arm A n          40      60      80     100     120
    false pass     0.0%    0.0%    0.0%    0.0%    0.0%
    detect + 5pp    0.0%    0.0%    0.0%    0.0%    0.0%
    detect +10pp    0.0%    0.0%    0.0%    0.0%    0.0%
    detect +15pp    0.1%    0.0%    0.0%    0.0%    0.0%

=== SENSITIVITY ONLY: lag <= 96 h (window 3 to 7 days) ===
  pre-watermark enrolments                          153
  excluded: lag > limit                              81
  dropped: no entry row                              29
  dropped: entry/candle ratio outside [0.5, 2]        2
  immature (excluded, not censored)                  22
  unit check: median entry/candle price ratio 1.0012 over 21 tokens
  tokens with an outcome: 19
  coverage: window covered 15, not covered 4; hour slots 2525, slots with no candle 307 ( 12.2%)
  window length: 95.3 to 166.9 h (first_seen + 7 d minus entry, approx.)
  POOLED OUTCOME SPLIT (never split by any predictor):
    HIT          2   10.5%
    STOP         3   15.8%
    TIMEOUT     11   57.9%
    CENSORED     3   15.8%
    evaluable hit share (censored excluded):  12.5%
  round-trip cost c at 500 USD: median   3.1%, IQR   2.7% to   3.8%
  break-even hit share b = (0.5 + 0.0309) / 1.5 =  35.4%
  POWER: pass = A hit share >= b AND > B hit share; censored excluded; B = 4x A; 10000 draws, seed 20261102
    arm A n          40      60      80     100     120
    false pass     0.0%    0.0%    0.0%    0.0%    0.0%
    detect + 5pp    0.7%    0.1%    0.0%    0.0%    0.0%
    detect +10pp    4.3%    1.7%    0.8%    0.4%    0.2%
    detect +15pp   15.0%   10.5%    7.3%    5.9%    4.1%

=== ARM SIZES at entry, whole enrolled set (180 tokens) ===
  tier WATCH        51
  tier BEST         21
  tier SPECULATIVE   13
  tier EMERGING     10
  tier AVOID         1
  no entry row      84
  arm A (BEST) 21, arm B (other) 75

=== PACE since 2026-10-24 (clock: latest watch run 2026-10-30T21:05:37.976919+00:00) ===
  enrolments 57 over 6.88 days = 8.29/day
  with lag <= 24 h: 47 (6.83/day)
  tiers at entry, all since: WATCH 30, EMERGING 9, SPECULATIVE 7; no entry row 11
  tiers at entry, lag <= 24 h: WATCH 24, EMERGING 8, SPECULATIVE 6; no entry row 9
  arm A share at entry (lag <= 24 h): 0; no arm A n is reached at this pace
```
