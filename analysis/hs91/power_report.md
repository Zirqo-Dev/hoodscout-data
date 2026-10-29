# HS-91 gate 3: power check for the DEAD_72H candidate (report only)

Source: data/history.jsonl at commit d9f8047 (2026-09-04 to 2026-10-02). Every row asserted before 2026-10-18T13:30:25+00:00.
The candidate hypothesis is NOT registered. Nothing here decides anything.

Tokens reaching 4 distinct snapshots: 491. Not enterable (entry liq or price <= 0): 2. Pending at 72 h (grid runs past the last snapshot): 38.

## 1. Tokens per arm (floor 50,000 USD), censoring, enrolment lag

| arm | tokens (72 h window closed) | censored 72 h | evaluable 72 h | censored 24 h | lag median h | lag p25 h | lag p75 h | lag p90 h | lag max h |
|---|---|---|---|---|---|---|---|---|---|
| >= 50k | 254 | 156 (61.4%) | 98 | 60 (22.5%) | 1.53 | 1.47 | 2.98 | 12.10 | 160.95 |
| < 50k | 197 | 178 (90.4%) | 19 | 75 (35.5%) | 1.54 | 1.49 | 5.46 | 12.53 | 242.49 |

Enrolment lag = entry (4th distinct snapshot) minus the token's first snapshot.
Known grid points of 24 at 72 h, >= 50k: median 9, p25 6, p75 15.
Known grid points of 24 at 72 h, < 50k: median 6, p25 3, p75 7.
Censored tokens that nonetheless show two consecutive dead known points: 10 of 334 (3.0%). History only lists tokens while they are on the collector's lists, so a dying token tends to stop printing; censoring likely removes deaths, not a random sample.

## 2. Base DEAD rate per arm (pre-watermark backtest)

| floor | role | arm | DEAD 24 h (n evaluable) | DEAD 72 h (n evaluable) | gap 24 h pp | gap 72 h pp |
|---|---|---|---|---|---|---|
| 50k | candidate | >= 50k | 4.3% (207) | 8.2% (98) | +2.1 | +8.2 |
| 50k | candidate | < 50k | 2.2% (136) | 0.0% (19) |  |  |
| 25k | sensitivity only | >= 25k | 3.8% (292) | 6.9% (116) | +1.8 | +6.9 |
| 25k | sensitivity only | < 25k | 2.0% (51) | 0.0% (1) |  |  |
| 100k | sensitivity only | >= 100k | 2.9% (137) | 9.5% (74) | -1.0 | +7.1 |
| 100k | sensitivity only | < 100k | 3.9% (206) | 2.3% (43) |  |  |

## 3. Ceiling check (72 h, 50k floor)

No ceiling: both arms do NOT die above 80% at 72 h (>= 50k: 8.2%, < 50k: 0.0%).

## 4. Simulation (bootstrap by token, 72 h, 50k floor)

Resampling pool: evaluable (uncensored) tokens, 98 in >= 50k and 19 in < 50k. Observed gap +8.2 pp. 5000 reps per cell, seed 20261028. n counts EVALUABLE tokens per arm; censored tokens are not in the pool, so enrolment must be larger by the censored share.
(a) no true gap: both arms drawn from the pooled tokens (labels shuffled). (b) observed gap: each arm drawn from its own tokens, so (b) assumes the in-sample gap is the truth, which flatters detection.

WARNING: the < 50k pool has 19 evaluable tokens and 0 deaths. Resampling it at n = 100+ re-uses the same few tokens; its spread in (b) is understated, and with 0 or all deaths it has none at all.

| n per arm | threshold pp | false alarm (a) | detection (b) | (b) gap p5 / median / p95 pp |
|---|---|---|---|---|
| 100 | -10 | 0.2% | 0.0% | +4.0 / +8.0 / +13.0 |
| 100 | -15 | 0.0% | 0.0% | +4.0 / +8.0 / +13.0 |
| 100 | -20 | 0.0% | 0.0% | +4.0 / +8.0 / +13.0 |
| 140 | -10 | 0.1% | 0.0% | +4.3 / +7.9 / +12.1 |
| 140 | -15 | 0.0% | 0.0% | +4.3 / +7.9 / +12.1 |
| 140 | -20 | 0.0% | 0.0% | +4.3 / +7.9 / +12.1 |
| 200 | -10 | 0.0% | 0.0% | +5.0 / +8.0 / +11.5 |
| 200 | -15 | 0.0% | 0.0% | +5.0 / +8.0 / +11.5 |
| 200 | -20 | 0.0% | 0.0% | +5.0 / +8.0 / +11.5 |

## 5. Suggestion for the owner (not a decision)

Rule used: smallest n with false alarm <= 5% and detection >= 80%; ties go to the stricter (more negative) threshold.
SUGGESTION (for the owner, not a decision): do not set a threshold or min n from this data. The in-sample gap is +8.2 pp, above every tested threshold (the >= 50k arm dies MORE, not less), the < 50k arm has only 19 evaluable tokens, and most tokens are censored because history stops printing them. History alone cannot size this candidate.
