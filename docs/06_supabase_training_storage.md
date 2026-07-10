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
| 데이터셋 메타데이터 | `datasets`, `dataset_symbols`, `dataset_shards` | 프리셋 스냅샷, 종목 단위 split, shard 무결성 |
| 시퀀스 샘플 | `pivot-datasets` bucket | 가변 길이 parquet를 행 단위 JSONB로 저장하지 않음 |
| 품질 리포트 | `diagnostic_reports` | 학습 가능 여부와 재현 가능한 검사 결과 |
| 학습/평가 이력 | `training_runs`, `training_epochs`, `evaluations` | 설정, 곡선, 클래스별 지표를 구조화해 비교 |
| 체크포인트/스케일러/로그 | `pivot-models` bucket + `training_artifacts` | 바이너리와 검색 가능한 메타데이터 분리 |

원천 캔들은 모델 입력의 기반이지만 수집 계층의 운영 데이터이므로 이번 이전 범위에서 제외한다.
프리셋이 적용되어 생성된 데이터부터는 Supabase 없이는 영속 완료로 간주하지 않는다.

## 2. 관계와 불변 조건

- 프리셋은 `(name, version)`으로 식별하며 수정 대신 새 버전을 만든다. 삭제 대신
  `archived_at`을 사용한다.
- 데이터셋은 생성 시 프리셋 전체를 `preset_snapshot`에 복사한다. 이후 프리셋 변경이 기존
  데이터셋의 의미를 바꾸지 않는다.
- `dataset_symbols.split`은 생성 중에는 null일 수 있고, 데이터셋을 `ready`로 전환하기 전에는
  `train`, `validation`, `test` 중 하나로 확정한다. 종목 단위 분할을 저장해 샘플 누수 없이
  같은 학습을 재현한다.
- shard와 artifact는 `size_bytes`, SHA-256, MIME type, immutable object path를 함께 저장한다.
- 데이터셋은 모든 shard 업로드와 메타데이터 기록이 끝난 뒤에만 `ready`가 된다.
- 학습 run은 데이터셋 스냅샷과 하이퍼파라미터를 보존한다. epoch 지표와 최종 평가는 run과
  별도 행으로 저장해 진행 중 조회와 비교를 지원한다.

전체 DDL은 `supabase/migrations/20260710221442_training_storage.sql`이 기준이다.

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
2. 종목별 샘플을 로컬 임시 파일로 만들고 SHA-256과 행 수를 계산한다.
3. private Storage에 불변 경로로 업로드한다.
4. 업로드된 객체와 검증 값이 일치할 때 `dataset_shards`를 기록한다.
5. 모든 종목의 shard가 준비되면 집계 통계를 기록하고 `datasets.status = ready`로 바꾼다.

실패하면 데이터셋을 `failed`로 전환하고 원인을 보존한다. 메타데이터에 연결되지 않은 업로드는
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

## 6. 접근 제어

- 모든 학습 테이블은 RLS를 활성화하고 `anon`, `authenticated` 권한을 제거한다.
- 클라이언트 정책은 만들지 않는다. RLS 무정책의 기본 거부가 의도된 상태이며 service role은
  서버에서만 사용한다.
- 두 Storage bucket은 private이며 브라우저용 object policy를 만들지 않는다.
- FastAPI만 서버 전용 Supabase secret/service-role 키로 PostgREST와 Storage API를 호출한다.
- 프론트엔드 번들, API 응답, 로그에 secret/service-role 키를 포함하지 않는다.
- Storage 객체 생성/조회/삭제는 Storage API로 수행한다. 애플리케이션이 `storage.objects`를
  직접 수정하지 않는다.

## 7. 삭제와 보존

- 프리셋은 참조 중일 수 있으므로 archive가 기본이다.
- 데이터셋 또는 run 삭제는 연결된 Postgres 행과 Storage 객체 목록을 먼저 확정한 뒤 수행한다.
  객체 삭제 성공 후 메타데이터를 삭제하며, 부분 실패는 재시도 가능한 job으로 남긴다.
- 모델 재현에 필요한 데이터셋 스냅샷, run 설정, 최종 평가, 선택 체크포인트는 run 보존 기간과
  함께 유지한다.
- orphan 객체와 장시간 `building`/`running` 상태를 찾는 정기 정리 작업을 M3/M4 운영 기능에
  포함한다.
