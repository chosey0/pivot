"""실제 Supabase에 임시 프리셋/데이터셋을 생성해 저장 계약을 검증하는 smoke test.

`.env`의 서버 전용 키를 사용하며(값은 출력하지 않는다), 생성한 행과 Storage
객체는 검증 후 반드시 정리한다. 환경변수가 없으면 skip된다.
"""

import hashlib
import uuid
import warnings

import pytest

from pivot.config import FractalConfig, LabelingConfig, PreprocessPreset
from pivot.dataset import samples as sample_browser
from pivot.dataset.batch import build_snapshot, run_batch, split_config
from pivot.diagnostics import quality
from pivot.env import env_value
from pivot.ingestion.cache import cache_path, replace_cache
from pivot.storage.datasets import DatasetNotFoundError, DatasetRepository
from pivot.storage.diagnostics import DiagnosticReportRepository
from pivot.storage.jobs import JobRepository
from pivot.storage.lifecycle import delete_dataset
from pivot.storage.presets import PresetRepository
from pivot.storage.supabase import (
    DATASET_BUCKET,
    PostgrestClient,
    StorageObjectClient,
)

from fakes import make_candles

pytestmark = pytest.mark.skipif(
    not env_value("SUPABASE_URL")
    or not (env_value("SUPABASE_SERVICE_ROLE_KEY") or env_value("SUPABASE_SECRET_KEY")),
    reason="Supabase 환경변수가 설정되지 않음",
)

BROKER = "kiwoom"
SYMBOL = "SMOKE0"


def test_batch_roundtrip_against_real_supabase(tmp_path):
    db = PostgrestClient()
    storage = StorageObjectClient()
    presets = PresetRepository(db)
    jobs = JobRepository(db)
    datasets = DatasetRepository(db)

    tag = uuid.uuid4().hex[:8]
    path = cache_path(tmp_path, BROKER, "day", SYMBOL)
    replace_cache(path, make_candles(seed=11))

    preset_model = PreprocessPreset(
        name=f"pytest-smoke-{tag}",
        fractal=FractalConfig(n=5),
        features=["Open", "High", "Low", "Close"],
        labeling=LabelingConfig(mode="cls3", ignore_rule="none"),
    )

    preset_row = dataset = job = None
    try:
        preset_row = presets.create(preset_model)
        assert preset_row["version"] == 1

        dataset = datasets.create(
            name=f"pytest-smoke-ds-{tag}",
            preset_id=preset_row["id"],
            preset_snapshot=build_snapshot(preset_row, split_config()),
            timeframe="day",
            feature_columns=list(preset_model.features),
            symbols=[SYMBOL],
            splits={},
        )
        job = jobs.create(
            kind="preprocess_batch",
            payload={"dataset_id": dataset["id"], "symbols": [SYMBOL]},
            total_items=1,
        )

        run_batch(
            jobs=jobs,
            datasets=datasets,
            storage=storage,
            job_id=job["id"],
            dataset_id=dataset["id"],
            preset=preset_model,
            symbols=[SYMBOL],
            data_root=tmp_path,
            broker=BROKER,
        )

        job_row = jobs.get(job["id"])
        assert job_row["status"] == "succeeded", job_row["error"]
        dataset_row = datasets.get(dataset["id"])
        assert dataset_row["status"] == "ready"
        assert dataset_row["sample_count"] > 0

        shards = datasets.list_shards(dataset["id"])
        assert shards, "shard 메타데이터가 기록돼야 한다"
        blob = storage.download(DATASET_BUCKET, shards[0]["object_path"])
        assert hashlib.sha256(blob).hexdigest() == shards[0]["sha256"]
        assert len(blob) == shards[0]["size_bytes"]

        events = jobs.events_after(job["id"])
        assert events[-1]["event_type"] == "dataset_ready"

        # ── M3-B: 샘플 브라우저 — 실제 Storage에서 shard를 내려받아 검증
        sample_browser.evict(dataset["id"])
        cache_root = tmp_path / "shard-cache"
        page = sample_browser.list_samples(
            datasets, storage, dataset["id"], cache_root=cache_root, limit=5
        )
        assert page["total"] == dataset_row["sample_count"]
        assert [item["index"] for item in page["items"]] == list(range(len(page["items"])))
        detail = sample_browser.get_sample(
            datasets, storage, dataset["id"], 0, cache_root=cache_root
        )
        assert detail["feature_columns"] == list(preset_model.features)
        assert len(detail["features"]) == detail["length"]

        # ── M3-B: 데이터셋 진단 리포트 저장/조회
        reports = DiagnosticReportRepository(db)
        quality_report = quality.diagnose_dataset(
            datasets.get(dataset["id"]),
            datasets.list_symbols(dataset["id"]),
            datasets.list_shards(dataset["id"]),
            sample_split_stats=sample_browser.sample_split_stats(
                datasets,
                storage,
                dataset["id"],
                cache_root=cache_root,
                seed=42,
            ),
        )
        report_row = None
        try:
            report_row = reports.create(
                target_type="dataset",
                status=quality_report["status"],
                summary=quality_report["summary"],
                report={"checks": quality_report["checks"], "input": quality_report["input"]},
                dataset_id=dataset["id"],
                preset_id=preset_row["id"],
            )
            fetched = reports.get(report_row["id"])
            assert fetched["status"] in ("passed", "warning")
            assert fetched["report"]["checks"]
        finally:
            if report_row is not None:
                reports.delete(report_row["id"])

        # ── M3-B: 삭제 흐름(객체 → 메타데이터)을 실제 정리에 사용
        dataset_id = dataset["id"]
        delete_job_id = None
        try:
            result = delete_dataset(
                datasets=datasets, jobs=jobs, storage=storage, dataset_id=dataset_id
            )
            delete_job_id = result["job_id"]
            with pytest.raises(DatasetNotFoundError):
                datasets.get(dataset_id)
            dataset = None  # finally의 수동 정리를 건너뛴다
        except RuntimeError as exc:
            if "jobs_kind_check" not in str(exc):
                raise
            # dataset_delete kind 마이그레이션 미적용 — 수동 정리로 폴백
            warnings.warn(
                "supabase/migrations/20260711064111_dataset_delete_job_kind.sql이 "
                "아직 적용되지 않아 삭제 job 검증을 건너뜁니다",
                stacklevel=1,
            )
        finally:
            if delete_job_id is not None:
                jobs.delete(delete_job_id)
            sample_browser.evict(dataset_id, cache_root=cache_root)
    finally:
        # 정리 순서: Storage 객체 → 데이터셋(cascade) → job → 프리셋
        if dataset is not None:
            paths = [shard["object_path"] for shard in datasets.list_shards(dataset["id"])]
            storage.remove(DATASET_BUCKET, paths)
            datasets.delete(dataset["id"])
        if job is not None:
            jobs.delete(job["id"])
        if preset_row is not None:
            presets.delete(preset_row["id"])
