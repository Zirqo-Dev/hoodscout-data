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

## `OPENSEA_API_KEY` secret is not set

NFT works without it, but with zero headroom: OpenSea caps key creation at
2 per day ("Key creation rate limit exceeded. Maximum 2 keys per day.",
`retry-after: 43080`), and the 13:00/21:00 schedule mints exactly 2. Runs
39-62 went 23/24, the one failure being that 429. Consequence: a manual
`nft.yml` dispatch is a third mint and will fail, likely taking that day's
scheduled run with it. Setting the secret removes the cap and makes manual
NFT runs safe.

## The `Commit` step cannot resolve `data/` conflicts

`git pull --rebase --autostash` in each workflow's Commit step fails on a
content conflict in the data files rather than resolving it (seen on
collect.yml run 665, 2026-09-17, caused by pushing code while runs were in
flight). Exposure is lower since `stocks.yml` stopped being a scheduled
writer, but `collect.yml` and `nft.yml` can still collide.
