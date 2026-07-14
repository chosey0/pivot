"""일괄 전처리 파이프라인 검증 — preview/batch 동일성, 부분 실패, split 결정성."""

from pathlib import Path

import pandas as pd

from pivot.config import (
    CleaningConfig,
    FractalConfig,
    LabelingConfig,
    PreprocessPreset,
    Timeframe,
)
from pivot.dataset.batch import (
    assign_sample_splits,
    assign_splits,
    build_snapshot,
    run_batch,
    split_config,
)
from pivot.dataset.build import run_preprocess
from pivot.dataset.shards import build_shards, object_path, read_shard
from pivot.ingestion.cache import cache_path, load_cache, replace_cache
from pivot.storage.datasets import DatasetRepository
from pivot.storage.jobs import JobRepository
from pivot.storage.supabase import DATASET_BUCKET

from fakes import FakeDb, FakeStorage, make_candles

BROKER = "kiwoom"


def make_preset(name: str = "배치 테스트") -> PreprocessPreset:
    return PreprocessPreset(
        name=name,
        fractal=FractalConfig(n=5),
        features=["Open", "High", "Low", "Close"],
        labeling=LabelingConfig(mode="cls3", ignore_rule="none"),
    )


def write_cache(data_root: Path, symbol: str, seed: int) -> None:
    path = cache_path(data_root, BROKER, "day", symbol)
    replace_cache(path, make_candles(seed=seed))


class Harness:
    """router의 batch 시작 절차(행 생성)를 재현하고 run_batch를 돌린다."""

    def __init__(self, tmp_path: Path, symbols: list[str], cached: list[str]):
        self.db = FakeDb()
        self.jobs = JobRepository(self.db)
        self.datasets = DatasetRepository(self.db)
        self.storage = FakeStorage()
        self.data_root = tmp_path
        self.preset = make_preset()
        for seed, symbol in enumerate(cached, start=1):
            write_cache(tmp_path, symbol, seed)

        preset_row = {
            "id": 1,
            "name": self.preset.name,
            "version": 1,
            "schema_version": 1,
            "preset": self.preset.model_dump(mode="json"),
        }
        self.dataset = self.datasets.create(
            name="테스트셋",
            preset_id=1,
            preset_snapshot=build_snapshot(preset_row, split_config()),
            timeframe="day",
            feature_columns=list(self.preset.features),
            symbols=symbols,
            splits={},
        )
        self.job = self.jobs.create(
            kind="preprocess_batch", payload={}, total_items=len(symbols)
        )
        self.symbols = symbols

    def run(self) -> None:
        run_batch(
            jobs=self.jobs,
            datasets=self.datasets,
            storage=self.storage,
            job_id=self.job["id"],
            dataset_id=self.dataset["id"],
            preset=self.preset,
            symbols=self.symbols,
            data_root=self.data_root,
            broker=BROKER,
        )


