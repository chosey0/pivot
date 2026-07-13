"""품질 진단 검사가 passed/warning/failed를 올바르게 판정하는지 검증한다."""

import pandas as pd

from pivot.dataset.batch import assign_splits, split_config
from pivot.diagnostics import quality

from fakes import make_candles


def checks_by_id(report: dict, check_id: str, symbol: str | None = None) -> list[dict]:
    return [
        item
        for item in report["checks"]
        if item["id"] == check_id and (symbol is None or item.get("symbol") == symbol)
    ]


class TestCacheDiagnostics:
    def test_healthy_cache_passes(self):
        report = quality.diagnose_cache({"AAA": make_candles()}, timeframe="day")
        assert report["status"] == "passed"
        assert report["summary"]["failed"] == 0
        assert report["input"]["symbols"] == ["AAA"]

    def test_missing_cache_fails(self):
        report = quality.diagnose_cache({"AAA": None}, timeframe="day")
        assert report["status"] == "failed"
        assert checks_by_id(report, "cache_exists")[0]["status"] == "failed"

    def test_duplicate_and_unsorted_timestamps_fail(self):
        df = make_candles(length=50)
        broken = pd.concat([df, df.tail(3)]).sort_index()
        report = quality.diagnose_cache({"AAA": broken}, timeframe="day")
        assert checks_by_id(report, "time_unique")[0]["status"] == "failed"

        descending = df.iloc[::-1]
        report = quality.diagnose_cache({"AAA": descending}, timeframe="day")
        assert checks_by_id(report, "time_ascending")[0]["status"] == "failed"

    def test_ohlc_violation_fails(self):
        df = make_candles(length=50)
        df.iloc[10, df.columns.get_loc("High")] = df.iloc[10]["Low"] - 1
        report = quality.diagnose_cache({"AAA": df}, timeframe="day")
        assert checks_by_id(report, "ohlc_invariant")[0]["status"] == "failed"

    def test_volume_anomalies(self):
        df = make_candles(length=100)
        df.iloc[0, df.columns.get_loc("Volume")] = -5
        report = quality.diagnose_cache({"AAA": df}, timeframe="day")
        assert checks_by_id(report, "volume_values")[0]["status"] == "failed"

        df = make_candles(length=100)
        df.iloc[:20, df.columns.get_loc("Volume")] = 0
        report = quality.diagnose_cache({"AAA": df}, timeframe="day")
        assert checks_by_id(report, "volume_values")[0]["status"] == "warning"

    def test_day_gap_warns(self):
        df = make_candles(length=60)
        shifted = df.index[30:] + pd.Timedelta(days=40)
        df.index = df.index[:30].append(shifted)
        report = quality.diagnose_cache({"AAA": df}, timeframe="day")
        assert checks_by_id(report, "time_gaps")[0]["status"] == "warning"

    def test_short_history_warns_on_ma(self):
        report = quality.diagnose_cache(
            {"AAA": make_candles(length=60)}, timeframe="day"
        )
        assert checks_by_id(report, "ma_warmup")[0]["status"] == "warning"  # 120 > 60

    def test_kronos_structural_break_warns_without_mutating_cache(self):
        df = make_candles(length=240)
        original = df.copy(deep=True)
        df.iloc[130, df.columns.get_loc("Open")] = df.iloc[129]["Close"] * 1.5
        df.iloc[130, df.columns.get_loc("High")] = df.iloc[130]["Open"] + 1
        report = quality.diagnose_cache({"AAA": df}, timeframe="day")
        item = checks_by_id(report, "kronos_cleaning")[0]
        assert item["status"] == "warning"
        assert item["data"]["structural_breaks"] == 1
        assert original.iloc[:130].equals(df.iloc[:130])


