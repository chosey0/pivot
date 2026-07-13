# M4 학습·평가 병렬 구현 작업 문서

## 1. 목적과 완료 기준

M4는 Supabase의 `ready` 데이터셋을 사용해 모델을 학습하고, 학습 이력·평가 결과·검증된
체크포인트를 저장하며, 웹에서 진행 상황과 실제 라벨 대비 예측을 검수할 수 있게 만드는
단계다.

완료 기준은 다음과 같다.

1. 웹에서 `ready` 데이터셋과 학습 설정을 선택해 run을 시작한다.
2. 학습은 FastAPI 이벤트 루프와 분리된 프로세스에서 실행된다.
3. epoch별 train/validation 지표가 Supabase에 저장되고 SSE로 전달된다.
4. validation/test confusion matrix와 클래스별 precision/recall/F1을 확인한다.
5. 체크포인트는 `pivot-models` private bucket에 업로드한 뒤 SHA-256을 재검증한다.
6. 종목 차트에서 실제 프랙탈 라벨과 모델 예측을 함께 검수한다.
7. 취소·실패·완료 상태가 재시작 후에도 추적 가능한 durable 상태로 남는다.

## 2. 확정된 베이스라인 계약

- 입력 데이터는 Supabase의 `ready` 데이터셋만 허용하며 shard 누락·체크섬 불일치·진단
  `failed`는 학습 시작을 차단한다.
- 데이터셋에 저장된 종목 단위 `train`/`validation`/`test` split을 그대로 사용한다.
- shard에는 원본 float 피처를 유지하고, 샘플 단위 StandardScaler 변환은 loader에서 수행한다.
- 가변 길이 입력은 0 padding과 mask/length를 사용해 padding이 표현에 섞이지 않게 한다.
- 라벨은 `0=프랙탈 저점`, `1=프랙탈 고점`, `2=무시`인 3클래스다.
- 기본 손실은 3클래스 CrossEntropy이며 sampler/class weight는 run 설정으로 선택한다.
- 모델 선택 기본 지표는 `validation macro F1`이다. accuracy와 클래스별 지표도 함께 기록한다.
- seed, 피처 순서, 라벨 매핑, scaling, padding, 모델 설정과 dataset preset의
  `labeling.sample_pairing`을 immutable run snapshot에 저장한다. 기존 snapshot에 pairing 필드가
  없으면 `latest_opposite_v1`로 hydrate해 과거 학습 데이터 의미를 보존한다.
- 먼저 `cnn1d_legacy_v1` 재현 베이스라인을 만든 뒤 시간축 kernel을 사용하는
  `cnn1d_temporal_v1`을 비교한다.
- Kronos `filter`, Volume/Amount 무작위 마스킹, sampler 비교는 베이스라인 완료 후 별도 run으로
  수행한다. 증강은 train split에만 적용한다.
- 학습과 M5 추론은 동일한 torch 비의존 transform 함수를 사용한다.

## 3. 동시 작업 격리

### 3.1 Git 작업 공간

이 문서와 §2의 계약 변경을 먼저 `main`에 커밋한 뒤 worktree를 만든다. 그래야 두 branch가
같은 계약 commit에서 시작한다. `main`은 이후 통합 전용으로 유지하고 작업자를 별도
worktree와 branch로 격리한다.

```bash
git worktree add ../pivot-m4-core -b feat/m4-core main
git worktree add ../pivot-m4-ui -b feat/m4-ui main
```

| 작업자 | 경로 | 브랜치 | 역할 |
|---|---|---|---|
| Codex | `../pivot-m4-core` | `feat/m4-core` | 도메인·학습·저장소·API·테스트·문서 |
| Claude Code | `../pivot-m4-ui` | `feat/m4-ui` | React UI·프론트 API adapter·Playwright |
| 통합 | 현재 저장소 | `main` | 순차 병합과 실제 API E2E |

같은 worktree를 두 작업자가 동시에 사용하지 않는다. 각 작업자는 상대 branch의 파일을 직접
수정하거나 cherry-pick하지 않고 이 문서의 API 계약만 경계로 사용한다.

### 3.2 서버와 외부 상태

- Core API 개발 포트: `8001`
- UI Vite 개발 포트: `5174`
- 통합 검증 포트: `8000` / `5173`
- Supabase migration과 실제 쓰기 smoke는 Codex만 수행한다.
- Claude Code는 FastAPI 또는 Playwright request interception만 사용하며 Supabase에 직접
  접근하지 않는다.
- smoke 데이터는 `m4-core-smoke-*` 접두사를 사용하고 테스트 종료 시 정리한다.
- 기존 `ready` 데이터셋과 사용자가 만든 run/artifact를 수정하거나 삭제하지 않는다.

## 4. 파일 소유권

### 4.1 Codex 전용

