# Evidence layer

Layer 3 (outcome log, labeler, evaluator) is built and running; Layers 2, 4
and 5 are design only. The order is deliberate: **Layer 3 first**, because
without an outcome log there is no way to tell whether Layer 2 helped,
and a signal you cannot score is a signal you will end up trusting on vibes.

Everything in this document is a proposal to be argued with. Where a number is
measured it says so and gives the measurement; where it is an estimate it says
that too.

---

## Why this order

The forward-return baseline in `docs/TECH_NOTES.md` is the whole argument. Over 43,365
rows, tier did not separate forward returns: BEST beat WATCH on the pooled
median by 0.19pp and lost to it on the token-clustered median by 1.30pp, with
the sign flipping across three consecutive weeks. `screen_score` had already
failed the same test. `momentum_score` was renamed to `recent_move_score`
because its name claimed something the data did not support.

Three scores have now been built and talked about before anything could score
them. The evidence layer exists so the fourth one cannot be.

---

## Layer 3a — the snapshot log

One row per token per collect run. Write-only, never read by the collector,
never rewritten.

### Fields

```json
{"ts":"2026-10-18T09:00:27.123456+00:00","ca":"0x5b81…","tier":"BEST",
 "rms":3,"pc":14,"price":0.0421,"liq":184233.1,"fdv":2051221.0,
 "txns":4187,"byr":812}
```

| field | source | why |
|---|---|---|
| `ts` | run timestamp | join key, and the clock for outcome horizons |
| `ca` | contract address | **the only identity**. Tickers collide constantly — see below |
| `tier` | `classify()` | the thing being scored |
| `rms` | `recent_move_score` | the other thing being scored |
| `pc` | `persist_count` | lets a BEST row be re-derived; also the delivery canary |
| `price` | `price_usd` | outcome arithmetic |
| `liq` | `liq_usd` | slippage model input, and the exit-liquidity question |
| `fdv` | `fdv_usd` | with `liq`, reconstructs `liq_fdv_pct` |
| `txns` | `txns24` | pile membership for the random baseline |
| `byr` | `buyers24` | the input to the one gate with a known bug |

Short keys on purpose: at ~4,800 rows a day, key names are a material share of
the bytes.

### Format: one file per run

```
data/log/2026-10-18/0900.jsonl      # 50 lines, written once, never touched again
data/log/2026-10-18/0915.jsonl
```

Daily partitioning was the obvious proposal and it is the wrong one, for a
reason specific to committing data to git. **A file appended 96 times costs git
96 blobs, not one.** Measured against the live data:

| layout | git blob bytes/day, pre-delta | final bytes on disk |
|---|---|---|
| one file for everything | 48.80 MB | 1.01 MB |
| daily partition | 48.80 MB | 1.01 MB |
| hourly partition | 2.52 MB | 1.01 MB |
| **one file per run** | **1.01 MB** | 1.01 MB |

Git's delta compression handles pure appends well, so the daily figure would
not actually land at 48.8 MB — but it is the only layout where that depends on
packing behaviour rather than on arithmetic. One file per run is optimal by
construction and needs no assumptions.

It also buys something worth more than the bytes: **a per-run filename cannot
conflict.** The `Commit` step's open failure (`docs/TECH_NOTES.md`, run #1360) is an
add/add and content conflict on files two runs both rewrote. Two runs writing
`0900.jsonl` and `0915.jsonl` have nothing to collide over. The log is immune
to the bug that still takes `history.jsonl` down.

The cost is file count: 96/day, ~35,000/year. Compaction is below.

### Growth, measured

The implementation logs **every** token, not only the ~50 above
`MIN_SCORE_TXNS` that reach `history.jsonl`. Nothing here can be backfilled —
a row not written now is gone — and over-capturing is the reversible mistake.
It costs about 2.7x on disk. Earlier drafts of this document sized the log on
the scored set, and those figures were too low; these replace them.

One real run, 141 tokens: **28,017 bytes, 199 B/row**, 8,186 gzipped (3.4x on
a single file).

Gzip of one file understates what git stores, because consecutive runs are
nearly identical and git deltas them. Measured properly — 27 real snapshots
committed one run at a time into a scratch repo, then `git gc --aggressive`:

| | on disk | in the pack | per run |
|---|---|---|---|
| scored set (measured) | 0.29 MB | **0.07 MB** | **2.8 KB** |

| | raw/day | packed/day | packed/30d | packed/year |
|---|---|---|---|---|
| scored set (measured) | 1.00 MB | 0.27 MB | 8.0 MB | **97 MB** |
| all tokens (estimated) | 2.69 MB | ~0.3–0.7 MB | 8–20 MB | **100–260 MB** |

The all-tokens row is a **range, not a measurement**. The simulation could only
produce extra rows by duplicating real ones, which git deltas away perfectly
and would have flattered the result; the true figure is bounded below by the
measured scored-set number and above by scaling it 2.7x with no delta benefit
at all. Item 3 of this plan is to re-measure it against real runs after 24h,
and that number replaces this range.

**Baseline for that re-measurement, taken 2026-10-18 before the log existed:**
a full (non-shallow) clone of `Zirqo-Dev/hoodscout-data` reports
`size-pack: 48.95 MiB`, 14,766 objects, 2,392 commits, 50 MB `.git`. The
working checkout's 10.24 MiB is a shallow clone and understates the real repo
by about 5x — any size comparison has to use a full clone.

So a year of log adds somewhere between a doubling and a sixfold increase of
today's pack. That is the price of being able to answer "did this ever work",
and it is the reason compaction is designed now rather than later.

### Compaction

Per-run files are optimal for *writing* and bad for *reading*: 96 files a day,
~35,000 a year, and a labeler that wants a week has to open 672 of them.

**Nightly merge, `scripts/compact_log.py`, on its own schedule.** For every day
directory older than the current UTC day — so nothing can still be appending —
concatenate its files in filename order into `data/log/YYYY-MM-DD.jsonl.gz`,
verify the line count matches the sum of the parts, commit that, and only then
delete the directory.

- **Separate commit from the delete.** Write and verify the archive in one
  commit, remove the directory in the next. A crash between them leaves
  duplicate data, which is recoverable; the other order loses it.
- **Never touches today.** A run landing at 23:59 must not race the compactor.
- **Gzip, not plain.** At 3.4x on a single file and better across a day, a day
  becomes a few hundred KB.
- **Idempotent.** Re-running over an already-compacted day is a no-op.
- The pack cost is already paid — compaction frees working-tree inodes and
  makes reads cheap; it does not shrink history, because git keeps the
  per-run blobs forever. That is fine: they are small and they are the record.

Not in the first commit. The log is useful from the first run; compaction only
matters once there are thousands of files.

### Who reads the log

| reader | reads | writes |
|---|---|---|
| `collect.py` | **never** | one file per run |
| `compact_log.py` | closed day directories | one archive per day |
| `label.py` (3b) | log + archives | `data/outcomes/` |
| hypothesis evaluators (3c) | outcomes, filtered by `log_watermark` | a result, never the log |
| people, ad hoc | everything | nothing |
| the dashboard | **never** | — |

The one rule that matters: **nothing that writes the log may read it, and
nothing that reads it may feed back into a tier or a score.** The moment
`classify()` reads the log, the log stops being evidence and becomes an input,
and the next forward test has nothing independent to check against.

### Interaction with `history.jsonl`

They do not overlap and neither replaces the other.

| | `history.jsonl` | `data/log/` |
|---|---|---|
| purpose | working set the collector reads | permanent record nothing reads |
| window | 15 days, pruned | forever |
| written | full rewrite, every run | append-only, one file per run |
| read by | `persistence()`, `recent_move()`, `buyer_rate_rising()` | the labeler, and people |
| if it is lost | tiers go wrong until it refills | the past is gone |

