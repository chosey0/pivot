# Supabase 학습 데이터 저장 설계

Pivot의 프리셋 이후 파생 데이터는 Supabase를 단일 원본으로 사용한다. 관계와 상태를 조회해야
하는 작은 데이터는 Postgres에, 데이터셋 시퀀스와 모델 체크포인트처럼 큰 바이너리는 private
Storage에 저장한다. 이 문서는 M3 이후 저장소 구현의 데이터 계약이다.

## 1. 범위

| 데이터 | 저장 위치 | 이유 |
|---|---|---|
| 원천 캔들, 관심종목 | 로컬 parquet/json | 증분 수집과 차트 구간 조회의 운영 데이터 |
| 전처리 프리셋 | `training_presets` | 버전과 전체 파라미터 보존 |
| 장기 작업과 진행 이벤트 | `jobs`, `job_events` | 재시작 후에도 상태와 실패 원인 추적 |
| 데이터셋 메타데이터 | `datasets`, `dataset_symbols`, `dataset_shards` | 프리셋 스냅샷, 샘플 split 규칙, shard 무결성 |
| 시퀀스 샘플 | `pivot-datasets` bucket | 가변 길이 parquet를 행 단위 JSONB로 저장하지 않음 |
| 품질 리포트 | `diagnostic_reports` | 학습 가능 여부와 재현 가능한 검사 결과 |
| 학습/평가 이력 | `training_runs`, `training_epochs`, `evaluations` | 설정, 곡선, 클래스별 지표를 구조화해 비교 |
| 체크포인트/스케일러/로그 | `pivot-models` bucket + `training_artifacts` | 바이너리와 검색 가능한 메타데이터 분리 |
| 실시간 활성 모델 | `live_deployments` | 검증된 run/artifact 연결과 활성화 이력 보존 |

원천 캔들은 모델 입력의 기반이지만 수집 계층의 운영 데이터이므로 이번 이전 범위에서 제외한다.
프리셋이 적용되어 생성된 데이터부터는 Supabase 없이는 영속 완료로 간주하지 않는다.

## 2. 관계와 불변 조건

- 프리셋은 `(name, version)`으로 식별하며 수정 대신 새 버전을 만든다. 삭제 대신
  `archived_at`을 사용한다.
- 데이터셋은 생성 시 프리셋 전체를 `preset_snapshot`에 복사한다. 이후 프리셋 변경이 기존
  데이터셋의 의미를 바꾸지 않는다. `preset_snapshot`은 봉투(envelope) 구조로,
  프리셋 원본, split 규칙, 종목별 원천 캐시 위치를 함께 보존한다
  (`pivot.dataset.batch.build_snapshot`):
  ```jsonc
  {
    "schema_version": 1,
    "preset_id": 4, "preset_name": "...", "preset_version": 2,
    "preset": { /* training_presets.preset 전체 */ },
    "split": { "method": "stratified_sample_v1", "seed": 42,
               "ratios": { "train": 0.6, "validation": 0.2, "test": 0.2 } },
    "sources": {
      "005930": { "region": "domestic", "exchange": "" },
      "AAPL": { "region": "overseas", "exchange": "ND" }
    },
    "targets": [
      { "symbol": "005930", "region": "domestic", "exchange": "",
        "timeframe": "day", "start": "2025-01-01", "end": "2026-07-15" },
      { "symbol": "005930", "region": "domestic", "exchange": "",
        "timeframe": "min1", "start": "2026-07-01T09:00:00", "end": "2026-07-15T15:30:00" }
    ]
  }
  ```
  저장 시 Pydantic 검증으로 채워진 호환 기본값까지 `preset`에 materialize한다. 따라서 예전
  프리셋 JSON에 `cleaning` 필드가 없어도 데이터셋 스냅샷에는 실제 적용된
  `kronos_adapted_v1` 정책과 모드가 명시된다. schema v1 초기에 저장된 프리셋은 다음 누락값을
  새 기본값으로 덮지 않고 legacy 값으로 materialize해 과거 결과를 재현한다.
  - `fractal.tie_policy` 누락 → `all`
  - `labeling.sample_pairing` 누락 → `latest_opposite_v1`

  저장 데이터의 호환 처리는 `resolve_stored_preset(preset_json, schema_version)` 한 경로로
  단일화한다. `training_presets` list/get, batch, diagnostics, dataset/run snapshot hydration은
  모두 이 resolver를 사용하며 입력 JSON과 DB 원본을 수정하지 않는다. 반면 저장 row와 무관한
  신규 preview/preset 요청은 resolver를 사용하지 않고 새 기본값 `adjacent_markers_v1`을 쓴다.
  새 dataset snapshot은 검증·materialize된 `PreprocessPreset`만 받아 실제 pairing을 반드시
  명시한다. 이미 `ready`인 dataset/run snapshot은 migration하거나 덮어쓰지 않는다.