class TestPreviewDiagnostics:
    def stats(self, **overrides) -> dict:
        base = {
            "samples": 100,
            "points": 120,
            "class_counts": {0: 40, 1: 40, 2: 20},
            "dropped_nan": 5,
            "pairing_stats": {
                "rule": "adjacent_markers_v1",
                "adjacent_edges": 119,
                "unpaired_markers": 1,
                "dropped_invalid_position": 0,
                "dropped_label2": 14,
            },
            "overlap_clusters": {
                "tie_policy": "plateau_last",
                "plateau_clusters": 0,
                "plateau_clustered_points": 0,
                "dropped_plateau_points": 0,
                "max_plateau_cluster_size": 0,
                "sample_clusters": 0,
                "clustered_samples": 0,
                "redundant_samples": 0,
                "max_sample_cluster_size": 0,
                "threshold": 0.9,
                "max_end_gap": 9,
            },
            "cleaning": {
                "mode": "report_only",
                "retained_bars": 240,
                "removed_bars": 0,
                "removed_ratio": 0,
                "segments": 1,
                "structural_breaks": 0,
            },
        }
        return {**base, **overrides}

    def test_healthy_preview_passes(self):
        report = quality.diagnose_preview({"AAA": self.stats()}, input_snapshot={})
        assert report["status"] == "passed"

    def test_symbol_error_fails(self):
        report = quality.diagnose_preview(
            {"AAA": {"error": "no cached data"}}, input_snapshot={}
        )
        assert report["status"] == "failed"

    def test_low_sample_count_warns(self):
        report = quality.diagnose_preview(
            {"AAA": self.stats(samples=10, class_counts={0: 5, 1: 4, 2: 1})},
            input_snapshot={},
        )
        assert checks_by_id(report, "sample_count")[0]["status"] == "warning"

    def test_ignore_heavy_distribution_warns(self):
        report = quality.diagnose_preview(
            {"AAA": self.stats(class_counts={0: 10, 1: 10, 2: 80})}, input_snapshot={}
        )
        assert checks_by_id(report, "class_balance")[0]["status"] == "warning"

    def test_nan_drop_ratio_warns(self):
        report = quality.diagnose_preview(
            {"AAA": self.stats(dropped_nan=60)}, input_snapshot={}
        )
        assert checks_by_id(report, "feature_nan")[0]["status"] == "warning"

    def test_pairing_conservation_mismatch_fails(self):
        pairing = self.stats()["pairing_stats"] | {"adjacent_edges": 118}
        report = quality.diagnose_preview(
            {"AAA": self.stats(pairing_stats=pairing)}, input_snapshot={}
        )
        assert checks_by_id(report, "sample_pairing")[0]["status"] == "failed"

    def test_cleaning_findings_warn(self):
        report = quality.diagnose_preview(
            {
                "AAA": self.stats(
                    cleaning={
                        "mode": "filter",
                        "retained_bars": 150,
                        "removed_bars": 90,
                        "removed_ratio": 0.375,
                        "segments": 2,
                        "structural_breaks": 1,
                    }
                )
            },
            input_snapshot={},
        )
        assert checks_by_id(report, "kronos_cleaning")[0]["status"] == "warning"

    def test_unresolved_overlap_clusters_warn(self):
        overlap = self.stats()["overlap_clusters"] | {
            "tie_policy": "all",
            "plateau_clusters": 2,
            "plateau_clustered_points": 5,
            "sample_clusters": 2,
            "clustered_samples": 5,
            "redundant_samples": 3,
        }
        report = quality.diagnose_preview(
            {"AAA": self.stats(overlap_clusters=overlap)}, input_snapshot={}
        )
        item = checks_by_id(report, "sample_overlap")[0]
        assert item["status"] == "warning"
        assert item["data"]["redundant_samples"] == 3

    def test_plateau_candidates_without_sample_overlap_pass(self):
        overlap = self.stats()["overlap_clusters"] | {
            "tie_policy": "all",
            "plateau_clusters": 2,
            "plateau_clustered_points": 5,
        }
        report = quality.diagnose_preview(
            {"AAA": self.stats(overlap_clusters=overlap)}, input_snapshot={}
        )
        assert checks_by_id(report, "sample_overlap")[0]["status"] == "passed"