`write_history()` is untouched. The log writer is a second, independent call at
the end of `main()`, and it must not be able to fail the run: a bad log write
should print a warning, like `load_avoid()` does, not cost the pipeline its
data commit.

The `history.jsonl` full-rewrite problem stays open and gets worse, not better,
once there is a second writer. For scale: it is 0.60 MB today because the prune
emptied it, and a full 15-day window at the current cadence is ~72,000 rows —
**32 MB rewritten 96 times a day, 3.1 GB of fresh blobs daily.** The log does
not fix that. It does mean the fix can be made without fear of losing history,
because the log already has it.

### Commit step

`git add data` already covers `data/log/`. Nothing in the step changes. The one
thing to check before building this is whether 96 new paths a day slows
`git add`/`git status` enough to matter in a 3-minute job — measure, do not
assume.

---

## Layer 3a-h — the holders log

A second write-only log, on its own cadence and its own workflow. One row per
token per holders run, under `data/holders/YYYY-MM-DD/HHMMSS.jsonl`. Nothing
reads it yet.

**What it is not.** Not a tier input, not a score input, not shown on the
dashboard, and not joined to labels. `scripts/collect.py` does not import it
and `index.html` does not fetch it. It exists so that H-0004 — concentrated
holdings preceding worse 6h returns — can be tested later against data that
was collected before the question was asked.

### Fields

```json
{"ts":"2026-10-19T15:30:00+00:00","ca":"0x39db…","symbol":"PONS",
 "pool":"0x7504…","tier":"WATCH","n_holders_returned":20,
 "total_supply":null,
 "holders":[{"address":"0xeb53…","balance":"10000","is_contract":true,
             "label":"Uniswap V4 PoolManager"}],
 "deployer":"0xdead…"}
```

| field | source | why |
|---|---|---|
| `holders[].balance` | Blockscout `value`, verbatim | the raw integer in base units. Not divided by `decimals`: we do not always have them, and a number that is sometimes scaled and sometimes not is two fields wearing one name |
| `holders[].is_contract` | Blockscout address object | the single most useful exclusion signal, and `null` when the API did not say |
| `holders[].label` | `name`, `ens_domain_name` or the first public tag | what makes a PoolManager recognisable as one |
| `total_supply` | `/tokens/{ca}`, cached forever in `data/holders/token_meta.json` | every token, not the five the RPC happened to have. Asked once and never again: the registered metric reads supply and balances from the same snapshot, and re-reading a number that barely moves would cost 960 calls a day |
| `decimals` | the same cache | stored for display only. The metric divides a raw balance by a raw supply, so the scale cancels and decimals never enters the arithmetic |
| `creator_eoa` | the creation transaction's sender, cached forever | the wallet behind the factory. `creator_address_hash` is usually a factory contract — 119 tokens, 44 distinct creators, one of them 43 of them — so the deployer alone does not identify anyone |
| `deployer` | `/addresses/{ca}` `creator_address_hash` | looked up once per contract and cached in `data/holders/deployers.json` forever; a `null` is retried weekly, because the API not knowing today is not proof it never will |

### Cadence and budget

Every 3 hours, by `workflow_dispatch` from cron-job.org — the same arrangement
as `collect.yml`, and for the same reason. 120 tokens per run, ordered by
liquidity, DEAD and AVOID dropped.

At **20 credits per call** — confirmed against the first run's own credit
header, not just the docs — steady state is 960 calls and **19,200 credits a
day** against a free tier of 100,000, and a worst case of 2,400 calls a day if
every run hits its cap, inside the 3,000 ceiling.

Three lookups are one-time and cached forever: the deployer, the token's
supply and decimals, and the creator wallet behind the creation transaction.
A cold cache wants roughly 240 of them, which is more than one run's headroom,
so the backfill is sized from what the holders calls leave rather than from a
fixed number: it spreads over two or three runs and never competes with the
cross-section. The client throttles to 4 requests a second under a 5/s limit,
caps itself at 300 calls per run, honours `Retry-After`, and abandons the run
after three consecutive 429s.

**Hitting the call cap abandons the run too**, rather than returning empty
responses and letting the loop write whatever it had. That distinction was a
real bug for the length of one edit: a cap that quietly truncates produces
exactly the half-filled cross-section the next paragraph forbids.

**An abandoned run writes nothing.** A half-filled snapshot is worse than no
snapshot here: the metric ranks tokens against each other *within* a snapshot,
so a partial cross-section would quietly change what "top quartile" means.

### Exclusions are not applied at collection

The stored rows are raw. The pool, the router, the PoolManager, burn addresses
and the token contract are all still in them, and the analysis removes them at
evaluation time.

That is deliberate optionality. Which addresses count as infrastructure is a
judgement that will change — a new router appears, a label turns out to be
wrong — and a judgement baked into stored data cannot be revised without
re-collecting history that no longer exists. The collector's only contribution
is `data/holders/exclusions_hint.json`: contracts appearing in the top 3 of at
least 5 different tokens, which is what shared infrastructure looks like. It
carries `"applied": false` and nothing reads it automatically.

### What the first run settled

The run of 2026-10-19T15:34Z, 119 tokens, 239 calls, resolved the open
questions and found two design faults in the hypothesis that was registered
against it.

Settled: the holders item nests its address under `address_hash` with `hash`
inside — 1,589 holder records parsed with zero nulls in `is_contract` and
zero missing balances. `creator_address_hash` is populated for 119 of 119.
**20 credits per successful call, confirmed exactly**: 100,000 − 95,240 =
4,760 = 238 × 20, and the single 404 cost nothing.

Found, and the reason H-0005 exists:

- **`is_contract` does not catch burn addresses.** `0x…dead` reports
  `is_contract: false` and holds balances in the 10²⁷ range. It is in the top
  10 of 26 of the 119 tokens. An `is_contract`-only exclusion would have
  counted it as a real holder on a fifth of the board.
- **The top-20 denominator was degenerate.** 63% of tokens have 10 or fewer
  surviving holders, which makes their concentration exactly 1.0 by
  construction, and the top-quartile cutoff landed inside a tie block of 63.
  The split would have measured how many holders Blockscout returns.
- **119 tokens, 44 distinct deployers**, one of them responsible for 43. The
  `creator_address_hash` is usually a factory, which is why the collector now
  resolves the creation transaction's sender as `creator_eoa`.

### Verified and unverified

`api.blockscout.com` is unreachable from the authoring environment, so none of
this was exercised against the live API before the first run. Confirmed from
Blockscout's published reference: the `api.blockscout.com/{chain_id}/api/v2`
host, the `apikey` parameter or `authorization` header, the 20-credit default,
the 100,000/day and 5/s free tier, the `x-credits-remaining` family of
response headers, and the `{"items": [...], "next_page_params": {...}}` shape
with a nested address object and a raw `value` string. **Not** confirmed:
whether the holders item's address key is `address_hash` or `address`, and the
exact spelling of the creation-transaction field. The parser accepts every
documented spelling and returns `None` rather than raising, and
`scripts/holders.py --probe` prints the real keys from two or three tokens so
the first Actions run settles it.

The API key is sent as a header, never as a query parameter, so a failing
request's URL cannot carry it into a log line.

## Layer 3b — the outcome labeler

A separate script, `scripts/label.py`, on a separate schedule. It must not run
inside `collect.yml`: the whole point is that the thing being scored and the
thing doing the scoring are not the same run.

### What it does

For every log row older than a horizon and not yet labeled, find the same `ca`'s
nearest price print to the target, inside a tolerance window, and write one
outcome record per (row, horizon).