- 신규 데이터셋은 `dataset_symbols.split = null`로 두고 각 shard 행의 `split` 컬럼에 배정을
  저장한다. 전체 샘플을 라벨 0/1/2별로 정렬·seed 셔플한 뒤 각 클래스 안에서
  train/validation/test를 60/20/20으로 나누는 `stratified_sample_v1` 규칙이다. 같은 샘플 목록과
  seed는 항상 같은 결과를 만든다. 기존 `seeded_shuffle_v1/v2` 데이터셋은 저장된 종목 split을
  그대로 읽어 재현하며 migration하지 않는다.
- 한 데이터셋은 여러 타임프레임과 같은 종목의 여러 요청 기간을 포함할 수 있다. 단일
  타임프레임이면 `datasets.timeframe`에 해당 코드를, 둘 이상이면 `mixed`를 저장한다.
  `dataset_symbols`는 실제 종목당 한 행을 유지하고 여러 수집 항목의 샘플/클래스/길이/클리닝
  통계를 집계한다. 개별 의미는 snapshot의 `targets`와 shard 행의 `source_key`, `timeframe`으로
  보존하므로 DB schema 변경은 필요 없다. `source_key`는
  `region|exchange|symbol|timeframe|start|end` 형식이며 같은 종목의 로컬 `sample_index`와 함께
  split 재계산의 유일 키가 된다.
- shard와 artifact는 `size_bytes`, SHA-256, MIME type, immutable object path를 함께 저장한다.
- 종목별 `dataset_symbols.length_stats.cleaning`에는 정책/논문 URL, 원본·유지·제외 봉 수,
  세그먼트 길이, 구조적 경계와 사유별 카운트, 실제 사용 임계값을 저장한다. 이는 학습 shard를
  다시 읽지 않고도 클리닝 provenance를 진단하기 위한 메타데이터다.
- 종목별 `dataset_symbols.length_stats.pairing_stats`에는 `rule`, `adjacent_edges`,
  `unpaired_markers`, `dropped_invalid_position`, `dropped_label2`를 저장한다. legacy top-level
  `dropped_ignore`/`dropped_unpaired`의 의미와 값은 보존한다. `length_stats`의 `points`,
  `dropped_nan`과 종목 `sample_count`를 함께 사용해 adjacent 샘플이 누락 없이 집계됐는지
  다음 식으로 검증한다.
  `points = adjacent_edges + unpaired_markers`,
  `adjacent_edges = samples + dropped_label2 + dropped_nan + dropped_invalid_position`.
- `dataset_symbols.length_stats.overlap_clusters`에는 plateau 후보/제거 수와 생성 시점의 잔여
  overlap cluster 통계를 저장한다. 데이터셋 진단은 기존 데이터셋에도 동일 기준을 적용할 수
  있도록 shard의 샘플 메타데이터에서 종목별 통계를 다시 계산하고 무결성을 검증한다.
