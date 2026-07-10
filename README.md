# Pivot

**윌리엄스 프랙탈 기반 스윙 고점/저점 예측 워크벤치** — 캔들 수집부터 라벨링 검증, 데이터셋 생성, 모델 학습, 실시간 추론까지 하나의 로컬 웹 앱에서 수행한다.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5%2B-3178C6?logo=typescript&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-8-646CFF?logo=vite&logoColor=white)
![lightweight-charts](https://img.shields.io/badge/lightweight--charts-v5-2962FF)
![uv](https://img.shields.io/badge/uv-package%20manager-DE5FE9)
![Supabase](https://img.shields.io/badge/Supabase-symbol%20master-3FCF8E?logo=supabase&logoColor=white)
![Tests](https://img.shields.io/badge/pytest-passing-brightgreen?logo=pytest&logoColor=white)
![Milestone](https://img.shields.io/badge/milestone-M2%20done%20%C2%B7%20M3%20next-blue)

## 개요

윌리엄스 프랙탈(Williams Fractal)은 중심 봉 앞뒤의 봉을 봐야 확정되는 **후행 지표**다.
Pivot은 이 지표로 과거 캔들에 고점/저점 라벨을 자동 생성하고, 라벨 시점까지의 시퀀스만
모델에 입력해 **"이 봉이 나중에 프랙탈 고점/저점으로 확정될 것인가"** 를 예측하는
분류 문제로 바꾼다.

구 [`Fractal`](../Fractal) 프로젝트의 후속으로, 코드를 이식하지 않고 **파이프라인을 문서
명세로 정리한 뒤 알려진 결함을 고치며 재구현**한다. 원천 데이터는 HTS 수동 CSV 대신
[broker-modules](https://github.com/chosey0/broker-modules) 증권사 OpenAPI SDK(키움 일/분/틱봉)로 직접 조회한다.

### 핵심 개념

| 개념 | 내용 |
|---|---|
| 프랙탈 라벨 | 크기 `n`의 center rolling window에서 중심 봉이 창 내 최고가/최저가면 고점/저점. 확정에 미래 `(n-1)//2`봉 필요 — 미확정 구간은 절대 라벨하지 않음 |
| 라벨 규약 | `0` 저점 · `1` 고점 · `2` 무시 (라벨 봉에서 MA20 < MA120 역배열) |
| 입력 윈도우 | **직전 반대 종류 마커 ~ 현재 마커의 스윙 구간** (가변 길이). 고점 샘플은 직전 저점부터, 저점 샘플은 직전 고점부터 |
| 프리셋 | 타임프레임·프랙탈 n·피처·필터를 이름 붙여 저장. 단건 미리보기와 일괄 처리가 **같은 프리셋, 같은 `pivot/` 함수**를 사용 → 결과 불일치 원천 차단 |
| 타임프레임 | `day` / `min{N}` / `tick{N}` 1급 개념. 수집 이후 전 파이프라인은 봉 종류에 무관(agnostic) |

## 아키텍처

```
pivot/          순수 도메인 패키지 (웹 비의존)
├─ ingestion/     broker-modules 조회 → 표준 DataFrame → parquet 캐시 (증분/기간 수집, 구간 로딩)
├─ labeling/      윌리엄스 프랙탈 라벨링 (라벨 모드 · 정배열/거래대금 필터)
├─ dataset/       스윙 윈도우 샘플 생성 — preview와 batch가 공유하는 단일 진입점
└─ symbols/       KIS 종목마스터 → Supabase 적재 + pg_trgm 유사도 검색

server/         FastAPI — pivot/ 호출·직렬화·job 관리만 (도메인 로직 없음)
web/            Vite + React + TS — lightweight-charts v5 차트 워크벤치
data/           parquet 캐시 · watchlist · 프리셋 · 데이터셋 (git 미추적, 파일 기반)
```

설계 원칙: **로직은 전부 `pivot/`에, 진입점은 얇게.** 학습과 실시간 추론이 동일한
스케일링/시퀀스 구성 코드를 재사용해 구 프로젝트의 학습-추론 전처리 불일치를 구조적으로 막는다.
상세는 [docs/05_package_layout.md](docs/05_package_layout.md).

## 화면 구성 (6탭)

| 탭 | 기능 | 상태 |
|---|---|---|
| 종목 & 데이터 | 종목 검색(Supabase 자동완성)·타임프레임/기간 선택 수집·캐시 상태·실데이터 차트(MA/거래량, 보조지표 패널, 구간 로딩) | ✅ |
| 전처리 실험실 | 프랙탈 파라미터 튜닝 → 디바운스 재계산, 라벨 마커(▲▼●), 통계 diff, 스윙 윈도우 하이라이트, 학습 피처 선택 | ✅ |
| 데이터셋 | 프리셋 일괄 적용(job + SSE), 샘플 브라우저 검수 | 🔜 M3 |
| 데이터 진단 | 원천/라벨/데이터셋 품질 게이트 (읽기 전용) | 🔜 M3 |
| 학습 & 평가 | run 관리, 학습 곡선 SSE, confusion matrix, 차트 예측 검증 | 🔜 M4 |
| 실시간 추론 | 증권사 WS 중계 → 봉 집계 → 봉 마감 판정 | 🔜 M5 |

### 마일스톤

- [x] **M0** 스캐폴딩 — FastAPI + Vite/React + 차트 컴포넌트
- [x] **M1** 데이터 수집 — 키움 일/분/틱 수집, parquet 캐시, 실데이터 차트 + 보조지표
- [x] **M2** 전처리 실험실 — 프랙탈 라벨링 재구현, preview API, 마커/통계/하이라이트
- [ ] **M3** 프리셋 + 일괄 처리 + 데이터 진단
- [ ] **M4** 학습 & 평가
- [ ] **M5** 실시간 추론

## 시작하기

### 요구사항

- Python **3.12+**, [uv](https://docs.astral.sh/uv/), Node.js 20+
- 키움증권 OpenAPI 앱 키 (캔들 수집), Supabase 프로젝트 (종목 검색 — 선택)

### 설치 & 실행

```bash
uv sync --extra server
npm --prefix web install

# .env 작성 (git 미추적 — 키는 절대 커밋하지 않는다)
# KIWOOM_APP_KEY / KIWOOM_SECRET_KEY
# KIS_APP_KEY / KIS_APP_SECRET_KEY          (실시간·종목마스터용)
# SUPABASE_URL / SUPABASE_SECRET_KEY 등     (종목 검색용, 선택)

scripts/run-api.sh   # 백엔드 :8000 (.env 자동 로드)
scripts/run-web.sh   # 프론트 :5173 (/api → 8000 프록시)
```

브라우저에서 `http://localhost:5173` 접속 → 종목 추가 → 타임프레임 선택 → 수집 →
전처리 실험실에서 파라미터를 조작하며 라벨링 결과를 확인한다.

국내 종목 검색을 쓰려면 Supabase에 `supabase/migrations/20260710_domestic_master.sql`을
적용한 뒤 종목마스터를 갱신한다:

```bash
scripts/update-domestic-master.sh
```

### 테스트

```bash
uv run pytest                 # 프랙탈 정렬·lag 경계·라벨 규약·스윙 윈도우·심볼 필터
npm --prefix web run lint     # oxlint
npm --prefix web run build    # tsc + vite build
```

## 저장 구조 (파일 기반, DB 없음)

```
data/
├─ raw/{broker}/{timeframe}/{symbol}.parquet   # 캔들 캐시 (day | min{N} | tick{N})
├─ meta/watchlist.json · meta/presets/*.json
└─ datasets/{name}/samples.parquet + meta.json # 프리셋 스냅샷 포함 → 재현 가능
models/runs/{run_id}/                          # config · history · metrics · checkpoints
```

## 문서

문서가 곧 명세다 — 구현과 달라지는 결정은 문서에 함께 반영한다.

| 문서 | 내용 |
|---|---|
| [docs/01_legacy_pipeline.md](docs/01_legacy_pipeline.md) | 구 Fractal 파이프라인 명세 (재구현 기준선) |
| [docs/02_improvement_backlog.md](docs/02_improvement_backlog.md) | 결함 수정(A) · 방법 실험(B) · 엔지니어링(C) 백로그 |
| [docs/03_data_ingestion.md](docs/03_data_ingestion.md) | broker-modules 수집 설계 (타임프레임·캐시·스키마) |
| [docs/04_webapp_design.md](docs/04_webapp_design.md) | 웹 워크벤치 설계 (6탭·프리셋·API·마일스톤) |
| [docs/05_package_layout.md](docs/05_package_layout.md) | 패키지 구조 — `pivot/` 라이브러리 + `server/` + `web/` |

## 관련 프로젝트

- [broker-modules](https://github.com/chosey0/broker-modules) — 증권사 OpenAPI 비동기 SDK (키움/KIS/토스/KRX)
- `../Fractal` — 선행 프로젝트 (PyQt5 + finplot, 본 프로젝트의 재구현 대상)