```text
pivot/dataset/transforms.py
pivot/dataset/loader.py
pivot/models/**
pivot/training/**
pivot/storage/runs.py
server/routers/runs.py
server/deps.py
server/main.py
tests/**
pyproject.toml
supabase/migrations/**
docs/**
AGENTS.md
```

Codex는 M4 core 작업 중 `web/**`를 수정하지 않는다.

### 4.2 Claude Code 전용

```text
web/src/pages/Training.tsx
web/src/pages/Training.css
web/src/features/training/**
web/src/components/training/**
web/src/api/training.ts
web/src/App.tsx
web/tests/** 또는 기존 프론트 e2e 위치
```

Claude Code는 `pivot/**`, `server/**`, `supabase/**`, `pyproject.toml`, `docs/**`, `AGENTS.md`를
수정하지 않는다. 공통 `App.css`는 수정하지 않고 `Training.css`를 `Training.tsx`에서 직접
import한다. 공용 차트를 확장해야 한다면 기존 파일을 수정하기보다 training 전용 wrapper나
primitive를 추가한다.

## 5. M4 HTTP·SSE 계약

브라우저는 FastAPI만 호출한다. Supabase secret, private object path, signed URL은 응답에
포함하지 않는다. 메타데이터 시간은 ISO 8601 문자열이고, 차트 시간은 기존 차트 계약에 맞춰
일봉 `yyyy-mm-dd`, 분·틱봉 unix 초를 사용한다. 확률·지표는 `number`, 라벨은 `0 | 1 | 2`다.

### 5.1 공통 타입

```ts
type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
type ModelType = 'cnn1d_legacy_v1' | 'cnn1d_temporal_v1'
type SamplerType = 'none' | 'weighted'

interface TrainingConfig {
  model: ModelType
  epochs: number
  batch_size: number
  learning_rate: number
  sampler: SamplerType
  seed: number
  scaling: 'sample_standard_v1'
  padding: 'zero_masked_v1'
  best_metric: 'val_macro_f1'
}

interface RunSummary {
  id: number
  name: string
  dataset_id: number
  dataset_name: string
  job_id: number | null
  status: RunStatus
  config: TrainingConfig
  device: string | null
  best_epoch: number | null
  best_metric_name: string | null
  best_metric_value: number | null
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

interface EpochMetrics {
  train_loss: number
  train_accuracy: number
  train_macro_f1: number
  validation_loss: number
  validation_accuracy: number
  validation_macro_f1: number
  learning_rate: number
}

interface EpochRow {
  run_id: number
  epoch: number
  metrics: EpochMetrics
  created_at: string
}

interface ClassMetrics {
  precision: number
  recall: number
  f1: number
  support: number
}

interface EvaluationResult {
  id: number
  run_id: number
  dataset_id: number
  split: 'validation' | 'test'
  metrics: Record<string, number>
  confusion_matrix: number[][]
  per_class_metrics: Record<'0' | '1' | '2', ClassMetrics>
  created_at: string
}

interface ArtifactSummary {
  id: number
  epoch: number | null
  kind: 'checkpoint' | 'best_checkpoint' | 'scaler' | 'history' | 'log' | 'report'
  size_bytes: number
  sha256: string
  metadata: Record<string, unknown>
  created_at: string
}
```

### 5.2 Endpoint

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/api/runs` | - | `RunSummary[]` |
| POST | `/api/runs` | `{name, dataset_id, config}` | `{run_id, job_id}` |
| GET | `/api/runs/{id}` | - | `{run, epochs, evaluations, artifacts}` |
| GET | `/api/runs/{id}/events` | `Last-Event-ID` 지원 | SSE |
| POST | `/api/runs/{id}/stop` | - | `{run_id, status: 'cancelled'}` |
| POST | `/api/runs/{id}/evaluate` | `{symbol, split}` | `PredictionEvaluation` |

Run 생성 시 허용 범위 밖 숫자, `ready`가 아닌 데이터셋, 진단 실패, shard 무결성 실패는
4xx로 거부한다. 시작 이후 실패는 run/job을 `failed`로 마감하고 `error`에 사용자 표시 가능한
원인을 기록한다. 이미 terminal인 run의 stop은 멱등 응답을 반환한다.

### 5.3 예측 차트 계약

차트 캔들은 기존 `/api/chart/{symbol}`을 사용하고 평가 API는 마커만 반환한다.
평가 API는 shard의 sample end time을 차트 시간 규약으로 직렬화한다. UI는 가장 늦은 예측
시각 직후를 `before`로 지정하고 최대 20,000봉을 요청해 과거 split의 예측도 최근 봉에
가려지지 않게 한다.

```ts
interface PredictionPoint {
  sample_index: number
  time: string | number
  actual_label: 0 | 1 | 2
  predicted_label: 0 | 1 | 2
  probabilities: [number, number, number]
  correct: boolean
}

