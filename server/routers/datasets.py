"""데이터셋 메타데이터 조회. 목록/통계는 Postgres 메타데이터만 읽는다
(docs/06 §5). shard 객체 자체는 브라우저에 내려주지 않는다."""

from fastapi import APIRouter, HTTPException

from pivot.storage.datasets import DatasetNotFoundError
from server.deps import dataset_repo

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.get("")
def list_datasets() -> list[dict]:
    return dataset_repo().list()


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