```
horizons:  1h   ± 15 min
           6h   ± 45 min
          24h   ± 2 h
```

**Labels are not committed.** They are derived data: deterministic from the
log, rebuildable in seconds, and at three horizons per row they came to
22.8 MB/day — 8.5x the log itself, 684 MB a month — which is a great deal of
git for something a seed and a log already determine. `scripts/label.py`
computes them in memory; `--out` dumps them to `.labels/`, which is gitignored.
Baselines are computed at summary time for the same reason: the draw is
seeded, so storing it would add bytes and a second thing to keep in sync.

The sketch below shows the shape of one outcome, not a file that exists.

```json
{"ts":"2026-10-18T09:00:27…","ca":"0x5b81…","h":"6h",
 "status":"ok","ret_gross":-0.0312,"ret_net_500":-0.0421,"ret_net_5000":-0.0588,
 "t_actual":"2026-10-18T15:02:11…","dt_h":6.03}
```

### Thinning, and what n means

A 6h horizon sampled every 15 minutes gives up to 24 overlapping observations
for every independent one, and reporting those as 24 data points is the most
common way to turn noise into a finding. So summaries thin the *entry
snapshots* — every token in a kept snapshot is kept, only the frequency drops:

| horizon | entry snapshots used | on the 686-snapshot replay |
|---|---|---|
| 1h | every one | 686 |
| 6h | one per hour | 346 |
| 24h | one per 4 hours | 89 |

Thinning is not enough on its own, because the kept snapshots still overlap. So
every row of a summary carries **`eff_n`**: distinct `(ca, horizon-bucket)`
cells, which is what survives collapsing the overlap. On the replay, BEST at 6h
showed 6,418 thinned rows and **1,223** cells — a 5.2x gap, and the second
number is the one any claim should be read against. Hypotheses therefore set
`min_eff_n`, not a row count.

Status histograms are computed over the thinned population and never filtered:
thinning changes how often a token is sampled, never which outcomes are
counted.

### Dead and delisted tokens — the part that decides whether any of this is honest

This is where the baseline analysis already found the sharpest bias: **DEAD
rows had a forward price in 0 of 108 cases, and SPECULATIVE in 51.1%.** A token
is DEAD partly *because* it stopped printing. Silently dropping unmatched rows
would quietly delete the worst outcomes from every number downstream.

So the labeler never drops a row. It assigns a status, always:

| status | condition | treated as |
|---|---|---|
| `ok` | a print inside tolerance | the return |
| `pending` | row is younger than the horizon | not yet labeled; retried |
| `gone_no_liq` | token still seen, `liq <= 0` at or before target | **−100%** |
| `gone_unseen` | token stopped appearing, last seen healthy | **censored, never a number** |
| `thin` | matched, but `liq` at target below the notional being modeled | `ok`, flagged unexitable |

`gone_no_liq` is a real total loss and is scored as one. `gone_unseen` is
genuinely unknown — the token may be alive and merely off our collection
surface, since the collector sees trending plus new pools plus six direct
lookups, not the whole chain. Any analysis must report the `gone_unseen` rate
beside its result, and a result where that rate differs across the groups being
compared is not a result.

**Every published number carries its status histogram.** A median with 40% of
its population censored is a different claim from one with 2%, and they should
not look alike.

### Slippage and fees

Returns are reported three ways, always together:

- `ret_gross` — price in, price out, the number everything has used so far
- `ret_net_500` — $500 round trip
- `ret_net_5000` — $5,000 round trip

```
net = gross − 2·fee − impact(size, liq_in) − impact(size, liq_out)
impact(size, liq) ≈ size / (liq/2)        # constant product, one side of the pool
fee = the pool's own tier (1% / 0.3% / 0.05%), from main_pool; 1% if unknown
```

Two fixed notionals rather than a swept curve, so every result is comparable
and nobody gets to pick the size that makes their hypothesis work. The impact
term is a constant-product approximation against half the reported pool TVL —
it is a model, not an execution. It is systematically optimistic: it ignores
the sandwich risk on a public mempool, ignores depth concentration in a V3
range, and assumes the quoted liquidity is real. **No number here has been
traded.** Say so wherever these are published.

The $5,000 case matters most: at the median PERSISTENT liquidity it is a large
enough fraction of the pool that the impact term dominates the signal, which is
itself the finding.

#### Where the fee comes from, and what it cannot tell us

`fee_source` is on every outcome and has exactly two values.

| value | when | coverage on 2026-10-18T14:00 |
|---|---|---|
| `name` | GeckoTerminal's pool name carried a trailing percentage | 41 of 138 rows (29.7%), 64.9% of liquidity |
| `fallback` | everything else, charged 1% | 97 of 138 (70.3%) |

There is deliberately no third value. A `dex_constant` would be defensible for
`uniswap-v2-robinhood`, whose 0.30% is hard-coded as `mul(997)` over
`mul(1000)` in `UniswapV2Library`, but not for `pons-v2` (third-party sources
say fees are set per launch), and not at all for `pons-v2-dex` or
`bankr-robinhood`, where no published fee was found. An invented constant would
look like knowledge in this field and propagate silently.

An `onchain` source is **not implementable**, and not only because the RPC is
unreachable from the authoring environment. Of the 97 fee-less rows, 60 store a
**32-byte pool id rather than an address** — `bankr-robinhood` 26,
`pons-v2-dex` 17, Uniswap v4 17. A v4 pool id is `keccak256(poolKey, 0xa0)`
(verified in `v4-core/src/types/PoolId.sol`) and the fee is one of the five
hashed fields, so it cannot be recovered from the id. Recovering it needs the
`PoolKey` from an Initialize event or an indexer. A v4 pool may also carry
`DYNAMIC_FEE_FLAG = 0x800000` (`LPFeeLibrary.sol`), meaning the fee changes per
swap and no constant exists even in principle.

**ASSERTED, UNVERIFIED.** A Bitquery investigation titled *"Uniswap v4 Hooks on
Robinhood Chain: The 0% Fee That Costs 1%"* reports that the chain's largest
hook shows 0% on every swap while its contract takes a median of about one cent
per dollar. `bitquery.io` is unreachable from this environment, so this comes
from a search summary and has **not** been checked against the page, let alone
against chain data. If it holds, a correctly-read v4 fee can still understate
the real cost by roughly a point, and `fee_source == "name"` is not the same as
"the cost is right". It is recorded here because a claim that would undercut
the cost model deserves to be visible while unverified rather than forgotten
until someone rediscovers it.

#### How much the uncertainty actually matters

A fee error moves `net500` by exactly `2 x` the error, independent of size. So
it shifts a *level*, and largely cancels in a *difference* between two groups
with similar unknown-fee shares. On the 2026-10-18T14:00 snapshot those shares
were BEST 53.3%, the scored pile 53.7%, EMERGING 50.0% — near enough that the
differential bias on H-0001's and H-0003's gaps is **under 0.1pp** against
thresholds of 1.0pp and 3.0pp.

The exposure is to *absolute* clauses. H-0003's clause (a) is a bar of −5.0pp
on the EMERGING median with nothing to cancel against, and the same uncertainty
moves that median by up to **±1.0pp** — a fifth of the bar.

So `scripts/evaluate.py` reports, per hypothesis: the **composition** (rows and
unknown-fee share per group), a **fee-known-only re-run** of the metric over
`fee_source == "name"` rows alone, and a **swept bound** giving the verdict at
assumed unknown fees of 0%, 0.3%, 1% and 1.9% — the range observed in the data.
**If the verdict is not the same across that sweep, the status becomes
`inconclusive` with the reason "fee uncertainty spans the threshold":** the fee
is deciding the result and the market is not.

