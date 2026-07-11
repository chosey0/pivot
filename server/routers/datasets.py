"""데이터셋 메타데이터 조회 + 샘플 브라우저 + 삭제. 목록/통계는 Postgres
메타데이터만 읽고(docs/06 §5), 샘플 조회는 서버가 private Storage에서 shard를
내려받아 해시 검증 후 반환한다. object path와 키는 응답에 싣지 않는다."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pivot.dataset import samples
from pivot.storage.datasets import DatasetNotFoundError
from pivot.storage.lifecycle import (
    DatasetDeletionBlockedError,
    DatasetDeletionFailedError,
    delete_dataset,
    run_cleanup,
)
from server.deps import SHARD_CACHE_ROOT, dataset_repo, job_repo, object_storage

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.get("")
def list_datasets() -> list[dict]:
    return dataset_repo().list()


@router.post("/cleanup")
def cleanup() -> dict:
    """orphan Storage 객체 / stale building 데이터셋 / stale job 정리 (멱등)."""
    return run_cleanup(
        datasets=dataset_repo(), jobs=job_repo(), storage=object_storage()
    )


@router.get("/{dataset_id}")
def get_dataset(dataset_id: int) -> dict:
    repo = dataset_repo()
    try:
        dataset = repo.get(dataset_id)
    except DatasetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    symbols = repo.list_symbols(dataset_id)
    shards = repo.list_shards(dataset_id)
    return {
        **dataset,
        "symbols": symbols,
        "shards": [
            # object_path는 내부 저장 경로라 응답에서 제외한다
            {
                "symbol": shard["symbol"],
                "shard_index": shard["shard_index"],
                "size_bytes": shard["size_bytes"],
                "row_count": shard["row_count"],
                "sha256": shard["sha256"],
            }
            for shard in shards
        ],
    }


@router.delete("/{dataset_id}")
def remove_dataset(dataset_id: int) -> dict:
    """객체 목록 확정 → Storage 삭제 → 메타데이터 삭제 (docs/06 §7).

    부분 실패는 dataset_delete job에 남고, 같은 호출을 다시 하면 재시도된다.
    """
    try:
        result = delete_dataset(
            datasets=dataset_repo(),
            jobs=job_repo(),
            storage=object_storage(),
            dataset_id=dataset_id,
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DatasetDeletionBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatasetDeletionFailedError as exc:
        raise HTTPException(502, str(exc)) from exc
    samples.evict(dataset_id, cache_root=SHARD_CACHE_ROOT)
    return result


@router.get("/{dataset_id}/samples")
def list_dataset_samples(
    dataset_id: int,
    label: Annotated[int | None, Query(ge=0, le=2)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    try:
        return samples.list_samples(
            dataset_repo(),
            object_storage(),
            dataset_id,
            cache_root=SHARD_CACHE_ROOT,
            label=label,
            offset=offset,
            limit=limit,
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except samples.DatasetNotReadyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except samples.SampleAccessError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{dataset_id}/samples/{sample_index}")
def get_dataset_sample(dataset_id: int, sample_index: int) -> dict:
    try:
        return samples.get_sample(
            dataset_repo(),
            object_storage(),
            dataset_id,
            sample_index,
            cache_root=SHARD_CACHE_ROOT,
        )
    except (DatasetNotFoundError, samples.SampleNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except samples.DatasetNotReadyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except samples.SampleAccessError as exc:
        raise HTTPException(502, str(exc)) from exc