- 데이터셋은 모든 shard 업로드와 메타데이터 기록이 끝난 뒤에만 `ready`가 된다.
- 학습 run은 데이터셋 스냅샷과 하이퍼파라미터를 보존한다. epoch 지표와 최종 평가는 run과
  별도 행으로 저장해 진행 중 조회와 비교를 지원한다.
- 실시간 모델 활성화는 `succeeded` run에 속한 `best_checkpoint`만 허용한다. 활성화 이력은
  `live_deployments`에 불변 행으로 추가하고 partial unique index로 활성 행을 최대 하나로
  제한한다. `activate_live_deployment()` security-invoker RPC가 기존 행 비활성화와 신규 행
  삽입을 한 트랜잭션으로 처리한다. 함수 실행 권한은 `service_role`에만 부여한다.
  혼합 타임프레임 데이터셋의 checkpoint는 저장 프리셋의 기본 `timeframe`으로 실시간 엔진을
  활성화한다. `mixed`는 학습 데이터셋 메타값이며 실시간 봉 집계 코드로 전달하지 않는다.

기본 DDL은 `supabase/migrations/20260710221442_training_storage.sql`, M5 배포 포인터는
`supabase/migrations/20260713100000_live_deployments.sql`이 기준이다.

## 3. Storage 경로

두 bucket은 모두 private이며 파일당 상한은 50 MiB다. 파일은 작은 단위로 shard하고 같은
경로를 덮어쓰지 않는다.

```text
pivot-datasets/
  datasets/{dataset_id}/{symbol}/part-{shard_index:05d}-{sha256_prefix}.parquet

pivot-models/
  runs/{run_id}/checkpoints/epoch-{epoch:04d}-{sha256_prefix}.pt
  runs/{run_id}/scalers/{name}-{sha256_prefix}.json
  runs/{run_id}/reports/{kind}-{sha256_prefix}.json
  runs/{run_id}/logs/{name}-{sha256_prefix}.txt
```

`sha256_prefix`는 전체 SHA-256의 앞 12자다. 전체 해시는 메타데이터 행에 저장한다. 현재
bucket 제한에 맞춰 각 파일은 50 MiB 미만으로 만든다. 큰 데이터셋은 종목별, 필요하면 종목
내 shard 번호로 나눈다.

## 4. 쓰기 수명주기

데이터셋 생성은 다음 순서를 따른다.

1. `datasets.status = building`과 대상 `dataset_symbols`를 생성한다.
2. 전 수집 항목을 1차 전처리해 클래스별 `(source_key, sample_index)`를 모으고 60/20/20 split을 확정한다.
3. 수집 항목별 샘플을 다시 전처리해 확정된 `split`과 함께 parquet shard로 직렬화하고 SHA-256과 행 수를 계산한다
   (`pivot/dataset/shards.py`, 행 = 샘플 1개, 가변 길이 시퀀스는 `features`
   list<list<float64>> 컬럼). 신규 shard는 출처 구분을 위한 `source_key`/`timeframe`과 overlap
   진단 재계산을 위한 `start_position`/`end_position`도 보존한다. 이전 shard는 같은 시작 시각
   기준 근사 통계로 표시한다.
4. private Storage에 불변 경로로 업로드한다 (`x-upsert: false`).
5. 업로드한 객체를 다시 내려받아 SHA-256이 일치할 때만 `dataset_shards`를 기록한다.
6. 모든 종목의 shard가 준비되면 집계 통계를 기록하고 `datasets.status = ready`로 바꾼다.

클리닝은 로컬 원천 parquet를 변경하지 않는다. `report_only`는 통계만 저장하고 기존 샘플을
그대로 생성한다. `filter`는 정상 세그먼트별로 지표·라벨·샘플을 독립 계산한 결과만 shard에
기록하며, 정책 입력은 `preset_snapshot`, 종목별 결과는 `length_stats.cleaning`, 품질 판정은
`diagnostic_reports`에 남아 세 층에서 재현 가능하다.