Per-tier known rates diverge much more than the group rates do — SPECULATIVE
14.1% against DEAD 58.6% — so a future hypothesis comparing those is not
protected the way H-0001 happens to be. All of these figures are one snapshot
of 138 rows and need recomputing as the sample grows.

### Baseline: same-pile random pick

A score only means something against the alternative of not using it. For each
labeled row, at the same `ts`, draw `k = 20` other tokens uniformly from the
same pile and label them identically.

"Same pile" is the comparison being made and has to be declared, not discovered:

- `pile=tier` — the other tokens in the same tier at that instant
- `pile=scored` — everything with `txns24 >= MIN_SCORE_TXNS`
- `pile=dashboard` — everything a reader would actually have seen on the page

The seed is `sha256(ts + ca + pile)`, so the draw is deterministic, reproducible
from the committed data, and cannot be re-rolled.

The headline number is never a return. It is **the signal's outcome minus the
same-pile random pick's outcome, at the same instant, with the same costs
applied and the same censoring reported.** A strategy that returns −2% in a
week when the random pick returned −6% is working.

---

## Layer 3c — hypotheses, written first

One file, `data/hypotheses.jsonl`, append-only, one object per line.

*Illustrative format only. The registered lines in `data/hypotheses.jsonl` are authoritative and differ from this example (pile, polarity, censoring, gates and stop_by).*

```json
{"id":"H-0001",
 "registered":"2026-10-20T09:00:00+00:00",
 "log_watermark":"2026-10-20T08:45:00+00:00",
 "claim":"PERSISTENT tokens with recent_move_score <= 0 beat a same-pile random pick at 6h",
 "population":"tier == 'BEST' and rms <= 0",
 "pile":"tier",
 "metric":"median(ret_net_500) − median(baseline ret_net_500)",
 "horizon":"6h",
 "min_n_rows":400,
 "min_n_tokens":30,
 "max_censored_pct":15,
 "pass_if":"metric >= +1.0pp and sign holds in >= 3 of 4 weekly subperiods",
 "fail_if":"metric <= +1.0pp after min_n is met, or censoring exceeds max_censored_pct",
 "stop_by":"2026-12-01",
 "status":"open"}
```

The rules that make it worth anything:

1. **`log_watermark` is the latest `ts` in `data/log/` at registration.** A
   hypothesis may only be evaluated on rows *after* its watermark. This is what
   makes pre-registration mechanical rather than a promise — the evaluator
   filters on it, and backdating requires rewriting committed history.
2. **`pass_if`, `fail_if` and `inconclusive_if` are all filled in before any
   result is seen**, and between them they cover every outcome. The third is
   not a hedge: a gap that clears the threshold but whose sign does not hold
   across subperiods is a real and distinct outcome, and it is the shape the
   earlier `chg_h6` finding had — +0.06pp in one week against −1.13 and
   −1.44pp in the next two. Calling that a pass would have been wrong and
   calling it a fail would have been wrong.
3. **Status moves one way**, through exactly these values:

   | status | meaning | set by |
   |---|---|---|
   | `registered` | written down, not yet decided | the registering commit |
   | `waiting` | gates unmet — `min_eff_n`, `min_n_tokens`, or fewer than four usable weekly subperiods when the decision turns on stability | `scripts/evaluate.py` reports it; the registry only moves on |
   | `inconclusive` | gates met, threshold cleared, sign unstable across subperiods | a person, naming the report |
   | `passed` / `failed` | the registered clause fired | a person, naming the report |
   | `expired` | `stop_by` reached with gates still unmet | a person, naming the report |
   | `superseded` | the question survives, the way it was operationalised does not | recorded by the successor, not by editing the original |

   **`superseded` is recorded by appending, never by editing.** The file is
   append-only, so an obsolete hypothesis cannot be marked in place. Its
   successor carries `"supersedes": "<id>"`, and that field is the whole
   mechanism: the original line stays exactly as it was written, and anything
   reading the registry treats an id named in some later record's
   `supersedes` as retired. The evaluator derives this from the registry: a
   superseded id with no evaluator rule is reported as `superseded` (by its
   successor, before any evaluation); one that has a rule is still evaluated,
   with the supersede shown as a note, because after outcomes have been seen
   superseding is a new test and the old result must stay visible.

   `waiting` is what the evaluator prints on every run until there is enough
   data; it is a state of the evidence, not of the hypothesis, and a
   hypothesis sitting at `registered` while the evaluator says `waiting` is
   the normal case. `expired` is a real result about the data rate, not a free
   pass. **The evaluator never writes to `data/hypotheses.jsonl`** — it emits a
   report, and a person moves the status in a commit that names it.
4. **Nothing is ever deleted from the file.** The denominator is the point, and
   it is arithmetic rather than etiquette. Test one hypothesis at a 5%
   threshold and a false pass happens 5% of the time. Test twenty and the
   chance that at least one passes on noise alone is 1 − 0.95²⁰ ≈ **64%** —
   so "we found one that worked" is the expected outcome of looking twenty
   times at nothing. A deleted or unregistered hypothesis shrinks the
   denominator without shrinking the search, which is the one edit that turns
   this file from evidence into advertising.

   **Superseding does not shrink the denominator either — but a hypothesis
   that never ran never consumed a test.** These are two different things and
   conflating them would be the same error in a nicer coat. H-0004 is the
   worked example: registered 2026-10-19 with `log_watermark: null`, it was
   superseded by H-0005 the same day, before the first holders snapshot had
   any outcome data joined to it. Zero rows were ever evaluated against it,
   no threshold was ever compared, no report ever named it. The family-wise
   arithmetic is therefore unchanged by its existence: the count of *tests
   performed* is what 1 − 0.95ⁿ is computed over, and H-0004 contributed
   none. It stays in the file, with its line untouched, because the record of
   what was asked is the thing the file is for — but a reader counting tests
   should count four, not five.

   The rule that follows, and it is not a loophole: a hypothesis may be
   superseded without cost **only** while nothing has been evaluated against
   it. Once a report has named it, changing the operationalisation is a new
   test and counts as one, however it is labelled.

   So: every report states how many hypotheses were open over the period, not
   just how many passed. Three registered and one passed is a claim worth
   making. Twenty registered and one passed is a coin landing heads. The file
   is the only thing that can tell those apart afterwards, and it cannot do it
   if anything was ever removed.
5. **A hypothesis that was not registered cannot be reported**, however good it
   looks. Things noticed while looking go in as new hypotheses with a fresh
   watermark, and get tested on data that does not exist yet.
6. **Every hypothesis records `labeler_version`**, the `git hash-object` of
   `scripts/label.py` at registration. The arithmetic that produces a number is
   part of the claim: change the cost model or the thinning and the same rows
   give a different answer, so a result evaluated under a different labeler is
   a different result and has to say so.

### Disclosure: the thresholds were not chosen blind

H-0001 to H-0003 were registered at watermark `2026-10-18T13:30:25Z` with a
`pass_if` and `fail_if` chosen by someone who had **already seen in-sample
numbers** — both the pre-log backtest over `history.jsonl` and a synthetic
replay of 43,365 of those rows through this exact labeler. That replay showed,
among other things, EMERGING at 6h with a −15.65% median against a −4.35%
baseline, and BEST at 6h with a −2.67% median against −4.17%.

The watermark makes the *evaluation* out of sample: only rows after it count,
and the replay is a different and earlier period. It does not make the
*thresholds* blind. A −5.0pp bar set after seeing −15.65% is a bar chosen to be
clearable, and +1.0pp for H-0001 was likewise picked knowing roughly how big
the gap was not.

