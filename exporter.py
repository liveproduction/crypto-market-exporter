"""Phase 0 Binance Public Spot exporter. Python 3.11+, standard library only."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOURCE = "BINANCE_PUBLIC_SPOT"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DAY = 86_400_000
DATASETS = {"1d": (1826, DAY), "4h": (90, DAY // 6)}
MAX_BYTES = 3_500_000
BASE_URL = "https://data-api.binance.vision"
FIELDS = "source,symbol,timeframe,open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trade_count,taker_buy_base_volume,taker_buy_quote_volume,taker_sell_base_volume,taker_sell_quote_volume,taker_buy_quote_ratio,is_closed".split(",")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def iso(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def millis(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset() == timezone.utc.utcoffset(parsed), "Timestamp must be UTC")
    result = round(parsed.timestamp() * 1000)
    require(iso(result) == value, "Timestamp must use canonical UTC ISO-8601")
    return result


def number(value):
    result = Decimal(str(value))
    require(result.is_finite(), "Non-finite decimal")
    return result


def integer(value):
    result = int(value)
    require(str(result) == str(value), "Invalid integer")
    return result


def decimal_text(value):
    return format(value, "f")


def get_bytes(url, attempts=5):
    """Bounded retries; never retry access restrictions or circumvent them."""
    for attempt in range(attempts):
        retry_after = 0
        try:
            request = Request(url, headers={"User-Agent": "crypto-market-exporter/1.0"})
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
            try:
                retry_after = float(error.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            # Do not retry earlier than Binance asks, or wait indefinitely.
            if retry_after > 60 or attempt == attempts - 1:
                raise
        except (URLError, TimeoutError, socket.timeout):
            if attempt == attempts - 1:
                raise
        time.sleep(max(retry_after, min(2 ** attempt, 30)))
    raise RuntimeError("Retries exhausted")


def api(path, **params):
    return json.loads(get_bytes(BASE_URL + path + ("?" + urlencode(params) if params else "")))


def parse_kline(raw, symbol, timeframe, cutoff):
    require(isinstance(raw, list) and len(raw) == 12, "Invalid Binance kline")
    opened, closed = integer(raw[0]), integer(raw[6])
    require(closed >= opened, "Invalid candle times")
    if closed >= cutoff:
        return None
    volume, quote, buy_base, buy_quote = (number(raw[i]) for i in (5, 7, 9, 10))
    values = [SOURCE, symbol, timeframe, iso(opened),
              *(decimal_text(number(raw[i])) for i in (1, 2, 3, 4, 5)),
              iso(closed), decimal_text(quote), str(integer(raw[8])),
              decimal_text(buy_base), decimal_text(buy_quote),
              decimal_text(volume - buy_base), decimal_text(quote - buy_quote),
              decimal_text(buy_quote / quote) if quote else "0", "true"]
    row = dict(zip(FIELDS, values))
    validate_row(row, cutoff)
    return row


def validate_row(row, cutoff):
    require(list(row) == FIELDS, "Invalid columns/order")
    require(row["source"] == SOURCE and row["symbol"] in SYMBOLS, "Invalid source/symbol")
    require(row["timeframe"] in DATASETS, "Invalid timeframe")
    opened, closed = millis(row["open_time_utc"]), millis(row["close_time_utc"])
    interval = DATASETS[row["timeframe"]][1]
    require(opened % interval == 0 and closed == opened + interval - 1, "Invalid interval")
    require(closed < cutoff and row["is_closed"] == "true", "Open candle")
    o, h, low, c = (number(row[key]) for key in ("open", "high", "low", "close"))
    require(0 < low <= min(o, c) <= max(o, c) <= h, "Invalid OHLC")
    keys = ("volume", "quote_volume", "taker_buy_base_volume", "taker_buy_quote_volume",
            "taker_sell_base_volume", "taker_sell_quote_volume")
    volume, quote, buy_base, buy_quote, sell_base, sell_quote = (number(row[key]) for key in keys)
    require(all(number(row[key]) >= 0 for key in keys), "Negative volume")
    require(integer(row["trade_count"]) >= 0, "Negative trade count")
    require(buy_base <= volume and buy_quote <= quote, "Buy volume exceeds total")
    require(sell_base == volume - buy_base and sell_quote == quote - buy_quote, "Invalid taker sell")
    ratio = buy_quote / quote if quote else Decimal(0)
    require(number(row["taker_buy_quote_ratio"]) == ratio, "Invalid taker buy ratio")


def fetch_symbol(symbol, timeframe, start, end, cutoff):
    rows = []
    cursor = start
    while cursor < end:
        batch = api("/api/v3/klines", symbol=symbol, interval=timeframe,
                    startTime=cursor, endTime=end - 1, limit=1000)
        require(isinstance(batch, list), "Invalid Binance response")
        if not batch:
            break
        last = cursor - 1
        for raw in batch:
            opened = integer(raw[0])
            require(cursor <= opened < end and opened > last, "Unordered/duplicate Binance page")
            last = opened
            row = parse_kline(raw, symbol, timeframe, cutoff)
            if row is not None:
                rows.append(row)
        cursor = last + DATASETS[timeframe][1]
        time.sleep(0.25)  # Phase 0 uses only ~12 weight units, conservatively paced.
    return rows


def validate_dataset(rows, timeframe, start, end, cutoff):
    require(timeframe in DATASETS, "Invalid dataset")
    interval = DATASETS[timeframe][1]
    require(end - start == DATASETS[timeframe][0] * DAY, "Wrong history window")
    require(end == cutoff // interval * interval, "Wrong closed-candle boundary")
    by_symbol = {symbol: [] for symbol in SYMBOLS}
    seen = set()
    for row in rows:
        validate_row(row, cutoff)
        require(row["timeframe"] == timeframe, "Mixed timeframes")
        opened = millis(row["open_time_utc"])
        key = (row["symbol"], timeframe, opened)
        require(key not in seen, "Duplicate candle")
        seen.add(key)
        by_symbol[row["symbol"]].append(opened)
    expected = list(range(start, end, interval))
    for symbol, times in by_symbol.items():
        require(times == expected, f"Incomplete/unordered history for {symbol} {timeframe}")


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def split_parts(rows, max_bytes=MAX_BYTES):
    require(max_bytes > 0, "Invalid size limit")
    groups = []
    current = []
    for symbol in SYMBOLS:
        group = [row for row in rows if row["symbol"] == symbol]
        require(group, f"Missing symbol {symbol}")
        if current and len(csv_bytes(current + group)) > max_bytes:
            groups.append(current)
            current = []
        current.extend(group)
    if current:
        groups.append(current)
    return groups


def write_export(directory, datasets, cutoff, max_bytes=MAX_BYTES):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "generated_at": iso(cutoff), "status": "complete",
                "source": SOURCE, "part_target_bytes": max_bytes, "datasets": {}}
    for timeframe, (days, interval) in DATASETS.items():
        end = cutoff // interval * interval
        start = end - days * DAY
        rows = datasets[timeframe]
        validate_dataset(rows, timeframe, start, end, cutoff)
        parts = []
        for index, group in enumerate(split_parts(rows, max_bytes), 1):
            content = csv_bytes(group)
            filename = f"{timeframe}-part-{index:03d}.txt"
            (directory / filename).write_bytes(content)
            symbols = list(dict.fromkeys(row["symbol"] for row in group))
            parts.append({"part": index, "filename": filename, "bytes": len(content),
                          "rows": len(group), "symbols_count": len(symbols), "symbols": symbols,
                          "sha256": hashlib.sha256(content).hexdigest(),
                          "oversized": len(content) > max_bytes})
        manifest["datasets"][timeframe] = {
            "history_days": days, "symbols_count": len(SYMBOLS), "rows": len(rows),
            "history_start": iso(start), "history_end": iso(end - 1),
            "parts_count": len(parts), "parts": parts}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate_export(directory, previous=None):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    require(manifest["schema_version"] == 1 and manifest["status"] == "complete", "Invalid manifest")
    require(manifest["source"] == SOURCE, "Invalid manifest source")
    require(set(manifest["datasets"]) == set(DATASETS), "Missing/unexpected dataset")
    cutoff = millis(manifest["generated_at"])
    require(cutoff <= time.time_ns() // 1_000_000 + 60_000, "Future export timestamp")
    target = manifest["part_target_bytes"]
    require(isinstance(target, int) and target > 0, "Invalid part target")
    if previous:
        require(cutoff > millis(previous["generated_at"]), "Export is not newer than previous run")
    files = {"manifest.json"}
    for timeframe, (days, interval) in DATASETS.items():
        dataset = manifest["datasets"][timeframe]
        start, end = millis(dataset["history_start"]), millis(dataset["history_end"]) + 1
        require(dataset["history_days"] == days, "Invalid history_days")
        require(dataset["symbols_count"] == len(SYMBOLS), "Invalid symbol count")
        require(dataset["parts_count"] == len(dataset["parts"]) > 0, "Invalid part count")
        if previous:
            require(end > millis(previous["datasets"][timeframe]["history_end"]), "Dataset regressed")
        rows, seen_symbols = [], set()
        for index, part in enumerate(dataset["parts"], 1):
            filename = f"{timeframe}-part-{index:03d}.txt"
            require(part["part"] == index and part["filename"] == filename, "Invalid part filename/number")
            files.add(filename)
            content = (directory / filename).read_bytes()
            require(len(content) == part["bytes"], "Wrong byte count")
            require(hashlib.sha256(content).hexdigest() == part["sha256"], "SHA-256 mismatch")
            reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
            require(reader.fieldnames == FIELDS, "Invalid CSV header")
            part_rows = list(reader)
            symbols = list(dict.fromkeys(row["symbol"] for row in part_rows))
            require(part["rows"] == len(part_rows) > 0, "Wrong part rows")
            require(part["symbols"] == symbols and part["symbols_count"] == len(symbols), "Wrong part symbols")
            require(not seen_symbols.intersection(symbols), "Symbol split between parts")
            seen_symbols.update(symbols)
            oversized = len(content) > target
            require(part["oversized"] == oversized, "Incorrect oversized flag")
            require(not oversized or len(symbols) == 1, "Oversized multi-symbol part")
            rows.extend(part_rows)
        require(dataset["rows"] == len(rows), "Wrong dataset rows")
        require(seen_symbols == set(SYMBOLS), "Missing expected symbols")
        validate_dataset(rows, timeframe, start, end, cutoff)
    require({path.name for path in directory.iterdir()} == files, "Unexpected export files")
    return manifest


def publish(staging, latest, previous=None):
    """Validate before swapping local folders; rollback on a failed install.

    Public publication is a single GitHub Pages artifact deployment. This local
    two-rename swap is for a single CLI writer, not a live web-server directory.
    """
    if latest.exists():
        previous = validate_export(latest)
    manifest = validate_export(staging, previous)
    backup = latest.with_name(".latest-backup")
    require(not backup.exists(), "Backup exists; inspect/recover previous interrupted run")
    moved = False
    try:
        if latest.exists():
            latest.rename(backup)
            moved = True
        staging.rename(latest)
    except BaseException:
        if moved:
            backup.rename(latest)
        raise
    if moved:
        shutil.rmtree(backup)
    return manifest


def export(output, previous=None):
    output.mkdir(parents=True, exist_ok=True)
    cutoff = integer(api("/api/v3/time")["serverTime"])
    datasets = {}
    for timeframe, (days, interval) in DATASETS.items():
        end = cutoff // interval * interval
        datasets[timeframe] = []
        for symbol in SYMBOLS:
            print(f"Fetching {symbol} {timeframe}", flush=True)
            datasets[timeframe].extend(fetch_symbol(symbol, timeframe, end - days * DAY, end, cutoff))
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=output))
    try:
        write_export(staging, datasets, cutoff)
        return publish(staging, output / "latest", previous)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def remote_manifest(base_url, optional=False):
    try:
        return json.loads(get_bytes(base_url.rstrip("/") + "/manifest.json"))
    except HTTPError as error:
        if optional and error.code == 404:
            return None
        raise


def verify_url(base_url):
    manifest = remote_manifest(base_url)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for timeframe, dataset in manifest["datasets"].items():
            require(timeframe in DATASETS, "Invalid remote dataset")
            for index, part in enumerate(dataset["parts"], 1):
                filename = f"{timeframe}-part-{index:03d}.txt"
                require(part["filename"] == filename, "Invalid remote filename")
                (directory / filename).write_bytes(get_bytes(base_url.rstrip("/") + "/" + filename))
        return validate_export(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("export")
    generate.add_argument("--output", type=Path, default=Path("public"))
    generate.add_argument("--previous-url")
    validate = subparsers.add_parser("validate")
    validate.add_argument("directory", type=Path, nargs="?", default=Path("public/latest"))
    verify = subparsers.add_parser("verify-url")
    verify.add_argument("url")
    args = parser.parse_args()
    if args.command == "export":
        previous = remote_manifest(args.previous_url, optional=True) if args.previous_url else None
        result = export(args.output, previous)
    elif args.command == "validate":
        result = validate_export(args.directory)
    else:
        result = verify_url(args.url)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
