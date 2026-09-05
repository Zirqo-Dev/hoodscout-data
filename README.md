# hoodscout-data

Scheduled collection and scoring of Robinhood Chain market data. Three
independent pipelines run on GitHub Actions and commit their output back to
this repo, so every snapshot is a plain file with a git history behind it.

| Pipeline | Script | Schedule | Source |
|---|---|---|---|
| Liquidity pools | `scripts/collect.py` | every 30 min | GeckoTerminal |
| Stock tokens | `scripts/stocks.py` | every 15 min | GeckoTerminal + chain RPC |
| NFT collections | `scripts/nft.py` | 13:00 and 21:00 UTC | OpenSea |

All three also accept `workflow_dispatch` for an on-demand run.

## Pools — `collect.py`

Pulls trending and new pools, aggregates them by token, and scores each on
trade flow and liquidity depth. Tokens below `MIN_SCORE_TXNS` trades are left
deliberately unscored rather than given a misleading number.

Carries `flag_contradiction`, which marks a token whose buy pressure
disagrees with its price direction — the signal and the tape pointing
opposite ways.

Writes `data/latest.json`, a dated `data/daily/YYYY-MM-DD.json`, and appends
to `data/history.jsonl` on a 7-day rolling window.

## Stock tokens — `stocks.py`

Tracks float, locked share, and premium against a pre-blackout anchor for
Robinhood Stock Tokens.

Supply is derived from GeckoTerminal's `fdv / price`, which can go stale and
fake a mint or burn. Before any MINTED/BURNED alert fires, `totalSupply()` and
`decimals()` are read directly from the chain over JSON-RPC (`eth_call`). If
the two sources disagree by more than 1%, the alert still fires but is tagged
`UNCONFIRMED` with both figures — a stale feed degrades an alert's confidence
rather than silently suppressing or inventing one. A failed RPC lookup is
tagged the same way.

The RPC endpoint defaults to `https://rpc.mainnet.chain.robinhood.com` and can
be repointed with the `RPC_URL` environment variable.

Fired alerts are written to `ALERT.txt`, which the workflow turns into GitHub
issues titled `HoodScout alert: <SYMBOL> <TYPE>` — one open issue per alert
type per symbol. An existing issue is updated in place with the latest numbers
and any duplicates are closed, so a persistent condition does not open a new
issue every run.

Writes `data/stocks_latest.json` and appends to `data/stocks.jsonl`.

## NFTs — `nft.py`

Tracks floor price, volume, sales, and owner counts for a hand-verified list
of collections, and flags a floor move of 3% or more backed by fewer than 5
sales in 24h — a floor move on almost no trades is a quote, not a signal.

Writes `data/nft_latest.json` and appends to `data/nft.jsonl` on a 30-day
rolling window. Floor deltas need two runs before they mean anything.

Each run mints its own free-tier OpenSea API key; nothing is stored.

### WATCHLIST is verified by hand

A collection slug is not derivable from its display name — `cashcatss` carries
a double S, and `stonkbrokers-434284142` a numeric suffix — so a guessed slug
silently tracks the wrong collection or a copycat. Entries are added only
after a live check:

```
python scripts/nft.py --verify <slug> [<slug> ...]
```

which prints the name, chains, floor, and supply the API actually returns.
Discovery output never feeds `WATCHLIST` automatically.

### Modes

Both extra modes are `workflow_dispatch` inputs on `nft.yml`, not part of the
schedule.

- `--discover` ranks Robinhood Chain collections by 7-day volume
  (1-day selectable) into `data/nft_discover.json`. It records
  `social_presence_score`, a count of how many of eight listing metadata
  fields are non-empty. That is all it is: every one of those fields is free
  text a copycat can fill in, so the count says nothing about whether a
  project is real. `flag_dressed_but_thin` combines a near-complete listing
  with supply concentrated in few hands and a recent creation date — the shape
  of a cloned collection rather than a young genuine one.
- `--probe` dumps raw OpenSea responses and reports which `order_by` values
  the API accepts. Field naming has shifted across OpenSea API versions, so
  this exists to read the live shape instead of assuming it.

## Data files

| File | Written by | Contents |
|---|---|---|
| `data/latest.json` | `collect.py` | current pool/token snapshot |
| `data/daily/*.json` | `collect.py` | one snapshot per day |
| `data/history.jsonl` | `collect.py` | 7-day rolling token history |
| `data/stocks_latest.json` | `stocks.py` | current stock token snapshot |
| `data/stocks.jsonl` | `stocks.py` | stock token history |
| `data/nft_latest.json` | `nft.py` | current NFT snapshot |
| `data/nft.jsonl` | `nft.py` | 30-day rolling NFT history |
| `data/nft_discover.json` | `nft.py --discover` | surfaced collections, on demand |

Nothing here is financial advice, and none of these flags is a verdict. They
mark shapes worth a second look.
