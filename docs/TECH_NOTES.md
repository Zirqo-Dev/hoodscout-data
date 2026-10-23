# Technical notes

Measured analyses and design rationale. Tasks and status live on the Notion
HoodScout Board (card IDs HS-n); nothing here is a task list.

## `nft.py` `get()` has no 429 backoff

`post()` (key minting) backs off; `get()` — every data call, including the
~41 discovery makes — does not. Latent: runs that fail do so at `mint_key()`
before any GET executes. Same gap `collect.py` and `stocks.py` had before
30d36e7 / ec018e8.

## `history.jsonl` is full-rewritten every run

`write_history()` reads the file, prunes rows past `HISTORY_DAYS`, appends the
new snapshot and writes the whole thing back with `open(path, "w")`. So each
run commits a fresh ~21 MB blob to git for ~25 KB of genuinely new rows. At
the `*/15` cadence that is up to ~96 full rewrites a day. Should become an
append plus a periodic prune, or a rolling set of per-period files, so git
stores the delta rather than a new whole-file blob each time.

## Revisit the cadence-coupled constants after a week

`buyer_rate_rising`'s `window` (5) counts snapshots, not time, so its
wall-clock meaning depends on how many scheduled runs GitHub actually
delivers. It was set assuming `*/15` delivers ~96/day, but observed delivery
at `*/30` was 22.9 snapshots/day against a nominal 48 — under half, and the
2026-10-18 measurement below is worse still. If `*/15` lands nearer 46/day
than 96, the window is roughly 2x too short in time terms. Same for
`HISTORY_DAYS = 15`, which was halved to hold file size flat on the
assumption of doubled delivery.

`window` has a second, sharper problem, independent of the cadence: it
returns `False` only below 2 rows, so with 2 rows it tests one increase
instead of four. Measured pass rate is 40.0% at 2 rows, 24.7% at 3, 17.5% at
4 and 13.6% at 5 — a thin history makes the EMERGING gate **2.9x easier**,
not harder. Blast radius is small (the `MIN_SCORE_TXNS` gate keeps the
eligible pool at ~11 tokens, so about +3 entries right after a prune and
~14 false EMERGING token-snapshots a day in steady state), but the fix is to
require the full window or scale the bar, not to leave it reading as a
4-increase test.

`PERSIST_RUNS`/`PERSIST_MIN` (16/10) were the worst case of this and are
retired as of `2d6a4fb`; they are kept defined for one week so the old gate
can be recomputed beside the time-based one.

**Done 2026-10-18: `PERSIST_MIN_OBS` raised 2 -> 8.** The time-based gate
replacing them has its own thin-window hole. `need` is
`max(PERSIST_MIN_OBS, ceil(PERSIST_MIN_FRAC * snapshots_in_window))`, so after
a delivery gap leaves two snapshots five hours apart the fraction asks for
two — and a token seen at both ends of the gap and nowhere in between clears a
"healthy across 4h" bar on two observations. Eight is about two hours of real
prints at four runs an hour. Measured on the live file at 2026-10-18T09:00Z
(25 snapshots, 20 inside the window, `need` = 12 either way): identical sets
at 2 and at 8, 41 tokens each. On a window cut to two snapshots 4.68h apart:
40 tokens pass at 2, none at 8. So it is inert at a healthy cadence and only
bites after an outage, which is the intent.

Still open in the same area: `buyer_rate_rising`'s `window` has the mirror of
this bug and no floor at all.

## Scheduler delivery — off-peak minutes did not work, plan B in progress

GitHub dropped ~9 collect fires and the 21:00 NFT run on 2026-10-17;
off-peak minutes tried; external dispatch is plan B. Re-measure after 24 h:
delivered runs per day vs nominal.

**Measured 2026-10-18.** The off-peak move (`7162f6b`, crons to
`7,22,37,52` and `17 13,21`) did not help. Between the 21:56 and 23:58
collect runs, eight fires were nominal — 22:07, 22:22, 22:37, 22:52, 23:07,
23:22, 23:37, 23:52 — and **one was delivered**: Actions run 1358, which
started at 23:58:05 against the 23:52 cron, six minutes late. Nothing fired
between 23:58 and 04:03. So 1 of 8, and peak-minute contention is ruled out
as the explanation rather than merely unproven.