class TestRunBatch:
    def test_timeframe_specific_fractal_keeps_common_settings(self):
        preset = make_preset().model_copy(
            update={"fractal_windows": {"day": 7, "min1": 11}}
        )

        minute = preset.for_timeframe(Timeframe.from_code("min1"))

        assert minute.timeframe.code == "min1"
        assert minute.fractal.n == 11
        assert minute.features == preset.features
        assert minute.labeling == preset.labeling

    def test_snapshot_materializes_compatible_cleaning_defaults(self):
        preset = PreprocessPreset(cleaning=CleaningConfig())
        row = {
            "id": 1,
            "name": "legacy",
            "version": 1,
            "schema_version": 1,
            "preset": {"name": "legacy"},
        }

        snapshot = build_snapshot(row, split_config(), preset=preset)

        assert snapshot["preset"]["cleaning"]["mode"] == "report_only"
        assert snapshot["preset"]["cleaning"]["policy"] == "kronos_adapted_v1"
        assert snapshot["preset"]["labeling"]["sample_pairing"] == "adjacent_markers_v1"

    def test_success_marks_everything_ready(self, tmp_path):
        h = Harness(tmp_path, ["AAA", "BBB"], cached=["AAA", "BBB"])
        h.run()

        job = h.jobs.get(h.job["id"])
        assert job["status"] == "succeeded"
        assert job["completed_items"] == 2

        dataset = h.datasets.get(h.dataset["id"])
        assert dataset["status"] == "ready"
        symbol_rows = h.datasets.list_symbols(h.dataset["id"])
        assert [row["status"] for row in symbol_rows] == ["ready", "ready"]
        assert all(row["split"] is None for row in symbol_rows)
        assert dataset["sample_count"] == sum(row["sample_count"] for row in symbol_rows)
        assert dataset["sample_count"] > 0

        shards = h.datasets.list_shards(h.dataset["id"])
        assert len(shards) >= 2  # 종목당 최소 1개
        for shard in shards:
            data = h.storage.objects[(DATASET_BUCKET, shard["object_path"])]
            assert len(data) == shard["size_bytes"]
            assert shard["feature_schema"]["columns"] == h.preset.features
            assert set(read_shard(data)["split"].to_pylist()) <= {
                "train",
                "validation",
                "test",
            }

        events = h.jobs.events_after(h.job["id"])
        assert [e["sequence"] for e in events] == list(range(len(events)))
        assert events[0]["event_type"] == "job_started"
        assert events[-1]["event_type"] == "dataset_ready"

        assert all(
            row["length_stats"]["cleaning"]["policy"] == "kronos_adapted_v1"
            for row in symbol_rows
        )
        assert all(
            row["length_stats"]["pairing_stats"]["rule"]
            == "adjacent_markers_v1"
            for row in symbol_rows
        )
        assert all(
            row["length_stats"]["points"]
            == row["length_stats"]["pairing_stats"]["adjacent_edges"]
            + row["length_stats"]["pairing_stats"]["unpaired_markers"]
            for row in symbol_rows
        )

    def test_batch_shards_match_preview_samples(self, tmp_path):
        """preview(run_preprocess 직접 호출)와 batch 산출물이 동일해야 한다."""
        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.run()

        df = load_cache(cache_path(tmp_path, BROKER, "day", "AAA"))
        preview = run_preprocess(df, h.preset)

        rows: list[dict] = []
        for shard in h.datasets.list_shards(h.dataset["id"]):
            table = read_shard(h.storage.objects[(DATASET_BUCKET, shard["object_path"])])
            rows.extend(table.to_pylist())
        rows.sort(key=lambda row: row["sample_index"])

        assert len(rows) == len(preview.samples)
        features = preview.frame[preview.feature_columns].astype("float64")
        for row, sample in zip(rows, preview.samples):
            assert row["label"] == sample.label
            assert row["kind"] == sample.kind
            assert row["length"] == sample.length
            window = features.iloc[
                sample.start_position : sample.end_position + 1
            ].to_numpy()
            assert row["features"] == window.tolist()
            assert row["start_time"] == preview.frame.index[sample.start_position]
            assert row["end_time"] == preview.frame.index[sample.end_position]
            assert row["start_position"] == sample.start_position
            assert row["end_position"] == sample.end_position

    def test_same_symbol_multiple_timeframes_share_dataset_without_collisions(
        self, tmp_path
    ):
        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        minute = make_candles(seed=7).copy()
        minute.index = pd.date_range(
            "2025-01-01 09:00", periods=len(minute), freq="min", name="Time"
        )
        minute_path = cache_path(tmp_path, BROKER, "min1", "AAA")
        replace_cache(minute_path, minute)
        targets = [
            {
                "symbol": "AAA",
                "timeframe": code,
                "region": "domestic",
                "exchange": "",
                "broker": BROKER,
                "start": None,
                "end": None,
                "cache_start": None,
                "cache_end": None,
            }
            for code in ("day", "min1")
        ]

        run_batch(
            jobs=h.jobs,
            datasets=h.datasets,
            storage=h.storage,
            job_id=h.job["id"],
            dataset_id=h.dataset["id"],
            preset=h.preset.model_copy(
                update={"fractal_windows": {"day": 5, "min1": 7}}
            ),
            symbols=["AAA"],
            targets=targets,
            data_root=tmp_path,
            broker=BROKER,
        )

        rows = []
        shards = h.datasets.list_shards(h.dataset["id"])
        for shard in shards:
            rows.extend(
                read_shard(
                    h.storage.objects[(DATASET_BUCKET, shard["object_path"])]
                ).to_pylist()
            )
        assert {row["timeframe"] for row in rows} == {"day", "min1"}
        assert len({row["source_key"] for row in rows}) == 2
        assert [shard["shard_index"] for shard in shards] == list(range(len(shards)))
        expected = assign_sample_splits(
            [
                (row["source_key"], row["sample_index"], row["label"])
                for row in rows
            ]
        )
        assert all(
            row["split"] == expected[(row["source_key"], row["sample_index"])]
            for row in rows
        )

        symbol = h.datasets.list_symbols(h.dataset["id"])[0]
        stats = symbol["length_stats"]
        assert stats["targets"] == [
            {
                key: target.get(key)
                for key in ("symbol", "timeframe", "region", "exchange", "start", "end")
            }
            for target in targets
        ]
        assert stats["min"] <= stats["mean"] <= stats["max"]
        assert stats["points"] == (
            stats["pairing_stats"]["adjacent_edges"]
            + stats["pairing_stats"]["unpaired_markers"]
        )
        assert stats["cleaning"]["target_count"] == 2
        assert len(stats["cleaning"]["targets"]) == 2
        assert stats["overlap_clusters"]["sample_clusters"] >= 0
        assert h.jobs.get(h.job["id"])["completed_items"] == 2

    def test_partial_failure_keeps_processing_and_fails_dataset(self, tmp_path):
        # 첫 종목(BBB)은 캐시가 없어 실패 — 나머지 종목은 계속 처리돼야 한다
        h = Harness(tmp_path, ["BBB", "AAA"], cached=["AAA"])
        h.run()

        symbol_rows = {row["symbol"]: row for row in h.datasets.list_symbols(h.dataset["id"])}
        assert symbol_rows["BBB"]["status"] == "failed"
        assert "no cached data" in symbol_rows["BBB"]["error"]
        assert symbol_rows["AAA"]["status"] == "ready"
        assert symbol_rows["AAA"]["sample_count"] > 0

        dataset = h.datasets.get(h.dataset["id"])
        assert dataset["status"] == "failed"
        assert "BBB" in dataset["failure_message"]

        job = h.jobs.get(h.job["id"])
        assert job["status"] == "failed"
        assert job["completed_items"] == 2  # 실패해도 끝까지 진행
        event_types = [e["event_type"] for e in h.jobs.events_after(h.job["id"])]
        assert "symbol_failed" in event_types
        assert "symbol_succeeded" in event_types
        assert event_types[-1] == "dataset_failed"

    def test_shard_verification_failure_fails_symbol(self, tmp_path):
        class CorruptingStorage(FakeStorage):
            def download(self, bucket: str, path: str) -> bytes:
                return super().download(bucket, path) + b"!"

        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.storage = CorruptingStorage()
        h.run()

        symbol_rows = h.datasets.list_symbols(h.dataset["id"])
        assert symbol_rows[0]["status"] == "failed"
        assert "verification failed" in symbol_rows[0]["error"]
        assert h.datasets.list_shards(h.dataset["id"]) == []  # 검증 실패면 기록 금지
        assert h.datasets.get(h.dataset["id"])["status"] == "failed"

    def test_cancelled_before_start_does_nothing(self, tmp_path):
        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.jobs.finish(h.job["id"], "cancelled")
        h.run()
        assert h.jobs.get(h.job["id"])["status"] == "cancelled"
        assert h.jobs.events_after(h.job["id"]) == []

    def test_zero_sample_result_fails_dataset(self, tmp_path):
        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.preset = h.preset.model_copy(
            update={"fractal": FractalConfig(n=999)}
        )

        h.run()

        symbol = h.datasets.list_symbols(h.dataset["id"])[0]
        assert symbol["status"] == "failed"
        assert "no samples" in symbol["error"]
        assert h.datasets.get(h.dataset["id"])["status"] == "failed"
        assert h.datasets.list_shards(h.dataset["id"]) == []

    def test_ready_dataset_is_not_downgraded_when_final_event_fails(self, tmp_path):
        class FailingReadyEventJobs(JobRepository):
            def append_event(
                self, job_id: int, sequence: int, event_type: str, payload: dict
            ) -> dict:
                if event_type == "dataset_ready":
                    raise RuntimeError("event service unavailable")
                return super().append_event(job_id, sequence, event_type, payload)

        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.jobs = FailingReadyEventJobs(h.db)
        h.run()

        assert h.datasets.get(h.dataset["id"])["status"] == "ready"
        assert h.jobs.get(h.job["id"])["status"] == "succeeded"

    def test_ready_dataset_is_not_downgraded_when_job_finish_fails(self, tmp_path):
        class FailingFinishJobs(JobRepository):
            def finish(self, job_id: int, status: str, **kwargs) -> dict:
                if status == "succeeded":
                    raise RuntimeError("job update unavailable")
                return super().finish(job_id, status, **kwargs)

        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.jobs = FailingFinishJobs(h.db)
        h.run()

        assert h.datasets.get(h.dataset["id"])["status"] == "ready"
        job = h.jobs.get(h.job["id"])
        assert job["status"] == "failed"
        assert "dataset is ready" in job["error"]


