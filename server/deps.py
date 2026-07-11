"""서버 공용 설정/의존성.

Supabase 클라이언트/repository는 서버 전용 secret 키로 만들어지는 싱글턴이다.
키·객체 URL은 응답에 절대 싣지 않는다 (docs/06 §6).
"""

import os
from functools import lru_cache
from pathlib import Path

from pivot.storage.datasets import DatasetRepository
from pivot.storage.diagnostics import DiagnosticReportRepository
from pivot.storage.jobs import JobRepository
from pivot.storage.presets import PresetRepository
from pivot.storage.supabase import PostgrestClient, StorageObjectClient

DATA_ROOT = Path(os.getenv("PIVOT_DATA_DIR", "data"))
META_DIR = DATA_ROOT / "meta"
SHARD_CACHE_ROOT = DATA_ROOT / "tmp" / "shards"  # 재생성 가능한 shard 다운로드 캐시


@lru_cache(maxsize=1)
def _postgrest() -> PostgrestClient:
    return PostgrestClient()


@lru_cache(maxsize=1)
def object_storage() -> StorageObjectClient:
    return StorageObjectClient()


@lru_cache(maxsize=1)
def preset_repo() -> PresetRepository:
    return PresetRepository(_postgrest())


@lru_cache(maxsize=1)
def job_repo() -> JobRepository:
    return JobRepository(_postgrest())


@lru_cache(maxsize=1)
def dataset_repo() -> DatasetRepository:
    return DatasetRepository(_postgrest())


@lru_cache(maxsize=1)
def diagnostic_repo() -> DiagnosticReportRepository:
    return DiagnosticReportRepository(_postgrest())