Plan B is therefore in progress: an external scheduler calling
`workflow_dispatch`, which has fired reliably every time it was used this
session while schedules did not. Until it lands, the pipeline's effective
cadence is unpredictable and anything measured in snapshots is unsafe — which
is what `2d6a4fb` moved persistence off.

Consequence worth watching: the 2 h gap let `new_pools` accumulate two hours
of mints into one snapshot, and the 23:58 run took in 51 tokens it had never
seen, 49 of them a single zero-liquidity batch mint (tickers `5`..`53`, one
contract and one pool each, FDV climbing monotonically with the number). DEAD
went 25 -> 57 on composition alone; nothing was demoted into it.

## The `Commit` step cannot resolve `data/` conflicts — mitigated, not fixed

`git pull --rebase --autostash` in each workflow's Commit step fails on a
content conflict in the data files rather than resolving it (seen on
collect.yml run 665, 2026-09-17, caused by pushing code while runs were in
flight). Exposure is lower since `stocks.yml` stopped being a scheduled
writer, but `collect.yml` and `nft.yml` can still collide.

**Fired for real: collect.yml run #1360, 2026-10-18T04:20Z.** Both scripts
completed cleanly; the step that failed was `Commit`:

```
[main 8826a61] data: 2026-10-18T04:25Z
 5 files changed, 10355 insertions(+), 3809 deletions(-)
 ! [rejected]        main -> main (fetch first)
push rejected, rebasing (attempt 1)
   171c9f6..ad86cd4  main       -> origin/main
CONFLICT (add/add): Merge conflict in data/daily/2026-10-18.json
CONFLICT (content): Merge conflict in data/history.jsonl
CONFLICT (content): Merge conflict in data/latest.json
CONFLICT (content): Merge conflict in data/stocks.jsonl
CONFLICT (content): Merge conflict in data/stocks_latest.json
error: could not apply 8826a61... data: 2026-10-18T04:25Z
##[error]Process completed with exit code 1.
```

It was a collision: the scheduled run was created 51s after a manual
dispatch, queued behind it on the concurrency group, and ran on the checkout
pinned to the pre-push SHA. The `10355 insertions / 3809 deletions` is the
tell — a 15-minute delta should be about fifty rows, and is that size only
because `write_history()` rewrites the file whole.

**Mitigated** by `ref: main` on `actions/checkout` in all three workflows: a
queued run now takes main's tip when its job starts rather than the SHA it
was triggered on, so the stale-checkout path that caused #1360 is closed.

**Not fixed.** The retry loop still cannot resolve a whole-file conflict, so
two runs that genuinely overlap in time — both fetch, both rewrite, second
pushes — still fail exactly as above. The retry loop was written for a
fast-forwardable race and whole-file rewrites are never fast-forwardable.
The real repairs are either a merge strategy that knows the newer snapshot
always wins (`-X theirs`, or re-running the collector after a pull instead of
rebasing its commit), or the append-based `history.jsonl` design in the entry
above, which would remove the conflict rather than resolve it.

## Evidence layer: forward-return baseline by tier

Baseline for anything that later claims a tier predicts direction. Method:
for each token-snapshot, the same token's nearest price print to 6 h inside a
4-8 h window; recovered pre-prune `history.jsonl`, 2026-09-17..10-17, 43,365
rows, 686 snapshots. `eff. n` collapses (token, 6 h-bucket) cells, since
overlapping windows inflate raw n about 5x. `token-median` is the median of
per-token medians, which is what survives clustering.

| tier | rows | retained | n | tokens | eff. n | median | mean | win% | token-median |
|---|---|---|---|---|---|---|---|---|---|
| BEST | 12,805 | 94.6% | 12,115 | 85 | 1,270 | −0.376% | −0.307% | 43.5% | −2.732% |
| EMERGING | 1,775 | 93.9% | 1,667 | 111 | 310 | −7.457% | +1.547% | 38.5% | −6.316% |
| WATCH | 25,873 | 87.4% | 22,615 | 270 | 2,610 | −0.564% | +0.993% | 41.9% | −1.430% |
| SPECULATIVE | 2,489 | 51.1% | 1,273 | 50 | 182 | +0.000% | −0.439% | 28.3% | −0.377% |
| DEAD | 108 | 0.0% | — | — | — | — | — | — | — |
| AVOID | 315 | 90.2% | 284 | 1 | 29 | −0.196% | −0.754% | 43.0% | −0.196% |

