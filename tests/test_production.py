import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import exporter as e
import publish_data as p
from test_exporter import CUTOFF, fixture, kline


EXPECTED_SYMBOLS = tuple("AAVEUSDT ADAUSDT AEROUSDT AIXBTUSDT ALGOUSDT APTUSDT ASTERUSDT AVAXUSDT BNBUSDT BONKUSDT BTCUSDT CAKEUSDT COOKIEUSDT ENAUSDT ETHUSDT FETUSDT GMXUSDT HBARUSDT HEIUSDT ICPUSDT INJUSDT JUPUSDT LDOUSDT LINKUSDT LTCUSDT METUSDT MORPHOUSDT NEARUSDT ORCAUSDT PENGUUSDT PEPEUSDT PUMPUSDT RAYUSDT RENDERUSDT SOLUSDT STXUSDT SUIUSDT SUSDT SYNUSDT SYRUPUSDT TAOUSDT TRXUSDT VIRTUALUSDT WIFUSDT XLMUSDT XRPUSDT ZECUSDT".split())


def production_fixture():
    datasets = {}
    for timeframe, (_, interval) in e.DATASETS.items():
        end = CUTOFF // interval * interval
        datasets[timeframe] = []
        for index, symbol in enumerate(e.SYMBOLS):
            # Different listing dates, all shorter than the requested window.
            count = 20 + index
            datasets[timeframe].extend(e.parse_kline(kline(opened, timeframe), symbol, timeframe, CUTOFF)
                                       for opened in range(end - count * interval, end, interval))
    return datasets


