# Cleanup backlog

Known-open items, recorded here so they survive outside a chat log. Nothing
here is urgent or in progress.

## Stale branch: `claude/bump-github-actions-36sdya`

Do **not** delete without review. It diverges badly from `main`: a
`git pull --rebase origin main` on it tries to replay 66 unrelated commits
("Initial commit", "Create collect.py", ...) and conflicts on `README.md`.
Its last own commit is `138f10e` (2026-09-04). A fresh container can come up
checked out on it, so confirm the branch before working: all pipeline work
lives on `main`.

## `nft.py` `get()` has no 429 backoff

`post()` (key minting) backs off; `get()` — every data call, including the
~41 discovery makes — does not. Latent: runs that fail do so at `mint_key()`
before any GET executes. Same gap `collect.py` and `stocks.py` had before
30d36e7 / ec018e8.

## ~~`OPENSEA_API_KEY` secret is not set~~ — RESOLVED 2026-10-17

The secret is set. Verified by nft.yml **run 63** (manual dispatch,
2026-10-17T14:13:53Z, success): the step env shows `OPENSEA_API_KEY: ***`
and the run printed `using OPENSEA_API_KEY from the environment, not
minting`, so `mint_key()` returned before reaching `post("/auth/keys")`.
No 429, no mint_key error. Tracked 3 collections, discovered 20.

The 2-keys-per-day cap no longer applies, so manual `nft.yml` dispatches
are safe again and no longer cost a scheduled run its key.

## `history.jsonl` is full-rewritten every run

`write_history()` reads the file, prunes rows past `HISTORY_DAYS`, appends the
new snapshot and writes the whole thing back with `open(path, "w")`. So each
run commits a fresh ~21 MB blob to git for ~25 KB of genuinely new rows. At
the `*/15` cadence that is up to ~96 full rewrites a day. Should become an
append plus a periodic prune, or a rolling set of per-period files, so git
stores the delta rather than a new whole-file blob each time.

## Revisit the cadence-coupled constants after a week

`PERSIST_RUNS`/`PERSIST_MIN` (16/10) and `buyer_rate_rising`'s `window` (5)
count snapshots, not time, so their wall-clock meaning depends on how many
scheduled runs GitHub actually delivers. They were set assuming `*/15`
delivers ~96/day, but observed delivery at `*/30` was 22.9 snapshots/day
against a nominal 48 — under half. If `*/15` lands nearer 46/day than 96,
these are roughly 2x too large and the BEST bar spans ~8h rather than the
intended ~4h. Measure the real rate over a week, then retune. Same for
`HISTORY_DAYS = 15`, which was halved to hold file size flat on the
assumption of doubled delivery.

## The `Commit` step cannot resolve `data/` conflicts

`git pull --rebase --autostash` in each workflow's Commit step fails on a
content conflict in the data files rather than resolving it (seen on
collect.yml run 665, 2026-09-17, caused by pushing code while runs were in
flight). Exposure is lower since `stocks.yml` stopped being a scheduled
writer, but `collect.yml` and `nft.yml` can still collide.