수집 항목 하나의 실패는 해당 종목의 `dataset_symbols.error`에 기록하고 다음 항목을 계속 처리하되,
실패 항목이 하나라도 있으면 데이터셋은 `ready`가 아니라 `failed`로 마감한다
(부분 데이터셋으로 학습을 시작하지 않는다). 메타데이터에 연결되지 않은 업로드는
정리 작업이 삭제한다. 재시도는 기존 경로를 덮어쓰지 않고 새 경로를 사용한다. 학습 artifact도
먼저 Storage에 업로드하고 검증한 뒤 `training_artifacts`를 기록하는 같은 순서를 따른다.

SSE는 FastAPI가 현재 진행 상황을 전달하는 전송 수단이다. 복구 가능한 작업 상태와 이벤트는
각각 `jobs`, `job_events`에 남긴다.

## 5. 읽기와 로컬 캐시

- 데이터셋 목록, 필터, 통계는 Postgres 메타데이터만 조회한다.
- 샘플 상세와 학습 로더는 필요한 shard만 Storage에서 내려받는다.
- 내려받은 파일은 `data/tmp/`에 캐시할 수 있지만 삭제 가능해야 하며 Supabase와 충돌할 때
  복구 근거로 사용하지 않는다.
- 체크섬 불일치, 누락 객체, `ready`가 아닌 데이터셋은 학습 시작을 차단한다.
- 학습 loader는 shard의 원본 float 시퀀스를 샘플별로 표준화한 뒤 0 padding하고 length/mask를
  함께 전달한다. shard 행에 저장된 split을 재배정하지 않으며 train, validation, test 중 빈
  split이 있으면 시작하지 않는다. 최신 데이터셋 진단이 `failed`인 경우도 차단한다.
- 체크포인트는 `runs/{run_id}/checkpoints/best-{epoch}-{sha256_prefix}.pt`에 불변 업로드하고
  재다운로드 SHA-256 검증 후에만 `training_artifacts` 행을 기록한다. API 응답에는 private
  bucket의 `object_path`를 노출하지 않는다.
- 샘플 브라우저(`pivot/dataset/samples.py`)의 전역 샘플 순번은 shard 정렬
  (symbol asc, shard_index asc)과 shard 내 행 순서로 정해진다 — 데이터셋이 불변이므로
  순번도 안정적이다. 목록/필터 인덱스는 `features` 컬럼을 제외한 parquet 컬럼 읽기로
  만들고, 단건 상세만 해당 shard 하나를 읽는다. 내려받은 shard는
  `data/tmp/shards/{dataset_id}/{sha256}.parquet`에 캐시하며 캐시 히트도 해시를 재검증한다
  (불일치 시 폐기 후 재다운로드, 파일 수 상한으로 정리). `ready`가 아닌 데이터셋의 샘플
  조회는 409로 거부하고, 누락/손상 shard는 502로 명시적으로 드러낸다.

## 6. 접근 제어

- 모든 학습 테이블은 RLS를 활성화하고 `anon`, `authenticated` 권한을 제거한다.
- 클라이언트 정책은 만들지 않는다. RLS 무정책의 기본 거부가 의도된 상태이며 service role은
  서버에서만 사용한다.
- 두 Storage bucket은 private이며 브라우저용 object policy를 만들지 않는다.
- FastAPI만 서버 전용 Supabase secret/service-role 키로 PostgREST와 Storage API를 호출한다.
- 프론트엔드 번들, API 응답, 로그에 secret/service-role 키를 포함하지 않는다.
- Storage 객체 생성/조회/삭제는 Storage API로 수행한다. 애플리케이션이 `storage.objects`를
  직접 수정하지 않는다.