What it establishes:

- **BEST vs WATCH is a null, and the direction depends on the statistic.**
  Pooled, BEST leads by 0.19pp; clustered by token, WATCH leads by 1.30pp;
  paired within token (72 tokens with >=5 prints in each tier) BEST is better
  for 30 and worse for 42, median gap −0.49pp; by week the sign flips
  (−0.26, +0.48, +0.18pp). Treat "BEST beats WATCH" and its inverse as both
  unsupported. The dashboard legend quotes the paired figures.
- **EMERGING is lottery-shaped and that is not an artifact.** Median −7.46%
  against a mean of +1.55%: a few large winners against many losers. Negative
  on 11 of 15 days, 111 distinct tokens, and the heaviest contributors split
  both ways (HOODIE +17.4% over 79 prints, BEORN −28.6% over 40). Not one day
  and not one token.
- **DEAD retention is 0% by construction** — a token is DEAD partly because it
  stopped printing, so it can never have a forward price. SPECULATIVE at 51.1%
  is the only bucket where attrition could plausibly move the number.

Separately, on `recent_move_score` over the same window: the price half
(`_band(chg_h6)`) is monotone mean reversion (+5 band -> −3.96% median
forward, −5 band -> −0.69%) and survives pairing, clustering and a retention
control, but **not regime** — the gap is +0.06pp in W38 against −1.13 and
−1.44pp in W39/W40. The acceleration half is a confirmed null: paired within
token and within price band, accel-positive is better in 145 cells and worse
in 145, median gap +0.016pp. Do not build a directional flag on the
acceleration term.

## cron-job.org is the only scheduler, and a single point of failure

GitHub's `schedule:` was removed from `collect.yml` on 2026-10-18. It is
**not** paused or commented out — it is gone, and the file says how to put it
back. `nft.yml` keeps its own `schedule:` (`17 13,21 * * *`) unchanged;
`stocks.yml` has never had one.

Collection now depends entirely on cron-job.org calling `workflow_dispatch`
at :00/:15/:30/:45 UTC. Nothing else will start a collect run. If that job
stops, the pipeline stops silently: `latest.json` simply stops advancing, and
the only visible symptom is the dashboard's freshness chips going amber and
then red, plus PERSISTENT emptying out as the 6h persistence window drains.
There is no alert for "the scheduler died".

Two known expiries to watch:

- **The cron-job.org token expires in 90 days, around 2027-01-16.** When it
  does, dispatches start returning 401 and collection stops. cron-job.org
  emails on failure, so the first signal will be that email rather than
  anything in this repo.
- Failure notification is email only, to whoever owns the cron-job.org
  account. Nothing routes it into the repo or the dashboard.

Worth doing at some point: a staleness check that opens an issue when
`latest.json`'s `generated_at` falls more than an hour behind, so the repo
notices its own scheduler dying without depending on someone reading email.
That cannot live in `collect.yml` — the whole point is that it has to run
when collect does not.

## Nightly health brief (thin version)

Medium priority, not urgent. A free `scripts/brief.py` plus a nightly
workflow dispatched by cron-job.org, writing `data/brief/YYYY-MM-DD.md`.
Numbers only, computed from files already in the repo — **no LLM**.

Sections:

- **health** — delivery count for the day, gaps in it, freshness of each feed
- **what changed** — tier moves and new tokens, from `data/changes.json`
- **evidence status** — labeled row counts, `eff_n`, and how far each
  hypothesis in `data/hypotheses.jsonl` still is from its own `min_eff_n`
  and `min_n_tokens`
- **odd things** — anomalies worth a look
- **needs-you** — the cron-job.org token renewal around **2027-01-16** (see
  the scheduler entry above), hypotheses that have become decidable, stale
  feeds, and the labeler-version mismatch banner `evaluate.py` already
  raises when `scripts/label.py` no longer matches what the registry was
  written against

The rule that makes it safe: **the brief reports only.** It never edits
code, constants, thresholds or hypotheses. Nothing it writes is read back by
the collector.

Start it after the first real 6 h and 24 h labels exist — roughly a week of
collection — so it has something to say on day one. A smaller health-only
version (delivery gaps, stale feeds, expiring tokens) could ship sooner if
the wait is annoying.