class ConfigurationTests(unittest.TestCase):
    def test_exact_production_universe(self):
        self.assertEqual(e.load_symbols(), EXPECTED_SYMBOLS)
        self.assertEqual(len(e.load_symbols()), 47)

    def test_bad_configuration(self):
        for value in [[], ["BTCUSDT", "BTCUSDT"], ["btcUSDT"], ["BTC/USD"], {}, [None]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                e.checked_symbols(value)


class ProductionExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = production_fixture()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.latest = self.root / "public" / "latest"
        self.manifest = e.write_export(self.latest, self.datasets, CUTOFF, max_bytes=20_000)

    def test_newer_symbols_and_multiple_parts_both_timeframes(self):
        result = e.validate_export(self.latest)
        for timeframe, dataset in result["datasets"].items():
            self.assertGreater(dataset["parts_count"], 1)
            self.assertEqual(dataset["symbols_count"], 47)
            self.assertEqual(dataset["requested_symbols"], list(EXPECTED_SYMBOLS))
            self.assertEqual(dataset["exported_symbols"], list(EXPECTED_SYMBOLS))
            self.assertEqual(dataset["failed_symbols"], [])
            self.assertEqual(sum(part["rows"] for part in dataset["parts"]), dataset["rows"])
            symbols = [symbol for part in dataset["parts"] for symbol in part["symbols"]]
            self.assertEqual(symbols, list(EXPECTED_SYMBOLS))
            self.assertEqual(len(set(symbols)), 47)
            self.assertEqual(dataset["symbol_rows"]["AEROUSDT"], 22)
            self.assertGreater(dataset["symbol_history"]["AEROUSDT"]["history_start"], dataset["requested_history_start"])
            for part in dataset["parts"]:
                self.assertLessEqual(part["bytes"], 20_000)

    def test_internal_gap_rejected_even_for_recent_listing(self):
        datasets = copy.deepcopy(self.datasets)
        datasets["1d"].pop(5)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            e.write_export(self.root / "bad", datasets, CUTOFF)

    def test_missing_symbol_reports_incomplete_and_cannot_publish(self):
        datasets = copy.deepcopy(self.datasets)
        datasets["1d"] = [row for row in datasets["1d"] if row["symbol"] != "AEROUSDT"]
        staging = self.root / "staging"
        manifest = e.write_export(staging, datasets, CUTOFF + 1)
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["datasets"]["1d"]["failed_symbols"], ["AEROUSDT"])
        self.assertEqual(manifest["datasets"]["1d"]["symbol_rows"]["AEROUSDT"], 0)
        before = {path.name: path.read_bytes() for path in self.latest.iterdir()}
        with self.assertRaises(ValueError):
            e.publish(staging, self.latest)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.latest.iterdir()})

    def test_failed_symbol_in_diagnostics_is_not_a_complete_export(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["datasets"]["4h"]["failed_symbols"] = ["AEROUSDT"]
        (self.latest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Failed symbols"):
            e.validate_export(self.latest)

    def test_wrong_actual_history_or_symbol_row_count_rejected(self):
        for key, value in [("history_start", e.iso(0)), ("rows", 999)]:
            manifest = copy.deepcopy(self.manifest)
            manifest["datasets"]["1d"]["symbol_history"]["AEROUSDT"][key] = value
            (self.latest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                e.validate_export(self.latest)

    def failed_export(self, failure):
        before = {path.name: path.read_bytes() for path in self.latest.iterdir()}

        def fetch(symbol, timeframe, start, end, cutoff):
            if symbol == "AEROUSDT" and timeframe == "1d":
                if failure == "empty":
                    return []
                raise HTTPError(e.BASE_URL, 400, "Invalid symbol", {}, None)
            return [row for row in self.datasets[timeframe] if row["symbol"] == symbol]

        diagnostic = self.root / "diagnostic.json"
        with patch("exporter.api", return_value={"serverTime": CUTOFF + 1}), \
                patch("exporter.fetch_symbol", side_effect=fetch), \
                self.assertRaisesRegex(ValueError, "Incomplete export"):
            e.export(self.latest.parent, diagnostics=diagnostic)
        manifest = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["publication"], "not_published")
        self.assertEqual(manifest["datasets"]["1d"]["failed_symbols"], ["AEROUSDT"])
        self.assertEqual(manifest["datasets"]["4h"]["failed_symbols"], [])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.latest.iterdir()})
        self.assertFalse(list(self.latest.parent.glob(".export-*")))
        return manifest

    def test_zero_candles_fails_and_preserves_previous_latest(self):
        manifest = self.failed_export("empty")
        self.assertIn("No valid closed candles", manifest["datasets"]["1d"]["symbol_errors"]["AEROUSDT"])

    def test_permanent_failure_reports_symbol_and_preserves_previous_latest(self):
        manifest = self.failed_export("http")
        self.assertIn("HTTPError", manifest["datasets"]["1d"]["symbol_errors"]["AEROUSDT"])

    def test_all_symbols_failed_produces_explicit_manifest(self):
        manifest = e.write_export(self.root / "all-failed", {"1d": [], "4h": []}, CUTOFF)
        self.assertEqual(manifest["status"], "incomplete")
        for dataset in manifest["datasets"].values():
            self.assertEqual(dataset["failed_symbols"], list(EXPECTED_SYMBOLS))
            self.assertEqual(dataset["parts_count"], 0)
            self.assertIsNone(dataset["history_start"])

    def test_legacy_phase0_snapshot_validation_and_production_migration(self):
        old = self.root / "legacy"
        with patch.object(e, "SYMBOLS", e.PHASE0_SYMBOLS):
            datasets = fixture(CUTOFF - 1)
            manifest = e.write_export(old, datasets, CUTOFF - 1)
            # Real V1.0 schema, before diagnostics were introduced.
            for dataset in manifest["datasets"].values():
                for key in ("requested_history_start", "requested_history_end", "requested_symbols",
                            "exported_symbols", "failed_symbols", "symbol_errors", "symbol_rows", "symbol_history"):
                    dataset.pop(key)
            (old / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        validated = e.validate_previous_export(old)
        self.assertEqual(validated["datasets"]["1d"]["symbols_count"], 3)
        e.validate_export(self.latest, previous=validated)
        files = {f"latest/{path.name}": path.read_bytes() for path in old.iterdir()}
        p.materialize(files, self.root / "old-materialized", previous=True)
        with self.assertRaises(ValueError):
            e.validate_export(old)  # Old data cannot be republished as the 47-symbol candidate.


if __name__ == "__main__":
    unittest.main()
