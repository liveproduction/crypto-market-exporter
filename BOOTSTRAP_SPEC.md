# Bootstrap specification

Phase 0 below is the original validated baseline. V1.1 extends its universe as
described at the end of this document; the architecture and CSV columns remain unchanged.

## Phase 0 — feasibility first

Before building the full exporter, prove the complete transport chain with only:

- BTCUSDT
- ETHUSDT
- SOLUSDT

Datasets:
- 1d / 5 years
- 4h / 90 days

The first milestone is successful only when:
1. GitHub Actions can fetch Binance public klines.
2. Generated files are published at a stable HTTPS URL.
3. `manifest.json` and all data files can be read externally without authentication.
4. ChatGPT can ingest those URLs.

Do **not** expand to the complete symbol universe before this is proven.

## Binance

Use Binance public Spot market data only. No API key, secret, signing, account endpoints, orders or wallet data.

Handle pagination and rate limits correctly. Retry transient failures with bounded exponential backoff.

## Data contract

One row per CLOSED kline.

Columns, in this exact order:

```text
source,symbol,timeframe,open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trade_count,taker_buy_base_volume,taker_buy_quote_volume,taker_sell_base_volume,taker_sell_quote_volume,taker_buy_quote_ratio,is_closed
```

Derived raw-flow fields:

```text
taker_sell_base_volume  = volume - taker_buy_base_volume
taker_sell_quote_volume = quote_volume - taker_buy_quote_volume
taker_buy_quote_ratio   = taker_buy_quote_volume / quote_volume
```

Guard division by zero.

Use UTC ISO-8601 timestamps.

## Output splitting

- CSV body, `.txt` extension.
- UTF-8.
- Header repeated in every part.
- Target max size: 3.5 MB.
- Dynamic number of parts.
- Never split one symbol across two parts.
- Atomic publication: never expose a mixed old/new dataset.

## Manifest

Generate `public/latest/manifest.json`.

Required concepts:

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "status": "complete",
  "source": "BINANCE_PUBLIC_SPOT",
  "datasets": {
    "1d": {
      "history_days": 1826,
      "symbols_count": 3,
      "rows": 0,
      "history_start": "...",
      "history_end": "...",
      "parts_count": 0,
      "parts": []
    },
    "4h": {
      "history_days": 90,
      "symbols_count": 3,
      "rows": 0,
      "history_start": "...",
      "history_end": "...",
      "parts_count": 0,
      "parts": []
    }
  }
}
```

Each part should include:
- part number
- filename
- bytes
- rows
- symbols_count
- symbols
- sha256

## Validation

Before publication validate:
- no duplicate (symbol, timeframe, open_time)
- OHLC sanity
- non-negative volume/trade count
- only closed candles
- expected symbols all present
- no symbol split between parts
- sum of part rows == dataset rows
- every manifest SHA-256 matches its file
- latest dataset is newer than the previous successful run

Fail closed for publication: if generation/validation fails, leave the previous valid `latest` intact.

## Schedule

Daily GitHub Action around 07:15 UTC.

Also support manual `workflow_dispatch`.

## Language/runtime

Prefer Python 3 standard library plus minimal dependencies. Keep the project framework-free.

## Explicit non-goals

Do not add:
- database
- Docker unless technically necessary
- Apache/Nginx
- trading strategies
- RSI/MACD/Ichimoku/ATR/OBV in V1
- AI/OpenAI calls
- account/wallet endpoints
- private Binance credentials
- trading/execution
- UI/dashboard
- backtests

## Publication caveat

The repository is now public. GitHub Pages and the dedicated `data` branch are
both enabled and validated. Preserve both publication paths.

## V1.1 — production extension

Phase 0 was independently read and validated through the GitHub connector at
data commit `465afef039c5e1f7043799bc9d83f3e30699f93e`, generated at
`2026-10-08T20:16:14.658Z`. Do not repeat that feasibility milestone.

- Use exactly the 47 ordered Binance Spot symbols in `config/symbols.json`.
- Request 1826 days of 1D and 90 days of 4H; export all available closed candles.
- Recent listings need not have a full five-year history, but every symbol must
  have at least one valid closed candle in each dataset.
- Preserve duplicate/OHLC/volume/closure validation and reject internal gaps in
  each available symbol history. Record actual dates and row counts per symbol.
- Extend dataset diagnostics with requested/exported/failed symbols, symbol
  errors, per-symbol rows/history and requested history bounds. Preserve schema
  version 1 and the existing CSV field semantics.
- An incomplete attempt must set `status: incomplete`, identify failed symbols,
  retain a diagnostic manifest outside `public/`, fail visibly and leave the
  previous complete latest snapshot unchanged on both publication paths.
- Keep sequential paced pagination, bounded retries, dynamic splitting at
  3,500,000 bytes, intact symbol groups and oversized single-symbol flags.
- Preserve Pages and the atomic replacement of the single-root-commit `data`
  snapshot. Publish the exact same validated files through both paths.
- After a full manual workflow, resolve `data` to a SHA and validate every actual
  part, hashes, row counts, 47-symbol universe, headers and closed candles at that SHA.
- Keep FolioEye running. Prepare a latest-common-D1 comparison sample containing
  BTCUSDT, ETHUSDT, SOLUSDT, MORPHOUSDT, GMXUSDT and AEROUSDT.
- Stop after V1.1 validation; do not add indicators or new infrastructure.
