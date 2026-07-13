# Pivot

**윌리엄스 프랙탈 기반 스윙 고점/저점 예측 워크벤치** — 캔들 수집, 전처리 검수, 데이터셋 생성, 모델 학습·평가, 실시간 추론을 하나의 로컬 웹 앱에서 다룬다.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)
![TypeScript](https://img.shields.io/badge/TypeScript-6-3178C6?logo=typescript&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-8-646CFF?logo=vite&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-Postgres%20%2B%20Storage-3FCF8E?logo=supabase&logoColor=white)
![Milestone](https://img.shields.io/badge/Milestone-M4%20완료-2EA44F)

## 개요

윌리엄스 프랙탈(Williams Fractal)은 중심 봉 앞뒤의 봉을 봐야 확정되는 **후행 지표**다.
Pivot은 이 지표로 과거 캔들에 고점/저점 라벨을 자동 생성하고, 라벨 시점까지의 시퀀스만
모델에 입력해 **"이 봉이 나중에 프랙탈 고점/저점으로 확정될 것인가"** 를 예측하는
분류 문제로 바꾼다.

로컬의 구 `../Fractal` 프로젝트 후속으로, 코드를 이식하지 않고 **파이프라인을 문서
명세로 정리한 뒤 알려진 결함을 고치며 재구현**한다. 원천 데이터는 HTS 수동 CSV 대신
[broker-modules](https://github.com/chosey0/broker-modules) 증권사 OpenAPI SDK(키움 일/분/틱봉)로 직접 조회한다.

현재 **M1–M4가 완료**되어 수집부터 학습·평가까지 동작한다. 다음 구현 순서는 이번에
확정한 프랙탈 샘플 페어링 계약 반영과 **M5 Kiwoom WebSocket 실시간 추론**이다.

### 핵심 개념


| 개념     | 내용                                                                                                   |
| ------ | ---------------------------------------------------------------------------------------------------- |
| 프랙탈 라벨 | 크기 `n`의 center rolling window에서 중심 봉이 창 내 최고가/최저가면 고점/저점. 확정에 미래 `(n-1)//2`봉 필요 — 미확정 구간은 절대 라벨하지 않음 |
| 라벨 규약  | `0` 저점 · `1` 고점 · `2` 무시. 기존 `MA20 < MA120` 무시 조건은 선택 옵션으로 유지                                      |
| 입력 윈도우 | **확정 변경(구현 예정):** 시간순 인접 마커 pair의 양 끝을 포함하는 가변 길이 구간. 같은 종류 pair는 `2`, 다른 종류 pair는 도착 마커 기준 `0`/`1` |
| 프리셋    | 신규 기본값은 `adjacent_markers_v1`. 필드가 없는 기존 저장 프리셋·데이터셋·run은 `latest_opposite_v1`으로 해석          |
| 파이프라인 | 단건 미리보기와 batch는 같은 `pivot/` 함수를 사용하고, 학습과 실시간 추론은 동일한 변환 코드를 재사용                                    |
| 타임프레임  | `day` / `min{N}` / `tick{N}` 1급 개념. 수집 이후 전 파이프라인은 봉 종류에 무관(agnostic)                                |

`cls2_drop`은 클래스 2 샘플만 제외하며, 마커 자체는 다음 인접 pair의 기준으로 남긴다.
예를 들어 `L1 → L2 → L3 → H`이면 `L1→L2 = 2`, `L2→L3 = 2`, `L3→H = 1`이다.


## 아키텍처

```text
pivot/          순수 도메인 패키지 (웹 비의존)
├─ ingestion/     broker-modules 조회 → 표준 DataFrame → parquet 캐시
├─ cleaning/      K-line 품질 분석과 구간별 정제
├─ labeling/      윌리엄스 프랙탈 마커·필터
├─ dataset/       preview/batch 공용 샘플, shard, loader, transform
├─ diagnostics/   원천·라벨·데이터셋 품질 진단
├─ models/        legacy/temporal CNN1D
├─ training/      학습·평가·체크포인트 처리
├─ storage/       Supabase Postgres/Storage 저장소 경계
└─ symbols/       KIS 국내 종목마스터 정규화·검색

server/         FastAPI 오케스트레이션, job/SSE, 별도 학습 프로세스
web/            Vite + React + TS — lightweight-charts v5 차트 워크벤치
data/           원천 캔들 · watchlist · 재생성 가능한 실행 캐시 (git 미추적)
Supabase        프리셋·job·dataset·run 메타데이터와 private shard/checkpoint
```

설계 원칙: **로직은 전부 `pivot/`에, 진입점은 얇게.** 학습과 실시간 추론이 동일한
스케일링/시퀀스 구성 코드를 재사용해 구 프로젝트의 학습-추론 전처리 불일치를 구조적으로 막는다.
상세는 [docs/05_package_layout.md](docs/05_package_layout.md).

## 화면 구성 (6탭)


| 탭        | 기능                                                                        | 상태    |
| -------- | ------------------------------------------------------------------------- | ----- |
| 종목 & 데이터 | 종목 검색(Supabase 자동완성)·타임프레임/기간 선택 수집·캐시 상태·실데이터 차트(MA/거래량, 보조지표 패널, 구간 로딩) | ✅     |
| 전처리 실험실  | 프랙탈 파라미터 튜닝 → 디바운스 재계산, 라벨 마커(▲▼●), 통계 diff, 스윙 윈도우 하이라이트, 학습 피처 선택       | ✅     |
| 데이터셋     | 프리셋 CRUD, batch job/SSE, shard, 샘플 검수, 삭제·정리                                | ✅ M3 |
| 데이터 진단   | 원천/preview/dataset 품질 게이트, K-line 분석                                          | ✅ M3 |
| 학습 & 평가  | run/SSE, CNN1D 학습, confusion matrix, 클래스 지표, 예측 마커                         | ✅ M4 |
| 실시간 추론   | Kiwoom `0B` 체결 → 봉 집계 → 봉 마감 추론                                             | 🔜 M5 |


### 마일스톤

- [x] **M0** 스캐폴딩 — FastAPI + Vite/React + 차트 컴포넌트
- [x] **M1** 데이터 수집 — 키움 일/분/틱 수집, parquet 캐시, 실데이터 차트 + 보조지표
- [x] **M2** 전처리 실험실 — 프랙탈 라벨링 재구현, preview API, 마커/통계/하이라이트
- [x] **M3-A** 프리셋 + batch dataset
- [x] **M3-B** 샘플 검수 + 데이터 진단 + lifecycle
- [x] **M4** 학습 & 평가
- [ ] **M5** Kiwoom WebSocket 실시간 추론

## 시작하기

### 요구사항

- Python **3.12+**, [uv](https://docs.astral.sh/uv/), Node.js **20.19+ 또는 22.12+**
- 키움증권 OpenAPI 앱 키 (캔들 수집, 향후 M5 실시간 체결)
- Supabase 프로젝트 (프리셋, dataset, 진단, run, artifact 저장)
- KIS 앱 키 (국내 종목마스터를 갱신할 때만 필요)

### 설치 & 실행

```bash
uv sync --extra server --extra train
npm --prefix web ci --include=optional --offline=false

# .env 작성 (git 미추적 — 키는 절대 커밋하지 않는다)
# KIWOOM_APP_KEY / KIWOOM_SECRET_KEY
# SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY  (서버 전용)
# KIS_APP_KEY / KIS_APP_SECRET_KEY          (종목마스터 갱신 시)

# macOS / Linux / Windows 공통 (API :8000 + 웹 :5173)
uv run --extra server --extra train python scripts/dev.py all

# 서버를 별도 터미널에서 실행할 때
uv run --extra server --extra train python scripts/dev.py api
uv run python scripts/dev.py web
```

`.env`는 서버 코드가 저장소 루트에서 자동으로 읽는다. 호스트나 포트를 바꾸려면
`--host`, `--api-port`, `--web-port`를 사용한다. 기존 macOS/Linux용
`scripts/run-api.sh`, `scripts/run-web.sh`도 같은 공통 런처를 호출한다.
운영체제를 바꾼 뒤에는 이전 OS의 `node_modules`를 재사용하지 말고
`npm --prefix web ci --include=optional --offline=false`로 현재 OS용 네이티브
패키지를 다시 설치한다.

브라우저에서 `http://localhost:5173` 접속 → 종목 추가 → 타임프레임 선택 → 수집 →
전처리 실험실에서 파라미터를 조작하며 라벨링 결과를 확인한다.

Supabase의 `supabase/migrations/`를 순서대로 적용한다. 국내 종목 검색을 쓰려면
종목마스터도 갱신한다:

```bash
scripts/update-domestic-master.sh
```

### 테스트

```bash
uv run --extra server --extra train python -m pytest tests -q
npm --prefix web run lint
npm --prefix web run build
```

## 저장 구조 (로컬 + Supabase)

```
data/
├─ raw/{broker}/{timeframe}/{symbol}.parquet   # 캔들 캐시 (day | min{N} | tick{N})
├─ meta/watchlist.json                         # 로컬 UI 운영 상태
└─ tmp/                                        # 재생성 가능한 다운로드/업로드 작업 캐시

Supabase Postgres                              # 프리셋·job·데이터셋 메타·진단·run·평가
Supabase Storage/pivot-datasets                # private parquet shard
Supabase Storage/pivot-models                  # private checkpoint·scaler·리포트
```

학습 관련 데이터의 원본은 Supabase다. 로컬 파일은 작업 중 임시 캐시로만 사용하며,
상세 스키마와 객체 경로는 [docs/06_supabase_training_storage.md](docs/06_supabase_training_storage.md)를 따른다.

## 문서

문서가 곧 명세다 — 구현과 달라지는 결정은 문서에 함께 반영한다.


| 문서                                                                           | 내용                                           |
| ---------------------------------------------------------------------------- | -------------------------------------------- |
| [docs/01_legacy_pipeline.md](docs/01_legacy_pipeline.md)                     | 구 Fractal 파이프라인 명세 (재구현 기준선)                 |
| [docs/02_improvement_backlog.md](docs/02_improvement_backlog.md)             | 결함 수정(A) · 방법 실험(B) · 엔지니어링(C) 백로그           |
| [docs/03_data_ingestion.md](docs/03_data_ingestion.md)                       | broker-modules 수집 설계 (타임프레임·캐시·스키마)          |
| [docs/04_webapp_design.md](docs/04_webapp_design.md)                         | 웹 워크벤치 설계 (6탭·프리셋·API·마일스톤)                  |
| [docs/05_package_layout.md](docs/05_package_layout.md)                       | 패키지 구조 — `pivot/` 라이브러리 + `server/` + `web/` |
| [docs/06_supabase_training_storage.md](docs/06_supabase_training_storage.md) | 학습 데이터 Supabase 스키마·Storage·수명주기·보안 계약       |
| [docs/07_m4_implementation_plan.md](docs/07_m4_implementation_plan.md)       | M4 학습·평가 구현 계약                                  |
| [docs/08_m5_implementation_plan.md](docs/08_m5_implementation_plan.md)       | M5 Kiwoom WebSocket 실시간 추론 계획                    |


## 관련 프로젝트

- [broker-modules](https://github.com/chosey0/broker-modules) — 증권사 OpenAPI 비동기 SDK (키움/KIS/토스/KRX)
- `../Fractal` — 선행 프로젝트 (PyQt5 + finplot, 본 프로젝트의 재구현 대상)
