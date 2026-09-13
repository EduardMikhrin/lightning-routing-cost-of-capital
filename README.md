# lightning-routing-cost-of-capital

**Eduard Mikhrin** ([0009-0002-6104-4368](https://orcid.org/0009-0002-6104-4368))
National Technical University of Ukraine "Igor Sikorsky Kyiv Polytechnic
Institute", Kyiv, Ukraine

Reproducible data collection for a paper on the economics of a Lightning Network
routing node treated as an investment. Every number the paper prints traces back
to a raw API response stored here with a timestamp and a SHA-256 hash.

The design rule throughout: **an absent number stays absent.** No fallbacks, no
mock data, no "reasonable defaults". A source that does not answer stops the run
with an explicit error, and a limitation that cannot be worked around is written
down in this file instead of being papered over.

---

## What is in the repository

```
config.yaml            every model parameter and every fetch setting
CITATION.cff           machine-readable citation metadata
LICENSE                MIT, covers scripts/
LICENSE-DATA           CC BY 4.0, covers data/
scripts/
  common.py            HTTP with retries, immutable raw storage, manifest, hash checks
  fetch_mempool.py     mempool.space public REST API   (no key)
  fetch_amboss.py      api.amboss.space GraphQL         (key optional, see below)
  fetch_price.py       CoinGecko BTC/USD and BTC/UAH    (no key)
  build_tables.py      the model; reads raw, writes derived CSV + the figure
data/raw/              API responses byte-for-byte, never edited, never overwritten
data/raw/manifest.jsonl  one line per fetch: source, url, time, status, file, sha256
data/derived/          the CSV tables the paper cites
figures/               the figure, plus the CSV it was plotted from
```

### How data integrity is enforced

1. **Non-200 is fatal.** `common._request` retries only what a retry can fix
   (transport errors, 429, 5xx). A 4xx is reported immediately with the response
   body — it means the request was wrong, and retrying would hide that.
   A GraphQL 200 carrying an `errors` array is also treated as a failure.
2. **Raw files are append-only.** Filenames carry a UTC stamp
   (`<source>_<endpoint>_<YYYY-MM-DDTHH-MM-SSZ>.json`) and are created with
   `open(..., "xb")`, so an existing file is never overwritten.
3. **Every write is hashed.** `data/raw/manifest.jsonl` records the SHA-256 of
   the bytes on disk. `build_tables.py` re-hashes each file before reading it and
   refuses to build if a byte changed, so a hand-edited snapshot can never reach
   a published table.
4. **All timestamps are ISO 8601 UTC.**

---

## Setup

Python 3.11+.

```bash
uv venv && uv pip install -r requirements.txt
```

or, without uv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### API key (optional)

Only Amboss involves a key, and at the last run every query answered without
one. Set it to raise your rate limit and to be an identifiable client:

1. Sign in at <https://amboss.space>, open the account panel, and create a key
   under **API Keys**.
2. `cp .env.example .env` and fill in `AMBOSS_API_KEY=...`.

`.env` is in `.gitignore` and must never be committed. Also edit
`http.user_agent` in `config.yaml` if you fork this — it identifies the client
to the public APIs and should point at your own repository, not this one.

### Full run — one command

```bash
make snapshot PYTHON=.venv/bin/python
```

That is `fetch` (all three collectors, writing new raw snapshots and manifest
lines) followed by `tables` (the model, the CSVs and the figure). `make
clean-derived` removes derived output only; raw snapshots are never deleted by
any target.

---

## Sources

| Source | Endpoint | What is taken | Limitations / what is not available |
|---|---|---|---|
| mempool.space | `GET /api/v1/fees/recommended` | on-chain fee tiers, integer sat/vB | Rounds up: in a near-empty mempool every tier reads `1`. |
| mempool.space | `GET /api/v1/fees/precise` | same tiers down to 0.1 sat/vB | Captured for sensitivity. Switch with `model.channel.fee_source`. |
| mempool.space | `GET /api/v1/lightning/statistics/latest` | total capacity, node and channel counts, avg/median channel capacity, avg/median fee rate (ppm) and base fee (msat) | The row's own `added` date lags the fetch by up to ~2 weeks. `build_tables.py` reports it as `data_as_of_utc`; cite that, not the fetch time. |
| mempool.space | `GET /api/v1/lightning/statistics/3y` | historical series for the dynamics section | **The fee fields do not exist on the historical endpoint** — only `channel_count`, `total_capacity` and the node counts. mempool.space therefore publishes **no historical ppm or base-fee series at all**; there is no substitute endpoint. Documented intervals stop at `3y` (`latest\|24h\|3d\|1w\|1m\|3m\|6m\|1y\|2y\|3y`), so 3 years is the longest history the published contract offers. `fetch_mempool.py` rejects any other value rather than probing undocumented ones. |
| mempool.space | `GET /api/v1/lightning/nodes/rankings` | headline top nodes by capacity and by channel count | Returns only ~6 entries per list. |
| mempool.space | `GET /api/v1/lightning/nodes/rankings/liquidity` `.../connectivity` | top 100 by capacity and by channel count | Top 100 only; no full-network node dump is published. |
| api.amboss.space | `getOffers(offerType: CHANNEL)` | live open channel-lease offers: `base_fee`, `fee_rate`, `min_size`, `max_size`, `min_block_length` — the ask side of the Magma market | Excludes `amboss_fee_rate` (the marketplace's own fee), whose unit is not documented, so the offer-side rate **understates the buyer's all-in cost**. Manual substitute: read the total price off a Magma checkout in the web UI and compare. |
| api.amboss.space | `getMarketMetrics.order_details(from:)` | executed leases: `size`, `block_duration`, `lnr`, `status` — the trade side | Carries no explicit price field, only `lnr`. On a free-tier key the query answers *"Cannot query data older than 7 days"*; anonymously it returns the full window, so `sources.amboss.authenticate.magma_order_details: false` deliberately omits the header. Flip it to `true` if your tier lifts the window. |
| api.amboss.space | `getMarketMetrics.lnr_series(from:, period:)` | Amboss's published aggregate LNR index | Same 7-day authenticated window as above; same config switch. |
| api.amboss.space | `getMarketMetrics.lnr_curve_buckets` | rate term structure by channel-size bucket | Collected and stored; not yet consumed by a table. |
| api.amboss.space | `getRankLists` | the node universe for the fee-rate distribution | **Returns only the top 20 pubkeys per list.** That is the ceiling on the distribution's sample, not a sampling choice. |
| api.amboss.space | `getNode(pubkey).graph_info.fee_buckets` | per-node histogram of outbound fee rates in ppm | **Amboss publishes fee rates only as a 25-bucket histogram**, never as raw per-channel ppm values, so percentiles are interpolated inside the containing bucket and the top bucket (`10000 – Infinity`) is open-ended. Of the 20 ranked nodes, 3 return an empty `local_buckets` array and contribute nothing; the effective sample is recorded in `ppm_distribution.csv` and the skipped pubkeys in `_sources.json`. |
| api.coingecko.com | `GET /api/v3/simple/price` | BTC/USD and BTC/UAH at the snapshot instant | Spot only — the free plan's historical endpoints are not used, so a rebuild against an older raw file must use that file's own quote. |

### Not available from any source in this repository

- **Realised routing revenue and utilisation.** No public API publishes what a
  node actually forwarded. `u` (daily routed volume / locked capital) is
  therefore an **assumption**, swept across the whole `[0, 1]` grid in
  `sensitivity.csv` rather than asserted at one value. The manual substitute is
  an operator's own `lnd` forwarding history (`lncli fwdinghistory`), which has
  to be supplied outside this repository.
- **Historical network-wide fee rates.** See the `statistics/3y` row above.
- **Channel lifetime.** `model.channel.lifetime_months` is an assumption; it
  would take a channel-close dataset to observe.

---

## The model

```
APY_routing = (F_earned - F_rebalancing - OnChain_amortised - OPEX) / K_locked

V             = u * K_locked * 365                     annual routed volume
F_earned      = ppm * 1e-6 * V
F_rebalancing = r_reb * F_earned
OnChain_amort = (K_locked / channel_size)
                * (open_vbytes + close_vbytes) * sat_per_vB
                * (12 / L_months)
OPEX          = opex_usd_per_year / BTCUSD
```

Every symbol is a `config.yaml` entry. Values are labelled there as OBSERVED
(overwritten from the snapshot at build time) or ASSUMPTION (the author's
choice, to be defended in the text). `channel_size` defaults to the observed
median channel capacity from mempool.space; `sat_per_vB` and `BTCUSD` come from
the snapshot.

APY is affine in `u`, so break-even utilisation is solved in closed form:

```
u* = (coc + (OnChain_amort + OPEX) / K_locked) / (ppm * 1e-6 * 365 * (1 - r_reb))
```

`build_tables.py` re-evaluates the model at `u*` and aborts if it does not land
on the cost-of-capital line, so the two cannot drift apart.

### Output

| File | Contents |
|---|---|
| `data/derived/table2_cost_of_capital.csv` | price of capital, annualised: own BTC (observed — read off one of the rows below, see `cost_of_capital.own_btc_opportunity`), Magma open offers (p25/median/p75/p90), executed Magma leases (p25/median/p75/p90), and the Amboss LNR index. Columns: `source, observed_rate_annual_pct, basis, snapshot_date, note`. |
| `data/derived/table3_scenarios.csv` | one row per (capacity, ppm) pair from `model.scenarios`, every term broken out in satoshis, plus break-even utilisation and whether it is reachable. |
| `data/derived/onchain_sensitivity.csv` | the same scenario grid re-run at several on-chain fee levels. The observed tier is the base case (`basis = observed`); the rest are stress scenarios (`basis = stress`), not observations. `clears_benchmark` flags rows at or above the cost of capital. |
| `data/derived/elasticity_sensitivity.csv` | the same grid with utilisation responding to the fee rate, `u(ppm) = u0 · (ppm/ppm_ref)^(−ε)`, swept over `elasticity.epsilon_list`. `ε = 0` is the inelastic case and reproduces `table3_scenarios.csv` exactly — the build asserts it. |
| `data/derived/sensitivity.csv` | APY over the `u` × `ppm` grid, with `break_even_u` and a flag for whether it is reachable at `u ≤ 1`. |
| `data/derived/ppm_distribution.csv` | outbound fee-rate percentiles plus the network-wide mempool.space comparison, and the sample-coverage rows. |
| `data/derived/ppm_histogram.csv` | the merged bucket histogram the percentiles were interpolated from. |
| `data/derived/network_snapshot.csv` | headline Lightning figures with units and `data_as_of_utc`. |
| `data/derived/lightning_history.csv` | the 3-year series. |
| `data/derived/_sources.json` | every raw file this build consumed, with hashes, plus the resolved model inputs. |
| `figures/fig1_apy_vs_utilization.png` | APY vs utilisation, one curve per ppm, cost-of-capital line, break-even markers. |
| `figures/fig1_apy_vs_utilization.csv` | the exact data the figure was plotted from. |

Two rates in `table2` are computed differently and the difference matters. The
offer side is annualised by this repository from the posted terms
(`base_fee + size * fee_rate / 1e6` over `min_block_length`, scaled to a
52,560-block year). The trade side uses Amboss's `lnr` **as returned** — LNR is
defined as the cost of a channel over a one-year term of ~52,560 blocks, so it
is already annualised and is not re-scaled here.

### Figure conventions

300 dpi, 16.0 × 10.0 cm (an A4 text block with ~2.5 cm margins), serif labels at
9 pt, and curves separated by line style *and* marker so the plot survives
black-and-white printing. The underlying values ship alongside as CSV.

---

## Headline results

Every figure in this section is regenerated by `build_tables.py` from the CSVs
below and injected between the markers — it cannot drift from the snapshot. The
reading of those figures, after the markers, is written by hand.

Sources: [`table3_scenarios.csv`](data/derived/table3_scenarios.csv),
[`onchain_sensitivity.csv`](data/derived/onchain_sensitivity.csv),
[`elasticity_sensitivity.csv`](data/derived/elasticity_sensitivity.csv).

<!-- BEGIN generated: headline -->
Snapshot `2026-09-13`. Benchmark **2.37 %/yr** (`magma_orders_median`, n=3265) — the median annualised rate on executed Magma leases, i.e. the observed price of renting the same liquidity. Baseline utilisation `u = 0.15`, on-chain fees at the observed 1 sat/vB.

| Capacity | ppm | APY | vs benchmark | break-even `u*` | reachable at `u ≤ 1` |
|---:|---:|---:|---:|---:|:--|
| 0.1 BTC | 100 (network median) | -3.52 % | -5.89 pp | 2.46 | **no** |
| 0.1 BTC | 464 (ranked-node median) | -2.13 % | -4.50 pp | 0.53 | yes |
| 0.1 BTC | 1000 (assumption) | -0.07 % | -2.45 pp | 0.25 | yes |
| 1 BTC | 100 (network median) | -0.02 % | -2.39 pp | 1.08 | **no** |
| 1 BTC | 464 (ranked-node median) | +1.38 % | -0.99 pp | 0.23 | yes |
| 1 BTC | 1000 (assumption) | **+3.43 %** | **+1.06 pp** | 0.11 | yes |
| 10 BTC | 100 (network median) | +0.33 % | -2.04 pp | 0.95 | yes |
| 10 BTC | 464 (ranked-node median) | +1.73 % | -0.64 pp | 0.20 | yes |
| 10 BTC | 1000 (assumption) | **+3.78 %** | **+1.41 pp** | 0.09 | yes |

**2 of 9** scenarios clear the benchmark at `u = 0.15`. Fixed OPEX of $300/yr is 389,570 sat at the snapshot rate, which is **3.9 %** of a 0.1 BTC node's capital (389,570 / 10,000,000 sat).

### Under a busier fee market

| ppm | 1 sat/vB (observed) | 50 sat/vB (stress) | 200 sat/vB (stress) |
|---:|---:|---:|---:|
| 100 | +0.33 % | -0.18 % | -1.75 % |
| 464 | +1.73 % | +1.22 % | -0.35 % |
| 1000 | +3.78 % | +3.27 % | +1.70 % |

(best case at each fee level: the 10 BTC node.) Scenarios clearing the benchmark, by fee level: **2/9** at 1 sat/vB, **2/9** at 50 sat/vB, **0/9** at 200 sat/vB.

### If demand responds to price

`u(ppm) = u0 · (ppm / 100)^(−ε)` with `u0 = 0.15`, so gross revenue scales as `ppm^(1−ε)` and ε = 1 is the pivot. APY for the 10 BTC node, with the count over all 9 scenarios:

| ε | 100 ppm | 464 ppm | 1000 ppm | clearing benchmark |
|---:|---:|---:|---:|---:|
| 0 | +0.33 % | +1.73 % | +3.78 % | 2/9 |
| 0.5 | +0.33 % | +0.78 % | +1.16 % | 0/9 |
| 1 | +0.33 % | +0.33 % | +0.33 % | 0/9 |
| 1.5 | +0.33 % | +0.13 % | +0.07 % | 0/9 |
<!-- END generated: headline -->

### What this means

**The result is a fee-policy result, and it is fragile in three directions.**

At both *observed* fee medians — the network median and the median across
Amboss-ranked nodes — routing returns less than simply leasing the same capital
out on Magma. Only an aggressive fee policy clears the benchmark, and only on the
larger two node sizes.

*Fixed OPEX punishes small nodes.* The OPEX share above is computed from the
`opex_sat` column against locked capital, so it moves with the BTC price rather
than being restated here. At that share, every 0.1 BTC scenario is negative
regardless of fee policy; at the network median the required utilisation exceeds
1.0, which is not merely unreachable but physically meaningless.

*Scale saturates early.* The 1 → 10 BTC step adds roughly 0.35 pp, because OPEX
is the only term that does not scale with capacity. There is no third act to
growing the node.

*On-chain fees were a non-factor here, and that is an artefact of the snapshot.*
The mempool was near-empty. Re-run at 200 sat/vB, **no scenario clears the
benchmark** — the best two cases fall to roughly +1.4 % and +1.7 % against a
2.37 % benchmark. At 50 sat/vB only the two aggressive-fee scenarios survive, and
the mid fee rate at 1 BTC drops below 1 %. The 50 and 200 sat/vB columns are
assumptions about a fee market this snapshot did not see, labelled
`basis = stress` in the CSV; the count of clearing scenarios per fee level is the
`clears_benchmark` column.

**The elasticity sweep is the load-bearing caveat.** Everywhere else this model
holds `u` independent of `ppm` — perfectly inelastic demand for forwarding — and
that single assumption is what makes "raise fees" look like free money. Let
volume respond to price and gross revenue becomes proportional to `ppm^(1−ε)`,
so unit elasticity is the pivot and the sweep traces three distinct regimes:

- **ε < 1 — fees still pay, but not enough.** Raising the fee rate still raises
  revenue, and APY still climbs across the fee grid. The *direction* of the
  inelastic result survives; only its magnitude collapses, and what remains
  falls short of the benchmark.
- **ε = 1 — fee policy stops mattering at all.** Volume lost exactly offsets
  rate gained, revenue is invariant to `ppm`, and APY is identical at every fee
  rate. The sweep reproduces this exactly rather than approximately, which is a
  useful check that the curve is specified correctly.
- **ε > 1 — raising fees destroys revenue.** The ordering inverts and the best
  policy becomes the *lowest* fee rate in the grid. "Raise fees" is not merely
  insufficient here, it is the wrong direction.

So no scenario clears the benchmark once ε ≥ 0.5, but not for a single reason:
below unit elasticity because a fee increase cannot close a ~2 pp gap, above it
because the fee increase is self-defeating. Only the ε > 1 regime involves
revenue being given back in units not routed — that mechanism does not apply at
ε = 0.5, where revenue is still rising with the fee rate.

The ε = 0 column reproduces the main scenario table exactly, and the build
asserts that identity per scenario rather than trusting it.

No elasticity of forwarding demand is observable from any public API, so ε is an
assumption about behaviour, not a measurement — as is `u` itself. The honest
statement of the headline result is conditional: *a routing node clears the
observed cost of capital only under an aggressive fee policy, a quiet on-chain
fee market, and demand that barely responds to price.* Drop any one of those and
it does not.

---

## Snapshot

<!-- BEGIN generated: snapshot -->
**Snapshot `2026-09-13`** (ISO 8601; the files this build used were collected `2026-09-13T15:21:47Z` – `2026-09-13T15:22:20Z`). The mempool.space network totals inside it carry their own row date, `2026-08-30`.
<!-- END generated: snapshot -->

`data/raw/manifest.jsonl` records the exact time and hash of every file ever
collected; `data/derived/_sources.json` lists the subset the current tables were
built from. The network-totals date above is mempool.space's own `added` field,
and that is the one to cite for network figures — it lags collection by up to a
fortnight.

`data/raw/` also holds one `lightning_statistics_5y` file from an earlier run.
`5y` is not a documented interval; the endpoint answered it, but the repository
no longer asks for it (see the `statistics/3y` row above). The file stays because
raw snapshots are append-only — nothing in `data/derived/` is built from it.

> **Lightning Network data changes daily.** Capacity, channel counts, fee
> policies and Magma lease rates all move from one day to the next, and the
> on-chain fee market moves within a single day. Figures quoted from this
> repository are valid for their snapshot date and no other. Re-running `make
> snapshot` produces new files next to the old ones rather than replacing them,
> so an earlier build stays reproducible; point `build_tables.py` at an older
> snapshot by trimming `manifest.jsonl` to the run you want.

---

## Reproducibility

<!-- BEGIN generated: snapshot -->
**Snapshot `2026-09-13`** (ISO 8601; the files this build used were collected `2026-09-13T15:21:47Z` – `2026-09-13T15:22:20Z`). The mempool.space network totals inside it carry their own row date, `2026-08-30`.
<!-- END generated: snapshot -->

Requires Python 3.11+ and a network connection only for the second path below.

### Rebuild the published tables from the archived snapshot — no network

This is the path that reproduces the numbers in the paper. It reads the raw
files committed under `data/raw/`, re-hashes each one against
`data/raw/manifest.jsonl`, and regenerates every CSV and the figure. If any raw
byte differs from what was collected, it aborts instead of producing a table.

```bash
git clone https://github.com/EduardMikhrin/lightning-routing-cost-of-capital.git
cd lightning-routing-cost-of-capital
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make tables PYTHON=.venv/bin/python
```

With uv, the last two steps collapse to:

```bash
uv venv && uv pip install -r requirements.txt
make tables PYTHON=.venv/bin/python
```

Output: `data/derived/*.csv`, `data/derived/_sources.json`,
`figures/fig1_apy_vs_utilization.png` and its `.csv`. Runs in a few seconds.

### Collect a fresh snapshot instead

```bash
cp .env.example .env        # optional: add AMBOSS_API_KEY
make snapshot PYTHON=.venv/bin/python
```

This runs all three collectors and then rebuilds the tables. Takes roughly a
minute — `fetch_amboss.py` issues one request per ranked node with a
configurable pause between requests (`http.pause_between_requests_seconds`).

**A fresh snapshot will not reproduce the numbers above.** New raw files are
written alongside the old ones under new timestamps, nothing is overwritten, and
`build_tables.py` then uses the newest file per endpoint — so the tables move to
the new snapshot. To go back to an archived one, trim `manifest.jsonl` to the
run you want. See the volatility note under *Snapshot*.

### What makes it reproducible

Raw responses are stored byte-for-byte and never edited; filenames and manifest
records carry UTC timestamps; every file is hashed at collection time and
re-verified at build time; and every model parameter lives in `config.yaml`
rather than in code, so the inputs behind any figure are the snapshot plus one
versioned config file.

---

## Citation

Machine-readable metadata is in [CITATION.cff](CITATION.cff). To cite the
dataset and scripts:

> Mikhrin E. (2026). Lightning routing cost of capital: dataset and collection
> scripts (v1.0) [Data set]. National Technical University of Ukraine "Igor
> Sikorsky Kyiv Polytechnic Institute". https://orcid.org/0009-0002-6104-4368

---

## License

- Code (`scripts/`): MIT License — see [LICENSE](LICENSE)
- Data (`data/`): CC BY 4.0 — see [LICENSE-DATA](LICENSE-DATA)

Data collected from public APIs (mempool.space, Amboss, CoinGecko).
Terms of the respective providers apply to the underlying data.

CC BY 4.0 covers this dataset as a work — the selection, collection and
arrangement of the snapshots plus the derived tables — not the third-party
content inside `data/raw/`, which stays under each provider's own terms. Check
those before redistributing: mempool.space (<https://mempool.space/about>),
Amboss (<https://amboss.space>, account terms), CoinGecko
(<https://www.coingecko.com/en/api/terms>) — the free plan requires attributing
CoinGecko as the price source.

Attribute all three sources in the paper's *Data and methods* section, with the
snapshot date above.
