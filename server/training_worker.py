"""spawn 가능한 M4 학습 프로세스 진입점."""

from pivot.training.runs import run_training
from server.deps import (
    SHARD_CACHE_ROOT,
    dataset_repo,
    diagnostic_repo,
    job_repo,
    object_storage,
    run_repo,
)


def execute(run_id: int, job_id: int) -> None:
    run_training(
        runs=run_repo(),
        jobs=job_repo(),
        datasets=dataset_repo(),
        diagnostics=diagnostic_repo(),
        storage=object_storage(),
        run_id=run_id,
        job_id=job_id,
        cache_root=SHARD_CACHE_ROOT,
    )