## Brief proposals file — optional, ships with the brief

The brief may append suggestions to `data/proposals.md`, for example
"consider registering H-0004: <rule>". **A human approves; Claude Code then
registers the hypothesis.** Nothing is auto-adopted, and the proposals file
is never an input to anything that runs.

This exists so that noticing a pattern and acting on one stay separate
steps, which is the whole point of registering hypotheses before the data
that tests them.

## AI narrator for the brief — optional, later

Only worth doing if reading the plain brief starts to feel like a chore.
Needs an Anthropic API key as a repo secret and **separate billing** from
whatever runs Claude Code; estimated **$2-9/month, unverified** — the figure
has not been checked against real token counts for this brief's size. A
console spend limit is a precondition, not a nicety.

Same constraint as the brief itself: it **describes the numbers only, never
decides**. If it ever reads as advice, it is wrong.

## Paper trading (Layer 5)

**Blocked until H-0001..H-0003 resolve.** Not started, and should not be.

`docs/EVIDENCE_LAYER.md` currently defines Layers 2, 3a, 3b and 3c; there is
no Layer 4 or 5 written down yet, so the number is a placeholder for "after
the evidence layer returns verdicts".

When it does start, the rules are fixed **in advance**: entry, exit, size,
and fees taken from the fee model in `scripts/label.py` rather than assumed.
**No self-tuning** — constants and thresholds never change automatically.

The reason is not caution for its own sake. Retuning nightly on a small
sample is multiple testing: with enough passes something always looks
profitable, and the pre-registered evidence layer exists precisely to stop
that. A paper trader that adjusts itself defeats the thing it is supposed to
be tested by.

## Standing note: no self-adapting algo

A "self-adapting algo" is **explicitly rejected** for this project. It is not
a thing to get to later; it is the thing this design is built against.

Learning happens one way: a hypothesis is written down before the data that
tests it, Pete approves it, and it is registered in `data/hypotheses.jsonl`
with its own pass, fail and inconclusive conditions. Anything that changes a
threshold without passing through that step is a bug, not a feature.

## Deferred: DexScreener cross-check, boosts, DexPaprika

Three adjacent data sources, none of them adopted. The cross-check and the
backup are deferred for the same reason; boosts is deferred with a rule
attached.

- **DexScreener as a price/liquidity cross-check** — deferred. It would be a
  second opinion on numbers GeckoTerminal already gives us, and the evidence
  layer's hypotheses are registered against the GeckoTerminal series. Swapping
  or blending sources mid-stream changes what the registered numbers mean.
  Worth doing as a *separate* logged series if ever, never as a replacement.
- **DexScreener boosts as a "promoted" flag** — **deferred to the Layer 2
  go/no-go**, and this is the one with a rule attached. Boosts are paid
  placement, so a flag derived from them is a flag for "someone spent money on
  visibility". Used **as a predictor** that contaminates a forward-return test:
  it correlates with promotion campaigns, and a promotion campaign is a cause
  of the price move being measured, not an independent observation of it. A
  hypothesis built on it that way would be untestable rather than merely weak.
  Recording it is a different act from acting on it, so the rule is precise:
  boosts may be **logged as a separate raw annotation** — for example, to
  separate paid attention from organic attention in Layer 2 — and must **never
  feed tier logic or act as a signal** unless a pre-registered hypothesis that
  Pete approved says so. The risk is using the flag as a predictor, not writing
  it down.
  Decided 2026-10-19 (HS-48): deferred, log-only.
- **DexPaprika as a backup source** — deferred for the same reason as the
  cross-check, plus an unverified cost and coverage story for Robinhood Chain.
  A backup that returns slightly different numbers is not a backup.

## Multi-chain config — deferred

Everything is hard-coded to Robinhood Chain: `NETWORK = "robinhood"` in
`collect.py`, `CHAIN = "robinhood"` in `nft.py`, `CHAIN_ID = "4663"` in
`holders.py`, and the chain's own RPC in `stocks.py`. Generalising means a
chain config object and a per-chain data directory, and it buys nothing until
there is a second chain worth watching. Recorded so that the next person
adding a constant knows there are already four places to change.

## Evaluator join for H-0005

