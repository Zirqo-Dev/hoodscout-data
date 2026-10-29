# HS-91 gate 3, option B: pooled DEAD_72H base rate (report only)

Run at 2026-10-28T17:24:29+00:00; latest watch run 2026-10-28T15:05:36+00:00. Pooled totals only: no outcome is split by entry liquidity, tier or anything else.
The candidate hypothesis is NOT registered. Nothing here decides anything.

## 1. Counts

- Enrolled (enrolled_at before this run): 153
- Entry matched (data/log row at or before enrolled_at, within 30 min): 77 (dropped, no entry row: 76)
- Dropped, entry liq not > 0: 1
- Dropped, enrolled_at - first_seen > 96 h: 34
- Dropped, entry + 72 h after the latest watch run (not yet mature): 16
- Eligible: 26
- Censored (fewer than 12 known points): 0
- Evaluable: 26
- Dead: 3

## 2. Pooled DEAD_72H rate

Pooled rate: 3 / 26 = 11.5% (95% Wilson 4.0% to 29.0%).
Deaths by clause (pooled): L only 2, P only 0, both 1, mixed L/P pair only 0.

## 3. Arm sizes only (eligible tokens; no outcomes)

Entry liq >= 50k: 19. Below 50k: 7. Share >= 50k: 73.1%.

## 4. Sizing (analytic)

Two-proportion normal approximation, alpha 0.05 one-sided, power 0.80. The two arms sit at p -/+ |gap|/2 around the centre p. n is EVALUABLE tokens per arm.

| centre p | gap -10 pp | gap -15 pp | gap -20 pp |
|---|---|---|---|
| pooled - 10 pp (1.5%) | n/a | n/a | n/a |
| pooled (11.5%) | 126 | 56 | 31 |
| pooled + 10 pp (21.5%) | 208 | 92 | 52 |

n/a: an arm rate would leave (0, 1).

## 5. Pace

New enrolments since 2026-10-24: 30 over 4.63 days = 6.48 per day. Share passing entry match, liq and 96 h rules: 83.3%. Pooled censored share among eligible: 0.0%.
Projection = evaluable today + per-day rate x pass share x (1 - censored share) x days from the latest watch run to (read date - 72 h), split by the item 3 share.

| read date | projected evaluable total | >= 50k | < 50k |
|---|---|---|---|
| 2026-12-07 (Mon) | 222 | 163 | 60 |
| 2026-12-14 (Mon) | 260 | 190 | 70 |
| 2027-01-11 (Mon) | 411 | 301 | 111 |

## 6. Suggestion for the owner (not a decision)

SUGGESTION (for the owner, not a decision): size for a -15 pp gap: min 92 evaluable tokens per arm, read on 2027-01-11; sized at the conservative centre 21.5% (pooled + 10 pp), where the one-sided 0.05 cutoff at that n is a gap <= -10.0 pp.
