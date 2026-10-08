# Bootstrap specification

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

The repository is currently private. Verify whether stable unauthenticated public hosting is available with the current GitHub plan. If private GitHub Pages is not publicly accessible, stop and report the smallest zero/near-zero-cost alternative instead of silently introducing infrastructure.