This replaces the H-0004 join entry, which has been deleted: H-0004 was
superseded by H-0005 on the day it was registered, before any outcome data was
joined to it. The work is the same shape with three changes, all of which came
from the first holders run rather than from taste:

- the denominator is `total_supply` from `data/holders/token_meta.json`, not
  the stored top 20. 63% of tokens had 10 or fewer surviving holders, which
  pins their top-20 concentration at exactly 1.0 and puts the quartile cutoff
  inside a tie block of 63 out of 100
- the join window is **3.5 hours**, as-of and never forward in time. At a
  3-hour cadence a 90-minute window can serve at best half the label rows
- the quartile split needs the **deterministic tie-break** H-0005 registers —
  conc10 desc, then `total_supply` desc, then `ca` asc — and the report has to
  state how many of the top arm were decided by the second or third key

Still true, unchanged from the H-0004 design: an as-of join must never look
forward, `label.py` needs no change, and this gets built when there are
snapshots to join against.

`data/hypotheses.jsonl` H-0005 notes refer to `BACKLOG.md`; that now means
this file.

## Candidate: creator-wallet concentration — NOT registered

The first holders run turned up something sharper than the hypothesis it was
collected for. 119 tokens, **44 distinct `creator_address_hash` values, one
of them responsible for 43 of them**. That address is a factory, which is why
the collector now resolves the creation transaction's sender into
`creator_eoa`.

The obvious question is whether tokens from a wallet that has launched many
tokens behave differently from one-offs. It is **deliberately not registered
yet**, for two reasons. There is no `creator_eoa` data at all until the
backfill lands, so a population cannot be defined. And the shape of the
distribution is the thing that would suggest the cut, which is precisely the
circumstance where writing the hypothesis first matters most.

When it is registered it needs: a launch-count-per-wallet feature computed
as-of the snapshot (never counting launches that had not happened yet, which
is the easy way to leak the future into it), a relative split as in H-0005,
and the same bar and gates. One hypothesis, written before the first look at
returns by launch count.

## Measure data/holders growth after 7 days, then choose

The first snapshot is 236 KB. At 8 runs a day that projects to ~1.9 MB/day,
~58 MB/month, ~0.7 GB/year, in a public repo that already has a
`history.jsonl` size entry above. **Do not act on the projection.** It
assumes a steady 119 tokens at 20 holders each, and the board turns over.

Measure the real thing after 7 days — `git count-objects -vH` before and
after, plus `du -sh data/holders` — and only then pick from: per-day
compaction into one file, dropping holders past the top 10, or a rolling
window that keeps recent snapshots whole and older ones summarised. Each
trades something different away, and the measurement decides which trade is
worth making. Note that compaction interacts with the as-of join: anything
that rewrites older snapshots changes what a past evaluation would have seen.

## Follow-up watch (HS-95)

Purpose: keep observing tokens for 7 days after we first see them, so a
future hypothesis is not blind to tokens that fall off our lists. Raw data
only; `scripts/followup.py` computes no return, gap or outcome, and reads
`data/history.jsonl` for `ts`, `ca` and `main_pool` only.

- Population: a token is enrolled once it has 4+ distinct snapshot `ts`
  (decided by Pete 2026-10-23) and its first_seen is within the last 7
  days. `data/followup/enrolled.json` records ca, main_pool, first_seen and
  enrolled_at; entries are frozen once written. The first run enrolled
  tokens already mid-window, which their enrolled_at shows.
- Schedule: `followup.yml`, workflow_dispatch only, started by cron-job.org
  at `5 */3 * * *` UTC. No GitHub schedule.
- Files: `watch/YYYY-MM-DD/HHMMSS.jsonl.gz` (multi-pool attributes verbatim
  plus ca, pool, run ts, one line per token), `watch/.../HHMMSS.meta.json`
  (process counts and pools not returned), `candles/<pool>.json` (hourly
  OHLCV up to first_seen + 7 d + 1 h, verbatim, fetched once per pool).
- Rate: 6 s between calls, 30 calls per run including the retry, watch
  first and backfill (at most 8, oldest first) with what is left. The first
  429 of a run waits 30 s and retries once; another 429 stops the run's calls.
  A failed backfill is due again next run, never retried in a loop.

data/followup is exploratory until a hypothesis that uses it is registered; nothing reads it into a report before then.