class TestSplits:
    def test_samples_are_stratified_60_20_20_per_class(self):
        samples = [
            (f"S{index % 3}", label * 10 + index, label)
            for label in (0, 1, 2)
            for index in range(10)
        ]
        splits = assign_sample_splits(samples, seed=42)

        for label in (0, 1, 2):
            values = [
                splits[(symbol, index)]
                for symbol, index, row_label in samples
                if row_label == label
            ]
            assert values.count("train") == 6
            assert values.count("validation") == 2
            assert values.count("test") == 2

    def test_deterministic_and_order_independent(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        first = assign_splits(symbols, seed=42)
        second = assign_splits(list(reversed(symbols)), seed=42)
        assert first == second
        assert assign_splits(symbols, seed=43) != first  # seed가 규칙의 일부

    def test_ratios_and_coverage(self):
        symbols = [f"S{i:03d}" for i in range(20)]
        splits = assign_splits(symbols, seed=42)
        counts = {name: list(splits.values()).count(name) for name in ("train", "validation", "test")}
        assert counts == {"train": 14, "validation": 3, "test": 3}
        assert set(splits) == set(symbols)

    def test_small_symbol_count_falls_back_to_train(self):
        assert set(assign_splits(["ONLY"]).values()) == {"train"}

    def test_three_or_more_symbols_keep_all_splits_non_empty(self):
        splits = assign_splits(["A", "B", "C", "D", "E"])
        assert set(splits.values()) == {"train", "validation", "test"}

    def test_legacy_split_remains_reproducible(self):
        splits = assign_splits(
            ["A", "B", "C", "D", "E"], method="seeded_shuffle_v1"
        )
        assert set(splits.values()) == {"train"}


class TestBuildShards:
    def test_chunking_respects_target_and_keeps_global_index(self, tmp_path):
        df = make_candles(length=300, seed=9)
        preset = make_preset()
        result = run_preprocess(df, preset)
        assert len(result.samples) > 4

        shards = build_shards(
            result.frame, result.samples, result.feature_columns, target_bytes=2_000
        )
        assert len(shards) > 1
        assert [shard.index for shard in shards] == list(range(len(shards)))
        assert sum(shard.row_count for shard in shards) == len(result.samples)

        indexes = []
        for shard in shards:
            indexes.extend(read_shard(shard.data).column("sample_index").to_pylist())
        assert indexes == list(range(len(result.samples)))

    def test_object_path_is_immutable_convention(self):
        sha = "a" * 64
        assert (
            object_path(7, "005930", 3, sha)
            == "datasets/7/005930/part-00003-aaaaaaaaaaaa.parquet"
        )

    def test_no_samples_produce_no_shards(self):
        df = make_candles(length=30, seed=1)
        preset = make_preset()
        enriched = run_preprocess(df, preset)
        assert build_shards(enriched.frame, [], enriched.feature_columns) == []
