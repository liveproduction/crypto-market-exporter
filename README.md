# crypto-market-exporter

Minimal, database-free market data exporter for the personal crypto analysis workflow.

## Goal

Fetch public Binance Spot market data once per day, generate static machine-readable exports, and expose them over HTTPS for ChatGPT analysis.

This repository intentionally contains **no trading execution, no private Binance keys, no database, no strategy engine, no SaaS code**.

## Target datasets

- **1d**: selected Spot symbols, 5 years
- **4h**: same symbols, 90 days

## Exported fields

- source
- symbol
- timeframe
- open_time_utc
- open
- high
- low
- close
- volume
- close_time_utc
- quote_volume
- trade_count
- taker_buy_base_volume
- taker_buy_quote_volume
- taker_sell_base_volume
- taker_sell_quote_volume
- taker_buy_quote_ratio
- is_closed

## Output

```text
public/latest/
  manifest.json
  1d-part-001.txt
  ...
  4h-part-001.txt
  ...
```

Data files contain CSV text served as `text/plain`. Parts must stay below ~3.5 MB and one symbol must never be split across parts.

## Runtime target

```text
GitHub Actions -> Binance public API -> static files -> HTTPS
```

No permanent worker and no MySQL.

## Migration rule

The existing FolioEye exporter remains the reference until this exporter has been validated for several consecutive runs. Do not switch off FolioEye before parity is confirmed.

See [BOOTSTRAP_SPEC.md](BOOTSTRAP_SPEC.md) and issue #1.

## Phase 0 implementation

Python 3.11+; **no third-party dependencies**. The symbol universe is deliberately
fixed to BTCUSDT, ETHUSDT and SOLUSDT until the transport chain and FolioEye parity
have been validated. Data comes from Binance's official unauthenticated
market-data-only host, `https://data-api.binance.vision`, using `/api/v3/time` and
`/api/v3/klines`. No Binance credentials are read or sent.

```sh
python -m unittest discover -s tests -v
python exporter.py export
python exporter.py validate public/latest
python exporter.py verify-url https://liveproduction.github.io/crypto-market-exporter/latest
```

`export` captures Binance server time once, then fetches exactly 1826 complete UTC
days and 90 days of complete 4-hour candles. The current interval is excluded.
Each symbol must have every expected timestamp, in order; missing history fails
the entire export. This requires 5478 D1 rows and 1620 H4 rows in Phase 0.
`history_start` is the first candle open time and `history_end` the last candle
close time. All timestamps include milliseconds and a UTC `Z` suffix. Decimals
use Python `Decimal`; the quote buy ratio has 28 significant digits, and is `0`
when quote volume is zero. Every part has the exact CSV header and UTF-8 content.

The size target is 3,500,000 bytes including the header. Splitting keeps each
symbol together. An oversized single-symbol part is explicitly marked with
`oversized: true`; multi-symbol oversized parts are rejected. Validation reads
the written files again and checks hashes, counts, fields, derived values,
complete history, manifest consistency and symbol boundaries.

Generation happens in a temporary directory. A validation or download failure
leaves `public/latest` intact. Local replacement uses a backup and rollback;
it is intended for one CLI writer, not a live web-server folder. If a process is
interrupted during the swap, inspect `public/.latest-backup` before retrying.
Public publication deploys one complete validated Pages artifact, never a set
of individual file updates. Data files are generated artifacts and are ignored
by Git. Export logs and the workflow artifact contain the real manifest.

## GitHub Pages and daily runs

The repository must remain public on GitHub Free. In repository **Settings →
Pages**, choose **GitHub Actions** as the source. Run **Actions → Daily Binance
market export → Run workflow** to prove Phase 0. The workflow also runs daily at
07:15 UTC (GitHub scheduled runs can be delayed). It tests, exports, independently
validates, uploads the full artifact, deploys it, and downloads the public files
without authentication to check their hashes and contents. Runs are serialized.
No custom secrets are required; GitHub's built-in deployment token is used only
by the Pages actions.

The previous public manifest is compared before deployment: generation time must
advance and neither dataset's end may regress. A manual rerun within the same
candle interval is allowed with a newer generation timestamp. Only an initial
HTTP 404 means there is no previous export; other fetch failures stop the run.
Timeouts and transient errors have five attempts with exponential backoff.
HTTP 429 honors numeric `Retry-After` up to 60 seconds; a longer wait fails closed.
HTTP 403/418/451 fails immediately; no alternate host is used to bypass access
restrictions. A blocked GitHub runner means Phase 0 is not validated.

Public endpoints after the first successful deployment:

- https://liveproduction.github.io/crypto-market-exporter/latest/manifest.json
- https://liveproduction.github.io/crypto-market-exporter/latest/1d-part-001.txt
- https://liveproduction.github.io/crypto-market-exporter/latest/4h-part-001.txt

The manifest lists all parts dynamically. Check response headers and anonymous
access during the first deployment, including `text/plain` for `.txt` files.
An export/test/validation failure never starts deployment and leaves the previous
public dataset available. A smoke-test failure after deployment is reported as a
workflow failure; it does not automatically roll back an already deployed artifact.
Clients reading across a deployment should verify the manifest hashes and retry
if they encounter an old/new mismatch.

FolioEye remains running. Next: compare OHLCV, quote volume, trades, taker flows,
row counts and timestamps for BTC/ETH/SOL over several successful daily runs.
Do not extend the universe or retire FolioEye before that comparison is approved.