This is recorded rather than fixed because the alternative — thresholds pulled
from nowhere — is worse, and because the honest response is to read a pass as
weaker evidence than a pass on a blind threshold would be. Future hypotheses
about a quantity nobody has measured yet do not carry this caveat, and should
say so explicitly when they do not.

### Layer 3d: reading a hypothesis

This protocol governs H-0001, H-0002 and H-0003, read once on Monday 2026-11-09. It applies to every later hypothesis unless its registration says otherwise. Decided by Pete on 2026-10-20 and 2026-10-21, before any read.

#### 1. Blind until the read date

Before a hypothesis's read date, nobody computes, posts or discusses its metric, gap, clause or status. That covers the Board, Notion, chat, briefs and the dashboard.

- Allowed before the read: eff_n, token counts, usable-week counts, and blind process metrics (row counts, gone_unseen share, fee-known share).
- Forbidden before the read: `evaluate.py --unblind`, `evaluate.py --report`, `label.py --summary`, and any ad-hoc script that prints a return.

**Disclosure.** Interim effect sizes for H-0001 to H-0003 were computed and seen on 2026-10-19 and 2026-10-20, before this rule existed (the first `label.py --summary` run and the 2026-10-20 independent review). Several later decisions were made by people who had seen those numbers: the reporting-only evaluator changes (b9e16bb2, 3e60d55e), the freeze rule, the replication rule and the censoring rule in step 5. None of them changed a deciding rule; the evaluator's decision path was verified AST-identical to f6ed629. Still, they were not made blind.

#### 2. One run, on pinned data

- **Data.** Use a fresh full (not shallow) clone, checked out at the first collector commit whose data timestamp is at or after 2026-11-09T07:00Z (2:00 AM Toronto). By then every W45 entry's 6h window [5.25h, 6.75h] has closed. The commit hash goes in the report.
- **Command, exactly:** `python scripts/evaluate.py --unblind --report`. Use the default pile (scored, as registered), with no `--since` and no `--pile`.
- **Once.** A run that crashes before printing any metric may be re-run after a fix. A run that printed any metric is the read. The report is committed unchanged, and commentary goes in a separate commit.
- **W46 rows.** About the first 1.75 h of W46 entries are labelled at that commit and enter the pooled metric. If W46 reaches the 30-row floor for any hypothesis, it counts as a week under the evaluator's existing rule. The report lists the weeks used, and the verdict line says so.

#### 3. Integrity audit, before unblinding

The audit prints counts, hashes and dates only. It is committed as `reports/2026-11-09-audit.md` before the unblinded run.

a. `git rev-parse --is-shallow-repository` prints false.

b. **Registry.** Every commit touching `data/hypotheses.jsonl` only appended lines. The H-0001 to H-0003 lines are byte-identical to the commit that added them. That commit is dated before 2026-10-18T18:45Z, the watermark plus 5.25h, before which no 6h outcome after the watermark could exist.

c. **Evaluator.** List every commit touching `scripts/evaluate.py` or `scripts/label.py` since 2026-10-18T14:00Z. Show that the decision path (the functions and constants in the HS-72 check) is AST-identical to f6ed629. Commits before 2026-10-18T18:45Z (5079b98, d5dfd54, 40ad08b) predate any possible post-watermark outcome; every later commit must be verdict-neutral. "AST-identical to f6ed629" means scripts/evaluate.py as it stood at commit f6ed629.

d. **Labeler.** Compare `git hash-object scripts/label.py` with the registered labeler_version (17e925ee). If they differ, run a blind equivalence check (`python scripts/label_equiv.py`): run both labeler versions on the pinned data and print only the number of (row, horizon) outcomes whose status or ret_net_500 differ. What a nonzero count means is decided before the read (Board HS-40).

e. **Earlier reports.** No file in `reports/` dated before the read contains an H-0001 to H-0003 metric, beyond what the step 1 disclosure covers.

#### 4. What a verdict may say

- Each verdict quotes the registry clause that fired.
- H-0001 is written with the null as its pass.
  - A pass reads: "BEST did not beat a scored-pile random pick by 1.0pp or more in median 6h net return, net of modelled costs." It never reads "no information" or "BEST is worthless": the design can reject an edge of at least 1.0pp, not every edge.
  - A fail reads: "BEST beat the scored pile by 1.0pp or more, sign held in at least 3 of 4 weeks." That is a pattern, not a signal (step 9).
- Every verdict line carries:
  - the words **"out-of-sample check of a backtest-tuned specification"**: the thresholds were set after the in-sample backtest, and interim numbers were seen before this protocol existed (caveats 1 and 2), so a pass or fail is not a clean pre-registered test. What may follow a pattern is set by the replication rule (step 9).
  - eff_n and tokens against the gates;
  - censoring against max_censored_pct;
  - fee-known share;
  - usable weeks, all-tier and arm-own;
  - whether the pooled and token-clustered medians agree in sign;
  - whether the labeler version matches;
  - a pointer to the thresholds-not-blind disclosure.
- The report keeps its count line: registered, superseded before evaluation, evaluated.

#### 5. Censoring above the registered cap

A status is set by a person, never by the evaluator (Layer 3c, rule 3).

When the read finds a population's gone_unseen share above that hypothesis's registered max_censored_pct, the person records **inconclusive (censoring)**, whatever clause fired. The share is gone_unseen rows over all population rows, as `gates_for` computes it. This applies to H-0001 (cap 20) and H-0002 (cap 25), whose clauses do not mention their cap, and matches what H-0003's registration already does (cap 50).

The report still shows every number. Evaluator code and the registry are unchanged.

Stated in advance: the blind check on 2026-10-21 at 02:30Z had H-0002 at 54.1% against its cap of 25, so H-0002 will very likely be recorded inconclusive on 2026-11-09.

#### 5b. Stability counted on more than four weeks

Decided by Pete on 2026-10-26, blind, before the read.

The registry says the sign must hold "in at least 3 of 4 weekly subperiods". evaluate.py instead counts at least 3 matching weeks among all usable weeks (30+ all-tier rows). The read will likely see 5 (a short W42 and a near-empty W46). The report's line "sign holds in 3 of 4" uses that count. The code stays frozen (step 8), so the registered wording is applied by hand:
- If the report says the sign holds and more than 4 weeks are usable, take the 4 usable weeks with the most all-tier rows, from the report's Weeks table. On a tie, take the later week.
- Read each of those weeks' signs from the Weekly subperiods table. If fewer than 3 of the 4 share the pooled metric's sign, the person records **inconclusive (instability)**.
- This can only turn a stability-dependent verdict (an H-0001 fail or an H-0002 pass) into inconclusive. It never creates a pass or a fail. H-0003's clauses do not use stability.
- With exactly 4 usable weeks, the evaluator's count already matches the registry and nothing changes.

#### 6. Caveats, written before the read