- `live_deployments`도 RLS를 활성화하고 `anon`, `authenticated`, `PUBLIC`의 테이블/RPC 접근을
  제거한다. 브라우저에는 deployment id와 공개 run 요약만 반환하고 artifact 경로·해시는
  반환하지 않는다.

## 7. 삭제와 보존

- 프리셋은 archive가 기본이다. 사용자가 영구 삭제를 선택하면 해당 버전을 참조하는 데이터셋이
  없는 경우만 허용하며, 기존 진단 리포트의 `preset_id`는 FK 규칙에 따라 null이 된다.
- 데이터셋 또는 run 삭제는 연결된 Postgres 행과 Storage 객체 목록을 먼저 확정한 뒤 수행한다.
  객체 삭제 성공 후 메타데이터를 삭제하며, 부분 실패는 재시도 가능한 job으로 남긴다.
- 모델 재현에 필요한 데이터셋 스냅샷, run 설정, 최종 평가, 선택 체크포인트는 run 보존 기간과
  함께 유지한다.
- orphan 객체와 장시간 `building`/`running` 상태를 찾는 정기 정리 작업을 M3/M4 운영 기능에
  포함한다.

M3-B 구현 (`pivot/storage/lifecycle.py`):

- **취소**: `POST /api/jobs/{id}/cancel`이 조건부 갱신으로 queued/running → cancelled를
  durable하게 전이하고, batch worker는 종목 사이와 shard 업로드 사이에서 상태를 확인해
  협조적으로 중단한다. 취소된 batch의 데이터셋은 `failed` + `cancelled by user`로 마감한다.
  마지막 종목 처리 후 도착한 취소도 `ready` 확정을 막는다. 반대로 `ready`로 확정된
  데이터셋은 job/event 텔레메트리 실패로 절대 강등하지 않는다.
- **데이터셋 삭제** (`DELETE /api/datasets/{id}`): ① `dataset_shards`에서 객체 목록을
  확정해 `dataset_delete` job payload에 얼리고 ② Storage 객체를 삭제한 뒤 ③ 성공 시에만
  메타데이터를 삭제한다(cascade). 부분 실패 시 메타데이터가 남아 같은 DELETE 호출이
  그대로 재시도가 되고, 실패 원인은 job에 남는다. `building` 데이터셋은 삭제를 거부한다
  (batch 취소 → failed 전환 후 삭제). `dataset_delete` job kind는
  `20260711064111_dataset_delete_job_kind.sql` 마이그레이션이 추가하며 원격 적용을 완료했다.
- **학습 run 삭제** (`DELETE /api/runs/{id}`): queued/running run과 현재 활성
  `live_deployments`에서 참조 중인 run은 거부한다. terminal run은 artifact의 bucket/path 목록을 `run_delete` job에
  확정하고 Storage 객체를 먼저 삭제한 뒤 run 메타데이터를 삭제한다. epoch/evaluation/artifact는
  FK cascade로 함께 제거되고 비활성 배포 이력도 run과 함께 제거된다. 원래 training job은 감사 이력으로 남는다. `run_delete` kind는
  `20260713133933_run_delete_job_kind.sql`이 추가하고, 비활성 배포 cascade는
  `20260714160724_cascade_inactive_live_deployments.sql`이 추가한다. 둘 다 원격 적용을 완료했다.
- **정리** (`POST /api/datasets/cleanup`, 멱등): ① 24시간 넘게 queued/running인 job을
  cancelled로 마감 ② 활성 job이 없는데 24시간 넘게 building인 데이터셋을 failed로 마감
  ③ `dataset_shards`가 참조하지 않는 1시간 이상 지난 `pivot-datasets` 객체를 삭제한다.
  building 데이터셋 폴더(업로드 진행 중일 수 있음)와 `pivot-models`(학습 artifact)는
  건드리지 않는다. age 임계값은 보수적으로 잡아 진행 중인 업로드(업로드→메타 기록 사이
  창)를 보호한다.
