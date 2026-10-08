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
