import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import exporter as e


CUTOFF = 1_760_000_000_000


def kline(opened=0, timeframe="1d", quote="100", buy_quote="40"):
    return [opened, "10", "12", "9", "11", "10", opened + e.DATASETS[timeframe][1] - 1,
            quote, 8, "4", buy_quote, "0"]


def fixture(cutoff=CUTOFF):
    datasets = {}
    for timeframe, (days, interval) in e.DATASETS.items():
        end = cutoff // interval * interval
        datasets[timeframe] = [e.parse_kline(kline(opened, timeframe), symbol, timeframe, cutoff)
                               for symbol in e.SYMBOLS
                               for opened in range(end - days * e.DAY, end, interval)]
    return datasets


class ParsingTests(unittest.TestCase):
    def test_fields_and_decimal_flow(self):
        row = e.parse_kline(kline(), "BTCUSDT", "1d", CUTOFF)
        self.assertEqual(list(row), e.FIELDS)
        self.assertEqual(row["taker_sell_base_volume"], "6")
        self.assertEqual(row["taker_sell_quote_volume"], "60")
        self.assertEqual(row["taker_buy_quote_ratio"], "0.4")
        self.assertEqual(row["open_time_utc"], "1970-01-01T00:00:00.000Z")
        self.assertEqual(row["is_closed"], "true")

    def test_zero_quote(self):
        row = e.parse_kline(kline(quote="0", buy_quote="0"), "BTCUSDT", "1d", CUTOFF)
        self.assertEqual(row["taker_buy_quote_ratio"], "0")

    def test_filter_open_candle_and_boundary(self):
        raw = kline()
        self.assertIsNone(e.parse_kline(raw, "BTCUSDT", "1d", raw[6]))
        self.assertIsNotNone(e.parse_kline(raw, "BTCUSDT", "1d", raw[6] + 1))

    def test_invalid_ohlc(self):
        raw = kline()
        raw[2] = "10"
        with self.assertRaisesRegex(ValueError, "OHLC"):
            e.parse_kline(raw, "BTCUSDT", "1d", CUTOFF)

    def test_invalid_volumes_and_trades(self):
        for index, value in [(5, "-1"), (8, -1), (8, 1.5), (9, "11"), (10, "101"), (1, "NaN")]:
            with self.subTest(index=index, value=value), self.assertRaises((ValueError, e.InvalidOperation)):
                raw = kline()
                raw[index] = value
                e.parse_kline(raw, "BTCUSDT", "1d", CUTOFF)

    @patch("exporter.time.sleep")
    @patch("exporter.api")
    def test_pagination(self, api, sleep):
        api.side_effect = [[kline(0)], [kline(e.DAY)]]
        rows = e.fetch_symbol("BTCUSDT", "1d", 0, 2 * e.DAY, CUTOFF)
        self.assertEqual(len(rows), 2)
        self.assertEqual(api.call_args_list[1].kwargs["startTime"], e.DAY)
        self.assertEqual(api.call_args_list[0].kwargs["endTime"], 2 * e.DAY - 1)

    @patch("exporter.time.sleep")
    @patch("exporter.api")
    def test_duplicate_page_rejected(self, api, sleep):
        api.return_value = [kline(0), kline(0)]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            e.fetch_symbol("BTCUSDT", "1d", 0, 2 * e.DAY, CUTOFF)


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phase0 = patch.object(e, "SYMBOLS", e.PHASE0_SYMBOLS)
        cls.phase0.start()
        cls.addClassCleanup(cls.phase0.stop)
        cls.datasets = fixture()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "latest"
        self.manifest = e.write_export(self.directory, self.datasets, CUTOFF)

    def test_manifest_and_sha256(self):
        result = e.validate_export(self.directory)
        self.assertEqual(result, self.manifest)
        self.assertEqual(result["datasets"]["1d"]["rows"], 1826 * 3)
        self.assertEqual(result["datasets"]["4h"]["rows"], 90 * 6 * 3)
        for dataset in result["datasets"].values():
            for part in dataset["parts"]:
                content = (self.directory / part["filename"]).read_bytes()
                self.assertEqual(part["sha256"], hashlib.sha256(content).hexdigest())
                self.assertTrue(content.startswith(",".join(e.FIELDS).encode()))

    def test_duplicate_detection(self):
        rows = self.datasets["1d"] + [self.datasets["1d"][0]]
        dataset = self.manifest["datasets"]["1d"]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            e.validate_dataset(rows, "1d", e.millis(dataset["history_start"]),
                               e.millis(dataset["history_end"]) + 1, CUTOFF)

    def test_missing_candle_or_symbol(self):
        dataset = self.manifest["datasets"]["1d"]
        for rows in [self.datasets["1d"][1:], [r for r in self.datasets["1d"] if r["symbol"] != "SOLUSDT"]]:
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                e.validate_dataset(rows, "1d", e.millis(dataset["history_start"]),
                                   e.millis(dataset["history_end"]) + 1, CUTOFF)

    def test_dynamic_splitting_never_cuts_symbol(self):
        rows = self.datasets["1d"]
        groups = e.split_parts(rows, 500_000)
        self.assertGreater(len(groups), 1)
        seen = set()
        for group in groups:
            symbols = {row["symbol"] for row in group}
            self.assertFalse(seen & symbols)
            seen.update(symbols)
            for symbol in symbols:
                self.assertEqual(sum(row["symbol"] == symbol for row in group), 1826)
        self.assertEqual(seen, set(e.SYMBOLS))

    def test_oversize_single_symbol_flag(self):
        directory = Path(self.temporary.name) / "oversized"
        manifest = e.write_export(directory, self.datasets, CUTOFF, max_bytes=100)
        for dataset in manifest["datasets"].values():
            self.assertEqual(dataset["parts_count"], 3)
            self.assertTrue(all(part["oversized"] for part in dataset["parts"]))
        e.validate_export(directory)

    def test_tamper_and_missing_file(self):
        filename = self.manifest["datasets"]["1d"]["parts"][0]["filename"]
        path = self.directory / filename
        original = path.read_bytes()
        path.write_bytes(original.replace(b"BTCUSDT", b"BADUSDT", 1))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            e.validate_export(self.directory)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            e.validate_export(self.directory)

    def test_manifest_rows_and_symbols(self):
        for key, value in [("rows", 0), ("symbols_count", 0), ("parts_count", 0)]:
            manifest = copy.deepcopy(self.manifest)
            manifest["datasets"]["1d"][key] = value
            (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                e.validate_export(self.directory)

    def test_stale_export_rejected(self):
        with self.assertRaisesRegex(ValueError, "not newer"):
            e.validate_export(self.directory, self.manifest)

    def test_failure_safe_validation(self):
        staging = Path(self.temporary.name) / "staging"
        e.write_export(staging, self.datasets, CUTOFF + 1)
        (staging / "1d-part-001.txt").write_text("bad", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with self.assertRaises(ValueError):
            e.publish(staging, self.directory)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def test_install_failure_rolls_back(self):
        staging = Path(self.temporary.name) / "staging"
        e.write_export(staging, self.datasets, CUTOFF + 1)
        before = (self.directory / "manifest.json").read_bytes()
        rename = Path.rename

        def fail_staging(path, target):
            if path == staging:
                raise OSError("simulated install failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_staging), self.assertRaises(OSError):
            e.publish(staging, self.directory)
        self.assertEqual(before, (self.directory / "manifest.json").read_bytes())

    def test_successful_replacement(self):
        staging = Path(self.temporary.name) / "staging"
        e.write_export(staging, self.datasets, CUTOFF + 1)
        e.publish(staging, self.directory)
        self.assertEqual(e.validate_export(self.directory)["generated_at"], e.iso(CUTOFF + 1))
        self.assertFalse(self.directory.with_name(".latest-backup").exists())


class HttpTests(unittest.TestCase):
    @patch("exporter.time.sleep")
    @patch("exporter.urlopen")
    def test_bounded_backoff(self, urlopen, sleep):
        urlopen.side_effect = URLError("temporary")
        with self.assertRaises(URLError):
            e.get_bytes("https://example.com")
        self.assertEqual(urlopen.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8])

    @patch("exporter.time.sleep")
    @patch("exporter.urlopen")
    def test_rate_limit_retry_after(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("https://example.com", 429, "rate limit", {"Retry-After": "7"}, None)
        with self.assertRaises(HTTPError):
            e.get_bytes("https://example.com", attempts=2)
        sleep.assert_called_once_with(7)

    @patch("exporter.time.sleep")
    @patch("exporter.urlopen")
    def test_access_restrictions_and_long_bans_fail_immediately(self, urlopen, sleep):
        for status, headers in [(403, {}), (451, {}), (418, {}), (429, {"Retry-After": "120"})]:
            urlopen.reset_mock()
            urlopen.side_effect = HTTPError("https://example.com", status, "blocked", headers, None)
            with self.assertRaises(HTTPError):
                e.get_bytes("https://example.com")
            self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