interface PredictionEvaluation {
  run_id: number
  dataset_id: number
  symbol: string
  timeframe: string
  split: 'validation' | 'test'
  points: PredictionPoint[]
}
```

### 5.4 SSE 이벤트

| event | data |
|---|---|
| `run` | `RunSummary` 최신 snapshot |
| `epoch` | `EpochRow` |
| `evaluation` | `EvaluationResult` |
| `artifact` | `ArtifactSummary` |
| `run_succeeded` | `{run_id}` |
| `run_failed` | `{run_id, error}` |
| `run_cancelled` | `{run_id}` |
| `heartbeat` | `{run_id}` |

영속 상태와 이벤트는 Supabase가 원본이고 SSE는 전달 계층이다. 재연결은 `Last-Event-ID`로
이미 처리한 durable event를 건너뛴다. UI는 SSE 단절 시 `GET /api/runs/{id}`로 최신 상태를
재조회한다.

## 6. 구현 분할

### 6.1 Codex: M4 core

1. dataset loader와 공용 transform
2. legacy/temporal CNN1D 및 padding mask 적용
3. 학습·평가·metric 순수 로직
4. run/epoch/evaluation/artifact repository
5. 별도 process worker, 취소, crash 마감
6. checkpoint 업로드·재다운로드 SHA-256 검증
7. `/api/runs`와 SSE, 예측 평가 API
8. 단위·통합·실 Supabase smoke와 문서 갱신

Core 완료 기준은 CLI/API로 기존 `ready` 데이터셋의 작은 run을 끝내고 run, epochs,
evaluation, checkpoint가 Supabase에서 재조회되는 것이다.

### 6.2 Claude Code: M4 UI

1. run 목록과 상태·best metric 표시
2. 새 run 설정 폼과 dataset snapshot 요약
3. 시작·중단·새로고침 동작
4. SSE 기반 epoch 곡선과 재연결 fallback
5. confusion matrix와 클래스별 P/R/F1
6. checkpoint 목록과 best checkpoint 강조
7. 종목·split 선택, 실제 라벨 대 예측 마커, 오분류 필터
8. loading/empty/error/cancelled 상태와 접근성
9. Playwright request interception fixture 기반 UI 검증

프로덕션 코드에 mock fallback을 넣지 않는다. Playwright 테스트에서만 이 문서의 응답을
intercept한다. UI 완료 기준은 backend 없이도 mock API로 run 생성 → 진행 → 완료 → 평가 차트
검수 흐름이 통과하는 것이다.

## 7. 통합 순서

1. `feat/m4-core`를 `main`에 먼저 병합한다.
2. core test와 실제 API smoke를 통과시킨다.
3. `feat/m4-ui`를 병합한다.
4. TypeScript 타입과 실제 OpenAPI/응답을 필드 단위로 대조한다.
5. `day20-allma-cleaning-kospi10`을 사용해 짧은 end-to-end run을 수행한다.
6. 학습 시작·SSE·중단·완료·평가 차트·체크포인트 왕복을 Playwright로 확인한다.
7. `uv run pytest -q`, `npm run lint`, `npm run build`, `git diff --check`를 실행한다.

충돌 해결은 계약 불일치와 `App.tsx` 통합에만 한정한다. 한 작업자의 구현을 다른 작업자의
branch에서 미리 복제하지 않는다.

## 8. 작업 규칙

- 두 작업자는 자신의 파일 소유권 밖 변경을 발견해도 되돌리지 않는다.
- 계약 변경이 필요하면 구현하지 말고 blocker와 필요한 변경을 보고한다.
- 새로운 프론트 차트 라이브러리를 추가하지 않고 lightweight-charts v5를 재사용한다.
- 브라우저는 Supabase에 직접 접근하지 않는다.
- 커밋·푸시·main 병합은 사용자가 별도로 지시할 때만 수행한다.
- 완료 보고에는 변경 파일, 검증 명령, 실제 확인 범위, 남은 위험을 포함한다.

## 9. 통합 결과 (2026-07-12)

- Core와 UI 변경을 `main` 작업 공간에서 계약 기준으로 통합했다.
- 실제 Supabase `ready` 데이터셋 `day20-allma-cleaning-kospi10`(id 20)으로
  `cnn1d_temporal_v1`, 1 epoch, batch 256 run을 MPS에서 완료했다.
- 웹에서 run 생성, SSE 상태·epoch 갱신, validation/test 지표, best checkpoint,
  validation 종목 207940의 예측 137건과 차트 마커를 확인했다.
- 실제 연동에서 발견한 비동기 데이터셋 선택 초기화와 일봉 예측 시간 직렬화 오류를 수정했고,
  최근 1,500봉 밖의 과거 split도 보이도록 예측 차트 조회 범위를 보정했다.
- 브라우저 콘솔 오류·경고와 실패 HTTP 응답은 0건이었고 가로 overflow가 없었다.
- 검증 run/job/checkpoint는 확인 후 삭제했다. Supabase에 smoke artifact를 남기지 않았다.