1. **Thresholds not chosen blind.** See the disclosure section.
2. **Interim numbers were seen** on 2026-10-19 and 2026-10-20 (step 1).
3. **Cost model.** The fee is known for only part of the rows; the rest use the 1% fallback. Share of population rows with `fee_source == "name"`, as `evaluate.py --blind` prints it, on 2026-10-26: H-0001 55.7%, H-0002 46.4%, H-0003 18.0%. The share drifts as rows accrue. The read quotes the report's own per-arm figures, which can differ from the population share. H-0003's level clause (a) is therefore mostly a statement about the fallback. The fee sweep in the report shows the sensitivity.
4. **Usable weeks.** They are counted on all-tier rows (30 per ISO week), not on the tested arm; the report shows the arm-own count beside it. W42 covers about 1.4 days from the 2026-10-18 13:30Z watermark, yet it weighs the same as a full week in the sign test. Step 5b covers a read with more than 4 usable weeks.
5. **Asymmetric burden.** For H-0001, a pass needs no stability and a fail needs 3 of 4 weeks. For H-0002, a fail needs no stability and a pass needs 3 of 4 weeks. The design keeps the null more easily than it rejects it, so a null is weak evidence of "no edge".
6. **Pooled vs clustered.** The registered pooled median decides. The token-clustered statistic is shown beside it and never decides. A sign disagreement between the two goes in the verdict line.
7. **Survivorship.** gone_unseen rows carry no return, so medians are over the rows that have one. Imputed -100% and flat-exit sensitivity is reported for H-0003 only.
8. **Labeler version.** See audit step 3d.
9. **H-0002's horizon** is narrower than the original 4 to 8h finding (registry notes). A null there does not fully refute the original.
10. **Slippage** is modelled, not traded.
11. **Gates beyond the clauses.** The evaluator also decides with two gates the registry clauses do not mention: the fee-sweep bound, and the known-fee-only re-run, which makes a verdict inconclusive if it flips it after meeting its own gates. Both were committed 2026-10-18 between 14:09Z and 15:05Z, after registration but before any post-watermark outcome existed, and are frozen with the rest of the decision path. The verdict line says which gate fired.

#### 7. If H-0003 is not ready at the pinned commit

evaluate.py computes a hypothesis's metric before it checks the gates, so an unblinded run prints H-0003's numbers even while it is waiting.

If H-0003's eff_n is below 100 or its token count below 20 at the pinned commit:
- its section of the 2026-11-09 report is an interim exposure, disclosed as such;
- H-0003 is read at the first Monday pinned commit after its gates are met, by this same procedure.

#### 8. Freeze rule

Once an evaluator join exists for a hypothesis, none of these may change: its rule, exclusion list, threshold, population filter or deciding statistic. Any such change is a new hypothesis, registered with a fresh watermark and counted in the tally. This holds now for H-0001 to H-0003, and for H-0007 once its join is built.

Allowed after the join:
- bug fixes shown verdict-neutral, by an AST-identical decision path or identical outputs on the pinned data checked blind;
- report-only additions;
- docs.

H-0007 is the last free supersession of the holders hypothesis.

#### 9. Replication rule

A result that supports a usable pattern is not a signal. "Supports a pattern" means an H-0001 fail, an H-0002 pass or an H-0003 pass; an H-0001 pass is the null and has nothing to confirm.

- **Confirmation.** Before anything reaches Layer 4, it gets a confirmation hypothesis: the same population, metric, thresholds and clauses, a new watermark (fresh data only), and its own read date fixed at registration. It counts in the tally.
- **Layer 4 entry.** Only a confirmed pattern may appear in a Layer 4 experimental panel, and from there only in Layer 5 paper trading with rules fixed in advance.
- **Failed confirmation.** It retires the pattern. No third try with tweaks; a changed rule is a new hypothesis.
- **Inconclusive confirmation.** It keeps the pattern out of Layer 4 but does not retire it. The confirmation hypothesis keeps waiting until its own stop_by.

#### 10. Recording the verdict

The evaluator never writes the registry. A verdict is recorded in three places:
1. the committed report;
2. a "Results" table appended to this document (id, read date, status, report path, commit);
3. the Board card.

No status lines go into `data/hypotheses.jsonl` until a status-line format exists and is tested against evaluate.py, because an unknown line type could crash it.

#### 11. What a verdict can and cannot detect (HS-77)

Simulated power, computed on pre-watermark data only: `data/history.jsonl` as of commit d9f8047 (2026-09-04 to 2026-10-02), the same rows the in-sample backtest already used. No post-watermark return was computed.

Method:
- The 6h outcomes and same-pile baselines mirror the labeler.
- Whole tokens are resampled with replacement, which keeps clustering and overlap.
- The in-sample gap is removed, then a known true edge is added.
- Weekly regime noise of 0.5 to 1.0pp is added (the pre-watermark weekly deviation was 0.8pp).
- The registered rules are applied at the projected 2026-11-09 sample size: 2,000 replicates per cell, seed 20261021.

Code and output: `analysis/hs77/`.

**H-0001** (eff_n about 1,800, about 126 tokens):

| True BEST edge | Read as fail (edge detected) | Read as pass |
|---|---|---|
| 0 | 1–3% | 96–99% |
| +0.5pp | 12–14% | 85–88% |
| +0.75pp | 25–26% | 74–75% |
| +1.0pp | 38–45% | 55–61% |
| +1.5pp | 65–77% | 23–34% |
| +2.0pp | 86–92% | 8–13% |
| +3.0pp | 98–100% | 0–2% |

Inconclusive was 1% or less in every cell.

**H-0002** (eff_n about 2,000 per arm; 4 equal time bins stand in for weeks, because rms was only logged from 2026-09-14):

| True gap (+4 arm minus −4 arm) | Read as pass (edge found) |
|---|---|
| 0 | 4–8% |
| −0.5pp | 20–25% |
| −1.0pp | 51–53% |
| −1.5pp | 77–85% |
| −2.0pp | 91–98% |

These H-0002 figures are moot if step 5 records it inconclusive (censoring).

**What this means for the wording**

- An H-0001 pass rules out a large edge, not a small one. Its verdict line adds: "This design detects a +2pp edge about 9 times in 10 and a +1pp edge about 4 times in 10; a pass does not exclude an edge below about 1.5pp."
- An H-0001 fail is unlikely to be a false alarm (1–3% when there is no edge), but it is still only a pattern until confirmed (step 9).
- An H-0002 pass is a false alarm 4–8% of the time when there is no edge, and it also needs confirmation.

