# binance-dune-pusher

Pushes Binance spot **1-second OHLC** data for a rolling **7-day window** to
Dune, every day via GitHub Actions. One table per pair.

Each run uploads the last 7 *complete* UTC days (the current day is ignored) and
**replaces** each Dune table, so every day the window rolls forward one day
("drop the oldest, add the newest") automatically — no dedup, no partial days.
Symbols not listed on Binance are **skipped** (logged, not failed), so you can
keep pending pairs in the list and they auto-activate once Binance lists them.

## Pairs

| Pair | Binance symbol | Table |
|---|---|---|
| SOL-USDC | `SOLUSDC` | `binance_solusdc_1s_ohlc_7d` |
| USDC-USDT | `USDCUSDT` | `binance_usdcusdt_1s_ohlc_7d` |
| SOL-USDT | `SOLUSDT` | `binance_solusdt_1s_ohlc_7d` |
| USDC-USD1 | `USD1USDC` ⚠️ | `binance_usd1usdc_1s_ohlc_7d` |
| SOL-USD1 | `SOLUSD1` | `binance_solusd1_1s_ohlc_7d` |
| PUMP-USDC | `PUMPUSDC` | `binance_pumpusdc_1s_ohlc_7d` |
| ZEC-USDC | `ZECUSDC` | `binance_zecusdc_1s_ohlc_7d` |
| HYPE-USDC | `HYPEUSDC` | `binance_hypeusdc_1s_ohlc_7d` |
| BONK-USDC | `BONKUSDC` | `binance_bonkusdc_1s_ohlc_7d` |
| PENGU-USDC | `PENGUUSDC` | `binance_penguusdc_1s_ohlc_7d` |
| TRUMP-USDC | `TRUMPUSDC` | `binance_trumpusdc_1s_ohlc_7d` |

⚠️ **USD1USDC** is listed by Binance in reverse: its price is **USDC per USD1**
(≈1.0), i.e. the inverse of "USDC-USD1". If you need USDC-as-base, use `1/price`.

Not on Binance (skipped automatically): **CASH-USDC**, **cbBTC-USDC**,
**USELESS-USDC**, **FARTCOIN-USDC**. They're left commented in the script's
default list so they turn on by themselves if Binance ever lists them.

## Setup (one thing to configure)

1. Create a GitHub repo and add these files (`push_binance_to_dune.py`,
   `.github/workflows/push-binance-dune.yml`, this README).
2. Get a Dune API key: Dune → **Settings → API** → create key.
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, name it **`DUNE_API_KEY`**, paste the key.

That's it. The workflow runs daily at **06:00 UTC**, and you can trigger it
manually any time from the **Actions** tab (**Run workflow**).

> Tip: run it once manually after adding the secret to create the table and
> confirm everything works.

## The Dune tables

- One table per pair: **`binance_<symbol>_1s_ohlc_7d`** (see the Pairs table).
- Query as **`dune.<your_handle>.binance_solusdc_1s_ohlc_7d`** etc. (replace
  `<your_handle>` with your Dune username; confirm the exact path under Dune →
  **Data → your uploads** after the first run).
- API uploads have **no `dataset_` prefix** (that's only for the Dune UI's
  manual upload), so point queries at `dune.<handle>.binance_solusdc_1s_ohlc_7d`.

Columns (one row per second):

| column | type | notes |
|---|---|---|
| `open_time_ms` | bigint | start of the 1s window, epoch **ms**, UTC |
| `datetime_utc` | timestamp | `YYYY-MM-DD HH:MM:SS` UTC |
| `open` / `high` / `low` / `close` | double | trade prices in the second (USDC/base) |
| `volume` | double | base-asset volume (e.g. SOL) |
| `quote_volume` | double | quote volume (USDC) — VWAP = `quote_volume/volume` |
| `trades` | int | number of trades; `0` = no trade, prices carried forward |

Sanity check in Dune:

```sql
select min(datetime_utc) as first_sec,
       max(datetime_utc) as last_sec,
       count(*)          as rows_
from dune.<your_handle>.binance_solusdc_1s_ohlc_7d;
-- expect 7 * 86400 = 604800 rows, spanning 7 full UTC days
```

## Add more pairs later

Edit `SYMBOLS` in the workflow, comma-separated:

```yaml
SYMBOLS: "SOLUSDC,ZECUSDC,PUMPUSDC,TRUMPUSDC"
```

Each symbol is pushed to its own table `binance_<symbol>_1s_ohlc_7d`.

## Configuration (env vars, all optional except the key)

| var | default | meaning |
|---|---|---|
| `DUNE_API_KEY` | — | **required** (set as a GitHub secret) |
| `SYMBOLS` | `SOLUSDC` | comma-separated Binance symbols |
| `DAYS` | `7` | window length in days |
| `TABLE_PREFIX` / `TABLE_SUFFIX` | `binance_` / `_1s_ohlc_7d` | table naming |
| `DUNE_IS_PRIVATE` | `false` | make the Dune table private |

## Run locally

```bash
DUNE_API_KEY=xxxxxxxx python push_binance_to_dune.py
# or a custom window / pairs:
DUNE_API_KEY=xxxxxxxx SYMBOLS="SOLUSDC,ZECUSDC" DAYS=7 python push_binance_to_dune.py
```

Python 3.9+ (uses only the standard library — no `pip install`).

## Notes

- **Source & integrity:** primary source is Binance's daily dumps
  (`data.binance.vision`) with SHA-256 checksum verification; if a day's dump
  isn't published yet, it falls back to Binance's public REST endpoint
  (`data-api.binance.vision`) for that day, so a run won't fail on publish lag.
- **GitHub cron caveat:** scheduled workflows are paused after ~60 days of no
  repo activity — push a commit occasionally, or the daily run stops.
- **Scaling:** the CSV upload endpoint handles the single-pair ~7-day size
  comfortably. If you later push many pairs or a much longer window and approach
  Dune's upload size limit, switch to the create/clear/insert table API and
  insert one day per request.