def make_dataset_rows(symbols: list[str], *, seed: int = 42):
    splits = assign_splits(symbols, seed=seed)
    dataset = {
        "id": 1,
        "name": "진단셋",
        "status": "ready",
        "sample_count": 10 * len(symbols),
        "class_counts": {"0": 4 * len(symbols), "1": 4 * len(symbols), "2": 2 * len(symbols)},
        "preset_snapshot": {
            "preset": {"labeling": {"sample_pairing": "adjacent_markers_v1"}},
            "split": split_config(seed),
        },
    }
    symbol_rows = [
        {
            "symbol": symbol,
            "split": splits[symbol],
            "status": "ready",
            "sample_count": 10,
            "class_counts": {"0": 4, "1": 4, "2": 2},
            "length_stats": {
                "min": 3,
                "max": 40,
                "mean": 12.0,
                "points": 13,
                "dropped_nan": 1,
                "pairing_stats": {
                    "rule": "adjacent_markers_v1",
                    "adjacent_edges": 12,
                    "unpaired_markers": 1,
                    "dropped_invalid_position": 0,
                    "dropped_label2": 1,
                },
            },
        }
        for symbol in symbols
    ]
    shard_rows = [
        {"symbol": symbol, "shard_index": 0, "row_count": 10} for symbol in symbols
    ]
    return dataset, symbol_rows, shard_rows


class TestDatasetDiagnostics:
    def test_healthy_dataset_passes(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        dataset, symbol_rows, shard_rows = make_dataset_rows(symbols)
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert report["status"] == "passed", report["checks"]

    def test_non_ready_dataset_fails(self):
        dataset, symbol_rows, shard_rows = make_dataset_rows(["AAA"])
        dataset["status"] = "building"
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "dataset_status")[0]["status"] == "failed"

    def test_split_rule_mismatch_fails_as_leakage(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        dataset, symbol_rows, shard_rows = make_dataset_rows(symbols)
        symbol_rows[0]["split"] = (
            "train" if symbol_rows[0]["split"] != "train" else "test"
        )
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "split_leakage")[0]["status"] == "failed"

    def test_missing_split_fails(self):
        dataset, symbol_rows, shard_rows = make_dataset_rows(["AAA", "BBB"])
        symbol_rows[1]["split"] = None
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "split_leakage")[0]["status"] == "failed"

    def test_empty_validation_split_warns(self):
        dataset, symbol_rows, shard_rows = make_dataset_rows(["AAA", "BBB"])
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "split_leakage")[0]["status"] == "warning"

    def test_shard_row_mismatch_fails(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        dataset, symbol_rows, shard_rows = make_dataset_rows(symbols)
        shard_rows[0]["row_count"] = 7
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "shard_integrity")[0]["status"] == "failed"

    def test_pairing_rule_mismatch_fails(self):
        dataset, symbol_rows, shard_rows = make_dataset_rows(["AAA"])
        symbol_rows[0]["length_stats"]["pairing_stats"]["rule"] = (
            "latest_opposite_v1"
        )
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "sample_pairing")[0]["status"] == "failed"

    def test_missing_shards_fail(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        dataset, symbol_rows, shard_rows = make_dataset_rows(symbols)
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows[1:])
        assert checks_by_id(report, "shard_integrity")[0]["status"] == "failed"

    def test_existing_dataset_overlap_clusters_warn(self):
        dataset, symbol_rows, shard_rows = make_dataset_rows(["AAA", "BBB"])
        report = quality.diagnose_dataset(
            dataset,
            symbol_rows,
            shard_rows,
            overlap_by_symbol={
                "AAA": {
                    "threshold": 0.9,
                    "max_end_gap": 9,
                    "clusters": 1,
                    "clustered_samples": 3,
                    "redundant_samples": 2,
                    "max_cluster_size": 3,
                },
                "BBB": {
                    "threshold": 0.9,
                    "max_end_gap": 9,
                    "clusters": 0,
                    "clustered_samples": 0,
                    "redundant_samples": 0,
                    "max_cluster_size": 0,
                },
            },
        )
        item = checks_by_id(report, "sample_overlap")[0]
        assert item["status"] == "warning"
        assert item["data"]["top_symbol"] == "AAA"

    def test_dominant_symbol_warns(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        dataset, symbol_rows, shard_rows = make_dataset_rows(symbols)
        symbol_rows[0]["sample_count"] = 1_000
        shard_rows[0]["row_count"] = 1_000
        dataset["sample_count"] = sum(row["sample_count"] for row in symbol_rows)
        report = quality.diagnose_dataset(dataset, symbol_rows, shard_rows)
        assert checks_by_id(report, "symbol_contribution")[0]["status"] == "warning"
