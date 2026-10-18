# Evidence layer

Design only. Nothing here is built. The order is deliberate: **Layer 3 first**,
because without an outcome log there is no way to tell whether Layer 2 helped,
and a signal you cannot score is a signal you will end up trusting on vibes.

Everything in this document is a proposal to be argued with. Where a number is
measured it says so and gives the measurement; where it is an estimate it says
that too.

---

## Why this order

The forward-return baseline in `BACKLOG.md` is the whole argument. Over 43,365
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
conflict.** The `Commit` step's open failure (`BACKLOG.md`, run #1360) is an
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

Output `data/outcomes/2026-10-18/0900.jsonl`, same per-run layout, same
non-conflicting property.

```json
{"ts":"2026-10-18T09:00:27…","ca":"0x5b81…","h":"6h",
 "status":"ok","ret_gross":-0.0312,"ret_net_500":-0.0421,"ret_net_5000":-0.0588,
 "t_actual":"2026-10-18T15:02:11…","dt_h":6.03}
```

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

One file, `docs/hypotheses.jsonl`, append-only, one object per line.

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
2. **`pass_if` and `fail_if` are both filled in before any result is seen**, and
   between them they cover every outcome. "Inconclusive" is allowed only as
   `min_n` not yet met.
3. **Status moves one way**, `open → passed | failed | expired`, in a commit
   that names the evaluation's output. `expired` when `stop_by` passes with
   `min_n` unmet; that is a real result about the data rate, not a free pass.
4. **Nothing is ever deleted from the file.** The denominator is the point: ten
   hypotheses with one pass is a very different claim from one hypothesis with
   one pass, and only an append-only registry can tell them apart. Any report
   states how many were open over the period.
5. **A hypothesis that was not registered cannot be reported**, however good it
   looks. Things noticed while looking go in as new hypotheses with a fresh
   watermark, and get tested on data that does not exist yet.

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

A kill is recorded in `docs/hypotheses.jsonl` as an `expired` entry with the
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
| `history.jsonl` conflicts | run #1360, still open | unchanged by this; see `BACKLOG.md` |
| xAI SDK changes shape | the pilot breaks | pin the SDK version; the cookbook is a moving target |
| Snowflake epoch constant wrong | every `first_mention_ts` silently off | cross-check against the model's `created_at` on run one; stop if they disagree |
| model invents a post | a citation-less URL in the data | keep only cited posts, as all three cookbook examples do |
| 10-post ceiling read as a count | popular tokens look identical | `saturated` flag; treat 10 as censored-above |
| cost ledger lost or corrupted | uncapped spend | treat missing as at-cap |
| cron-job.org dies | log stops, silently | existing single point of failure, `BACKLOG.md` |

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
clustered by token, WATCH leads by 1.30pp. **Both statistics, always, and the
paired within-token test when the populations overlap.**

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
