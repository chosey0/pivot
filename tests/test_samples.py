"""샘플 브라우저 접근 검증 — 안정 순번, 페이지네이션, 라벨 필터, shard 오류."""

import pytest

from pivot.config import FractalConfig, LabelingConfig, PreprocessPreset
from pivot.dataset import samples
from pivot.dataset.build import run_preprocess
from pivot.dataset.shards import build_shards, feature_schema, object_path
from pivot.storage.datasets import DatasetRepository
from pivot.storage.supabase import DATASET_BUCKET, PARQUET_CONTENT_TYPE

from fakes import FakeDb, FakeStorage, make_candles


@pytest.fixture(autouse=True)
def clear_index_cache():
    samples._index_cache.clear()
    yield
    samples._index_cache.clear()


def make_preset() -> PreprocessPreset:
    return PreprocessPreset(
        name="샘플 테스트",
        fractal=FractalConfig(n=5),
        features=["Open", "High", "Low", "Close"],
        labeling=LabelingConfig(mode="cls3", ignore_rule="none"),
    )


def make_ready_dataset(
    db: FakeDb,
    storage: FakeStorage,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
    *,
    target_bytes: int = 2_000,
):
    """여러 shard로 나뉜 ready 데이터셋과 종목별 원본 전처리 결과를 만든다."""
    datasets = DatasetRepository(db)
    preset = make_preset()
    dataset = datasets.create(
        name="샘플셋",
        preset_id=1,
        preset_snapshot={},
        timeframe="day",
        feature_columns=list(preset.features),
        symbols=list(symbols),
        splits={symbol: "train" for symbol in symbols},
    )
    expected = {}
    total = 0
    for seed, symbol in enumerate(symbols, start=1):
        result = run_preprocess(make_candles(seed=seed), preset)
        expected[symbol] = result
        shards = build_shards(
            result.frame, result.samples, result.feature_columns, target_bytes=target_bytes
        )
        assert len(shards) > 1, "다중 shard 시나리오를 보장해야 한다"
        for shard in shards:
            path = object_path(dataset["id"], symbol, shard.index, shard.sha256)
            storage.upload(
                DATASET_BUCKET, path, shard.data, content_type=PARQUET_CONTENT_TYPE
            )
            datasets.record_shard(
                dataset_id=dataset["id"],
                symbol=symbol,
                shard_index=shard.index,
                object_path=path,
                size_bytes=len(shard.data),
                row_count=shard.row_count,
                sha256=shard.sha256,
                feature_schema=feature_schema(result.feature_columns),
            )
        datasets.set_symbol_ready(
            dataset["id"],
            symbol,
            sample_count=len(result.samples),
            class_counts={},
            length_stats={},
        )
        total += len(result.samples)
    datasets.finalize_ready(dataset["id"], sample_count=total, class_counts={})
    return datasets, dataset["id"], expected


class TestListSamples:
    def test_pagination_is_stable_across_shards(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, expected = make_ready_dataset(db, storage)
        total_expected = sum(len(result.samples) for result in expected.values())

        full = samples.list_samples(
            datasets, storage, dataset_id, cache_root=tmp_path, limit=10_000
        )
        assert full["total"] == total_expected
        assert [item["index"] for item in full["items"]] == list(range(total_expected))
        # 종목 정렬(symbol asc)이 전역 순번의 기준이다
        aaa_count = len(expected["AAA"].samples)
        assert all(item["symbol"] == "AAA" for item in full["items"][:aaa_count])
        assert all(item["symbol"] == "BBB" for item in full["items"][aaa_count:])
        assert all(item["split"] == "train" for item in full["items"])

        page = samples.list_samples(
            datasets, storage, dataset_id, cache_root=tmp_path, offset=5, limit=7
        )
        assert page["items"] == full["items"][5:12]

    def test_label_filter_keeps_global_indices(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_ready_dataset(db, storage)
        full = samples.list_samples(
            datasets, storage, dataset_id, cache_root=tmp_path, limit=10_000
        )

        seen = 0
        for label in (0, 1, 2):
            filtered = samples.list_samples(
                datasets, storage, dataset_id, cache_root=tmp_path, label=label, limit=10_000
            )
            assert all(item["label"] == label for item in filtered["items"])
            assert filtered["items"] == [
                item for item in full["items"] if item["label"] == label
            ]
            seen += filtered["total"]
        assert seen == full["total"]

    def test_non_ready_dataset_is_rejected(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets = DatasetRepository(db)
        dataset = datasets.create(
            name="생성중",
            preset_id=1,
            preset_snapshot={},
            timeframe="day",
            feature_columns=["Close"],
            symbols=["AAA"],
            splits={"AAA": "train"},
        )
        with pytest.raises(samples.DatasetNotReadyError):
            samples.list_samples(
                datasets, storage, dataset["id"], cache_root=tmp_path
            )


class TestGetSample:
    def test_detail_matches_preprocess_output(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, expected = make_ready_dataset(db, storage)
        aaa = expected["AAA"]
        features = aaa.frame[aaa.feature_columns].astype("float64")

        for position in (0, len(aaa.samples) // 2, len(aaa.samples) - 1):
            sample = aaa.samples[position]
            detail = samples.get_sample(
                datasets, storage, dataset_id, position, cache_root=tmp_path
            )
            assert detail["symbol"] == "AAA"
            assert detail["label"] == sample.label
            assert detail["length"] == sample.length
            assert detail["feature_columns"] == aaa.feature_columns
            window = features.iloc[sample.start_position : sample.end_position + 1]
            assert detail["features"] == window.to_numpy().tolist()

    def test_out_of_range_index(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, expected = make_ready_dataset(db, storage)
        total = sum(len(result.samples) for result in expected.values())
        with pytest.raises(samples.SampleNotFoundError):
            samples.get_sample(datasets, storage, dataset_id, total, cache_root=tmp_path)


class TestShardFailures:
    def test_missing_object_is_explicit(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_ready_dataset(db, storage)
        storage.objects.clear()
        with pytest.raises(samples.SampleAccessError, match="missing or unreadable"):
            samples.list_samples(datasets, storage, dataset_id, cache_root=tmp_path)

    def test_corrupt_object_is_explicit(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_ready_dataset(db, storage)
        storage.corrupt_paths.update(path for _, path in storage.objects)
        with pytest.raises(samples.SampleAccessError, match="checksum mismatch"):
            samples.list_samples(datasets, storage, dataset_id, cache_root=tmp_path)

    def test_row_count_mismatch_is_explicit(self, tmp_path):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_ready_dataset(db, storage)
        db.tables["dataset_shards"][0]["row_count"] += 1
        with pytest.raises(samples.SampleAccessError, match="rows"):
            samples.list_samples(datasets, storage, dataset_id, cache_root=tmp_path)

    def test_disk_cache_serves_after_object_loss(self, tmp_path):
        """내려받은 shard는 해시 검증된 로컬 캐시로 재사용된다 (재생성 가능 캐시)."""
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_ready_dataset(db, storage)
        first = samples.list_samples(
            datasets, storage, dataset_id, cache_root=tmp_path, limit=10_000
        )
        samples._index_cache.clear()
        storage.objects.clear()  # 원격 유실 — 캐시가 그대로 서빙해야 한다
        second = samples.list_samples(
            datasets, storage, dataset_id, cache_root=tmp_path, limit=10_000
        )
        assert second == first