**Limits**
- September tokens at 30-minute cadence; the regime may differ.
- The scored pile is approximated as rows with a tier, because there was no txns24 field then.
- A 1% fallback fee on every row.
- The edge is added as a constant to every row.
- eff_n is projected linearly (H-0001 about 80 a day).
- Weekly noise is assumed Normal.
- Not simulated: the fee-sweep and known-fee-only gates, and the step 5 censoring rule. They can only add inconclusives, so the detection rates above are upper bounds.
- Subperiods: the simulation always uses exactly 4 subperiods with a 30-row floor on the tested arm. The real read will likely have 5 usable ISO weeks (a short W42 and a near-empty W46, both above the evaluator's all-tier 30-row floor) and needs 3 sharing the pooled sign. Extra short weeks make "sign holds" slightly easier to reach, so real inconclusives would be even rarer than the simulated 1% or less; detection rates barely change because an H-0001 pass needs no stability. Also simplified: an empty arm-week counts as a zero gap rather than being dropped, and BEST rows with no baseline are left out of the simulated BEST median (few rows). Reviewed by Claude Code on 2026-10-21; seeded rerun matched on numpy 2.5.3.

---

## Layer 2 — attention, via the xAI API

### What I verified, and what I could not

**`docs.x.ai` still does not resolve from this container** — the network policy
blocks it. But xAI's official cookbook, `xai-org/xai-cookbook`, is on GitHub and
GitHub is reachable, so the section below is built from **xAI's own working
code** at commit `72d7e40`, which is a better source than the reference anyway:
it shows what the fields are actually called and which of them can be trusted.

Verified, from that source:

| | evidence |
|---|---|
| Tool is `x_search` | `tools=[{"type": "x_search", "from_date": yesterday}]`, `x-sentiment-analysis/python/guide.ipynb`; `xSearch({…})` in the TS SDK |
| Options | `from_date`, `to_date`, `allowed_x_handles`, `enable_image_understanding`, `enable_video_understanding` |
| **Dates only, no times** | "Since X Search only takes dates, Grok … often narrows its own queries with 𝕏's `since:` operator down to the minute" |
| **At most 10 posts per search** | "X Search returns at most 10 posts per search, so the app runs one search for each of the last DAYS days" |
| **Only the last 30 days** | "X Search is limited to the last 30 days" |
| Citations are structured | a `citation` event carrying `citation.url`; post id via `` url.match(/\/status\/(\d+)/) `` |
| **Post time comes from the id** | "The post's date comes from its ID, which holds the time it was published" |
| Usage is itemised | `usage.cost_usd`, `usage.server_side_tool_usage_details.x_search_calls`, `.x_posts_fetched` |
| X query operators work | `(${topic}) min_faves:20 -is:retweet -is:reply` |

Three of those answer the questions the previous draft could not, and two of
them change the design.

**Timestamps: yes, but not from where you would expect.** In the sentiment
tracker, a post's `created_at` is part of **Grok's structured output** — the
model transcribing what it read, under the instruction "Don't make up or
paraphrase posts", then filtered against the citation list because
"keeping only cited posts rules out made-up ones". That is a model-written
field, and this project should not hang a first-mention time on one. The
fact-check example takes the honest route and the cookbook says so plainly:
the date comes from the post id. X ids are Snowflake ids with a millisecond
timestamp in the high bits, so the time is **derivable from the citation URL
alone, with no trust in the model at all**. That is what the pilot does.

**Still not verified:** the exact Snowflake epoch constant (the pilot
cross-checks derived times against the model's `created_at` on the first run
and stops if they disagree), whether a free-text search for a 42-character hex
string retrieves anything useful, whether an empty search is billed, and the
`$5 per 1,000 posts` figure — which no longer matters, because the response
reports `cost_usd` directly.

### What this changes

**`mention_count` was never going to be a count.** Ten posts per search is a
ceiling, so any token with real discussion saturates it and every popular token
looks identical. A count only exists if you run one search per day-window and
add them up, at 10 per window — which is what the cookbook does for its ten-day
chart, and which multiplies cost by the number of windows.

So the pilot stores `posts_found` and a `saturated` flag, and the field is
named for what it is: **"at least this many, up to 10 per window."** Anything
built on it has to treat 10 as censored-above, exactly as Layer 3b treats a
missing price as censored.

**First-mention has a hard floor at 30 days.** Not a tunable, not a budget
choice — the index does not go back further. `first_mention_ts` can only ever
mean "earliest post found within the last 30 days". For a token older than 30
days it is close to meaningless, and for a token younger than 30 days it is
real. **Store `age_days` beside it** so the two cases are never mixed, and
never compare a first-mention across that boundary.

### Pilot design

Deliberately small, deliberately boring, and **not** wired into `collect.yml`.

- **Scope:** up to 20 tokens — all PERSISTENT, then EMERGING, then WATCH by
  `txns24` descending, to a cap of 20.
- **Matching is by contract address only.** Never by ticker. In the snapshot
  this document was written against, 15 tickers were held by more than one live
  token: `CASHDUCK` by **ten**, `REXCOIN`, `OWN` and `Niggabutt` by four each,
  `CASHCAT`, `Vest` and `CASHDOG` by three. `AMC`, `GME` and `MSTR` each had two
  — the tracked stock token and something else, one of which is a funded
  impersonator already on `avoid.json`. A ticker match would produce confident
  garbage.
- **Query:** the bare contract address. No `min_faves` filter, unlike the
  cookbook — its threshold exists to cut spam on a popular topic, and here a
  single early post from a nobody is exactly the thing worth finding. The cost
  is that the results will be mostly bots; see the failure modes.
- **Two searches per token:** one over the last 24h, one over the last 30 days.
  The first is the running signal, the second establishes first-mention once
  and is skipped on later runs for a token that already has one.
- **Trust only citations.** Take `citation.url`, extract the post id, derive the
  time from the id. Ignore the model's own `created_at` except as a sanity
  check on run one. A post the model mentions that is not in the citation list
  is dropped, as all three cookbook examples do.

```json
{"ts":"…","ca":"0x5b81…","window":"24h","search_from":"2026-10-17",
 "posts_found":7,"saturated":false,
 "first_mention_ts":"2026-10-14T22:11:03Z","first_mention_source":"post_id",
 "token_age_days":11.4,"first_mention_floor":"2026-09-18",
 "urls":["https://x.com/…/status/1978…"],
 "x_posts_fetched":7,"x_search_calls":1,"cost_usd":0.041}
```

- **No sentiment score, no ranking, no feed into `classify()`.** The cookbook
  makes sentiment scoring easy, and that is the trap: there is nothing to
  validate a sentiment score against until Layer 3b has labelled outcomes, so a
  score added now is the fourth unvalidated score in a project that has already
  retired three. Whether attention predicts anything is a Layer 3c hypothesis,
  registered before the data is looked at. Sentiment comes after that, if at
  all.

### Cost, and the cap

The response reports `usage.cost_usd`. **Spend is read, not estimated** — the
previous draft's arithmetic was guesswork and is now unnecessary.

Shape of it: 20 tokens × 2 searches × up to 10 posts = up to 400 posts a run,
plus tokens. At the widely-reported $5/1,000 posts that is around $2/run before
token cost, so the cadence question answers itself the same way as before:

| cadence | runs/month |
|---|---|
| every collect run | 2,880 — never |
| hourly | 720 |
| **daily (the pilot)** | **30** |

**Hard cap, enforced in code, failing closed:**

- `XAI_MONTHLY_CAP_USD = 25.0`, a constant in the script.
- `data/attention/budget.json` holds `{"month":"2026-10","spent_usd":…,
  "runs":…,"x_posts_fetched":…}`, committed like any other data.
- Read the ledger **before** any API call; exit 0 with a printed reason if
  `spent_usd >= cap`. Exit 0, not 1, so a capped month is a quiet no-op rather
  than a red run.
- Accumulate `usage.cost_usd` after every response and write it back **in the
  same run**, so a crash mid-run cannot lose the spend that already happened.
  Write after each token, not once at the end.
- A missing or unparseable ledger is treated as **at the cap**. Failing closed
  on a money-spending script is worth the occasional false stop.
- A per-run post ceiling as a second limit, from `x_posts_fetched`, so one
  pathological response cannot spend the month in a single call.
- `cost_usd` is xAI's number, not the invoice. Reconcile against the real bill
  in week one.

### Kill conditions

Written before the pilot runs, so that abandoning it is a decision that was
already made rather than an argument had later. **Any one of these ends the
pilot**; it does not trigger a workaround.

Each has a check that can be computed from the stored rows, so none of them
depends on anyone noticing.

| # | condition | why it is fatal |
|---|---|---|
| 1 | **Derived post times disagree with the model's `created_at` by more than an hour** on run one | the Snowflake epoch constant is wrong, so every `first_mention_ts` is silently wrong. Stop before storing a second row |
| 2 | **Zero posts for 20/20 tokens on three consecutive runs** | searching for a bare contract address does not retrieve. The premise is false and no amount of tuning fixes it |
| 3 | **Over 80% of searches return the 10-post ceiling** | the measure is saturated, every token looks identical, and it cannot discriminate |
| 4 | **Over 20% of model-reported posts are absent from the citation list** | the model is inventing posts faster than citation-filtering removes them |
| 5 | **Monthly `cost_usd` reaches the cap** | already designed to fail closed; the pilot stops for the month, not "just this once" |
| 6 | **One run costs more than 4x the median run** | something is looping or fetching far more than intended. Stop and read it before the next run |
| 7 | **Over half the pilot's tokens are older than 30 days** | first-mention is unanswerable for most of the population, so the pilot cannot address its own question |
| 8 | **Any first-mention traces back to this project's own dashboard or repo** | the measurement is measuring itself, and nothing separates the two afterwards |
| 9 | **Anything outside `attention.py` reads the attention data** before a 3c hypothesis covering it is registered | the signal has become an input without ever being tested; that is the failure mode this whole document exists to prevent |
| 10 | **Six weeks with no registered hypothesis reaching `min_n`** | the data rate is too low to answer anything. Retire it and say so |

Conditions 1–4 are about whether the data means anything. 5–6 are about money.
7–8 are about whether the question is answerable at all. 9–10 are about
discipline, and 9 is the one most likely to be violated quietly.

A kill is recorded in `data/hypotheses.jsonl` as an `expired` entry with the
condition that fired, the same as any other result. "We stopped because it was
not working" is a finding and belongs in the record.

### The secret

`XAI_API_KEY`, repo secret, passed as `env:` on the step only — the same shape
as `OPENSEA_API_KEY` in `nft.yml`. The script prints `using XAI_API_KEY from
the environment` and **never the value, never a prefix, never a length**. No
fallback that mints or derives a key: absent secret means exit 0 with a
message, as the NFT path learned the hard way.

**X Premium does not come into it.** The consumer subscriptions — X Premium,
SuperGrok, grok.com — are a separate billing track from the developer API at
`console.x.ai`, and carry no API credit. The one official route from one to the
other runs the other way: X's own developer docs offer xAI credit as a rebate
on **X API** spend, up to 20% at $1,000+, nothing below $200. Linking an X
account to the xAI console associates the subscription but does not change API
access. Sources for this are secondary except X's developer docs, so check the
console's own billing page before assuming either way.

---

## Failure modes

### Things that break

| | what happens | containment |
|---|---|---|
| log writer throws | run loses its data commit | wrap in try/except, warn, continue — never fail the run |
| 96 files/day slows `git add` | 3-minute job creeps up | measure before building; roll closed days into one file |
| file count hits a limit | checkout/clone degrades | weekly roll-up of closed days |
| labeler runs inside collect | the thing scored scores itself | separate workflow, separate schedule |
| labeler backfills on every run | O(n²) as the log grows | watermark file; only label rows newer than it |
| `data/log/` files conflict | — | cannot: unique path per run |
| `history.jsonl` conflicts | run #1360, still open | unchanged by this; see `docs/TECH_NOTES.md` |
| xAI SDK changes shape | the pilot breaks | pin the SDK version; the cookbook is a moving target |
| Snowflake epoch constant wrong | every `first_mention_ts` silently off | cross-check against the model's `created_at` on run one; stop if they disagree |
| model invents a post | a citation-less URL in the data | keep only cited posts, as all three cookbook examples do |
| 10-post ceiling read as a count | popular tokens look identical | `saturated` flag; treat 10 as censored-above |
| cost ledger lost or corrupted | uncapped spend | treat missing as at-cap |
| cron-job.org dies | log stops, silently | existing single point of failure, `docs/TECH_NOTES.md` |

### Things that mislead, which is worse

**Survivorship is the big one.** Measured, not hypothesised: **100%** of DEAD
rows and 48.9% of SPECULATIVE rows have no forward price at 4-8h. DEAD is
total — 0 of 108 — because a token is DEAD partly for having stopped printing.
Any analysis that drops
unmatched rows is reporting the returns of tokens that survived, which is a
different and much prettier question.

**Overlapping windows.** A 6h horizon sampled every 15 minutes puts up to 24
overlapping observations behind every independent one. Measured at the */30
cadence that produced the baseline, collapsing to (token, 6h-bucket) cells cut
37,954 rows to ~9,300 — about 4x inflation, and the current cadence doubles
the overlap again. `min_n_rows` is therefore not enough on its own, which is
why `min_n_tokens` sits beside it.

**Token clustering.** One token with 600 prints can carry a pooled median on its
own. The baseline table has a worked case: pooled, BEST leads WATCH by 0.19pp;
clustered by token, WATCH leads by 1.30pp. **Both statistics are always reported, and the paired within-token test when the populations overlap; the registered (pooled) statistic decides, and a sign disagreement is written into the verdict (Layer 3d, step 6).**

**Regime.** The `chg_h6` mean-reversion effect was +0.06pp in one week and
−1.13/−1.44pp in the next two. Three weeks of data found an effect that one of
those three weeks did not contain. Every hypothesis carries a subperiod rule for
this reason.

**Reflexivity.** The dashboard is public and indexed. If anyone reads it, tokens
it surfaces may get attention *because* it surfaced them, and Layer 2 would then
be measuring the project's own echo. Record the dashboard's publication date and
treat attention data as contaminated after it.

**A model is in the retrieval path.** X Search is a server-side tool the model
drives: it writes the query, decides when to stop, and transcribes the results.
Two runs with identical inputs need not fetch identical posts. That is fine for
"was this discussed" and wrong for anything needing a stable denominator, so
nothing downstream may assume the search is reproducible. Only the citation
URLs are hard data.

**Post counts are censored above and biased below.** Two separate problems.
Censored above: ten posts per search is a hard ceiling, so every token with real
discussion reports the same number. Biased below: searching for a contract
address finds posts *containing a contract address* — overwhelmingly bots,
scrapers and launch spam, because humans discussing a token use the ticker,
which cannot be matched safely here. The cookbook's own comment is the tell:
"Without a like threshold, the latest posts on most topics are mostly spam."
It filters with `min_faves:20`; the pilot deliberately does not, because an
early post from a nobody is the thing worth finding, and pays for that in
noise. `posts_found` measures bot-and-scraper volume more than human attention
and is trivially manipulable by anyone who wants a token to look discussed.

**Left censoring on first-mention is structural, not a setting.** X Search
reaches back 30 days and no further. `first_mention_ts` can only mean "earliest
post found in the last 30 days" — real for a token younger than that, close to
meaningless for one older. `token_age_days` and `first_mention_floor` are
stored on every row so the two cases can never be compared by accident.

**Slippage is modelled, not traded.** The constant-product impact term is
optimistic in every direction that matters: no sandwiching, no V3 range
concentration, quoted liquidity assumed real and assumed present at the moment
of exit.

**Hypothesis-fishing.** Pre-registration stops only the most obvious version.
Registering twenty hypotheses and reporting the one that passed is still
fishing, which is why the registry is append-only and every report states how
many were open. A pass rate of 1-in-20 at a 5% threshold is exactly what noise
looks like.

**Horizon and threshold shopping.** Three horizons and several pile definitions
make a large enough grid to find something. Both are fixed in the hypothesis
before registration, and a hypothesis that changes either is a new hypothesis
with a new watermark.

---

## The smallest first commit

**Layer 3a's writer, and nothing else.**

- `scripts/collect.py`: one function, `write_log(tokens, now)`, and one call at
  the end of `main()`, wrapped so it can never fail the run.
- `data/log/.gitkeep`.
- This document.

Not in it: the labeler, the hypotheses file, anything xAI, any change to
`history.jsonl`, any change to `classify()` or to any threshold, any change to
the dashboard.

One thing Layer 2 needs that costs nothing to start now: the first-mention
floor moves forward every day. Thirty days from now, today's posts are gone
from the index. Nothing in the first commit depends on that, but it is the
reason not to leave the attention pilot for a quarter.

It is reviewable in about thirty lines. It is additive — nothing reads the log,
so a bug in it cannot move a tier or a score. It is reversible: delete the call
and the directory. And it starts the clock, which is the only thing here that
cannot be bought back later. Every day without it is a day of outcomes that can
never be labeled.

The natural second commit is the labeler, because the log is worth nothing
unlabeled. The natural third is the hypotheses file, before anyone looks at the
labels. Layer 2 comes after all three, and only once someone has read xAI's real
documentation.
