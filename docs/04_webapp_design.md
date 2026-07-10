# 웹 애플리케이션 설계 — 워크벤치

모든 작업(데이터 수집 → 전처리 검증 → 데이터 품질 진단 → 일괄 데이터셋 생성 → 모델 학습/평가 → 실시간 추론)을
웹 UI 위에서 수행한다. 핵심 사용 흐름:

> 관심종목 등록 → 캔들 수집 → **차트에서 프랙탈 라벨링 결과를 눈으로 확인하며 파라미터 튜닝**
> → 파라미터를 프리셋으로 저장 → 몇 종목으로 스팟체크 → **데이터 진단으로 품질 확인**
> → **프리셋을 전체 종목에 일괄 적용**해 학습 데이터셋 생성
> → 데이터셋으로 **모델 학습·평가** → 체크포인트를 골라 **실시간 추론**으로 검증

## 0. 확정된 기술 결정

| 항목 | 결정 |
|---|---|
| 백엔드 | FastAPI (Python 3.12+, uv) — broker-modules가 async/httpx 기반이라 자연스럽게 맞음 |
| 프론트엔드 | React + TypeScript + Vite |
| 차트 | TradingView **lightweight-charts v5** (`addSeries` + `createSeriesMarkers`) |
| 파라미터 변경 반영 | 자동 재계산 (300~500ms 디바운스) |
| 일괄 처리 대상 | 웹에서 관리하는 관심종목 리스트 (보조: 거래대금 상위 N 일괄 추가) |
| 전처리 로직 위치 | `pivot/` 순수 파이썬 패키지 — 웹과 무관하게 동작, FastAPI는 이를 호출만 |

## 1. 화면 구성 (6개 탭)

| 탭 | 역할 | 구 프로젝트 대응 |
|---|---|---|
| 종목 & 데이터 | 관심종목/수집/캐시 관리 | HTS 수동 다운로드 |
| 전처리 실험실 | 파라미터 튜닝 + 라벨링 검증 | `data_main.py` + finplot 확인 코드 |
| 일괄 처리 & 데이터셋 | 프리셋 일괄 적용, 데이터셋 검수 | `data_main.py` |
| 데이터 진단 | 원천 캐시/라벨/데이터셋 품질 점검 | 없음 (신규 품질 게이트) |
| 학습 & 평가 | 학습 실행/모니터링, run 비교, 차트 검증 | `train_main.py`, `test_main*.py` |
| 실시간 추론 | 실시간 체결 수신 + 모델 판정 | `main.py` (PyQt5 앱) |

### 1.1 종목 & 데이터 (Watchlist)

- 종목 검색(심볼 마스터) → 관심종목 추가/제거
  - 국내 종목마스터는 Supabase `public.domestic_master`에 저장한다.
  - 원천은 `broker-modules`의 `brokers.kis.symbols` KOSPI/KOSDAQ master이며, 우선 범위는 보통주다.
  - 검색은 Postgres `pg_trgm` 기반 RPC `search_domestic_master(query, match_limit)`로 수행한다.
  - 종목코드/종목명 입력 중 가장 유사한 후보를 드롭다운으로 표시하고, 방향키/Enter로 선택한다.
- **타임프레임 선택 수집**: 일봉 / N분봉 / N틱봉 중 선택해 수집
  - 타입(일/분/틱) + 단위 드롭다운 (분: 1,3,5,10,15,30,45,60 / 틱: 1,3,5,10,30 — 기본값 1)
  - 같은 종목이라도 타임프레임별로 캐시가 분리됨 (`data/raw/{broker}/{timeframe}/{symbol}.parquet`)
- 관심종목 테이블: 종목명/코드, **타임프레임별 캐시 상태**(수집 기간, 봉 수, 마지막 갱신), 수집/갱신 버튼
- 수집 기간 선택: 시작일/종료일을 직접 지정할 수 있다. 미지정 시 기존 캐시 기준 증분 수집,
  지정 시 해당 기간을 조회해 기존 parquet 캐시에 병합
- 실데이터 차트 보조지표 선택: lightweight-charts의 `LineSeries`/`HistogramSeries`를 사용해
  이동평균선(기본 5/20/60/120, 기간 추가/삭제 가능)과 거래량을 추가/제거한다.
  각 보조지표는 **차트 표시 여부**와
  **전처리 학습 피처 포함 여부**를 독립적으로 선택한다.
  - `+ 보조지표` 버튼은 차트 위 설정 패널을 연다. 패널 안에서 색상, 선 굵기, 기간,
    차트 표시, 학습 포함을 편집하고 `적용`/`취소`/`초기화`로 반영 여부를 통제한다.
  - 차트 좌상단 범례는 현재 캔들의 OHLC와 표시 중인 이동평균 기간을 실제 선 색상으로 보여준다.
    범례의 빠른 조작은 삭제만 제공하고, 표시/숨김은 `+ 보조지표` 설정 패널에서 일원화한다.
  - 보조지표 프리셋은 이름을 붙여 저장한다. M1에서는 브라우저 로컬 저장소에 보관하고,
    M3 프리셋 CRUD가 생기면 전처리 프리셋의 일부로 서버 저장한다.
  - 설정 패널은 전처리 `features` 미리보기와 입력 차원, 중복 기간, 차트 전용/학습 전용,
    MA 초기 NaN 구간 경고를 표시한다.
- 전체 갱신 버튼 (rate limit 고려해 순차 실행, 진행률 표시)
- 보조 기능: "거래대금 상위 N 종목 일괄 추가"

### 1.2 전처리 실험실 (Lab) — 핵심 화면

```
┌────────────┬──────────────────────────────────────┬───────────────┐
│ 관심종목    │  캔들차트 (lightweight-charts)         │ 파라미터 패널   │
│ 리스트      │   - 캔들 + MA 라인(5/20/120)          │  fractal n     │
│ (클릭 시    │   - 프랙탈 마커:                       │  피처 선택      │
│  차트 로드) │     ▲ 저점(0)  ▼ 고점(1)  ● 무시(2)   │  필터 토글:     │
│            │   - 마커 클릭 → 해당 샘플의            │   정배열/거래대금│
│            │     입력 윈도우(스윙 구간) 하이라이트    │  라벨 모드      │
│            │                                      │               │
│            ├──────────────────────────────────────┤ ───────────── │
│            │  통계 바: 샘플 수, 클래스 분포(0/1/2),  │ 프리셋 저장/    │
│            │  이전 파라미터 대비 증감(Δ)             │ 불러오기       │
└────────────┴──────────────────────────────────────┴───────────────┘
```

- 파라미터 패널 최상단에서 **타임프레임 선택** (일봉 / N분봉 / N틱봉, 프리셋에 포함).
  변경 시 해당 타임프레임 캐시로 차트를 다시 로드 (미수집이면 수집 유도)
- 파라미터를 바꾸면 디바운스 후 `/preprocess/preview` 호출 → 마커/통계 즉시 갱신
- **파라미터 변화에 따른 데이터 변화 확인**: 직전 계산 결과와의 diff를 통계 바에 표시
  (예: "샘플 142 → 118 (−24), 저점 51 → 40")
- 마커 클릭 시 그 샘플이 모델에 들어가는 실제 입력 구간을 차트 위에 하이라이트
  → "하나하나 직접 확인" 요구사항 충족
- **입력 윈도우 = 직전 반대 종류 마커 ~ 현재 마커의 스윙 구간** (가변 길이):
  고점 샘플은 직전 저점 마커부터 해당 고점까지, 저점 샘플은 직전 고점 마커부터
  해당 저점까지. 직전 반대 마커가 없는 첫 지점은 샘플에서 제외한다.
  (구 방식의 `max_len` 고정 길이 윈도우는 폐기)
- 샘플 상세 패널(선택): 해당 시퀀스의 스케일링 후 값 미리보기

### 1.3 일괄 처리 & 데이터셋 (Datasets)

- 프리셋 선택 + 대상(관심종목 전체 또는 선택) → **일괄 전처리 실행**
- 진행 상황: SSE로 종목별 진행률/성공/실패/샘플 수 스트리밍
- 완료 시 데이터셋 카드 생성: 이름, 생성일, 사용 프리셋(파라미터 스냅샷), 샘플 수, 클래스 분포
- 데이터셋 상세 = **샘플 브라우저**: 샘플을 페이지 단위로 넘기며 미니 차트로 개별 검수
  (라벨별 필터, 무작위 샘플링 보기)

### 1.4 데이터 진단 (Diagnostics)

원천 캔들, 전처리 결과, 데이터셋이 학습 가능한 품질인지 확인하는 독립 품질 게이트.
Lab은 개별 종목을 눈으로 확인하는 화면이고, Diagnostics는 전체 관심종목/데이터셋의 이상 징후를
한 번에 찾는 화면이다.

```
┌───────────────────────────────┬──────────────────────────────────┐
│ 진단 대상                       │  진단 결과 요약                     │
│  - raw cache: 종목/타임프레임    │   통과/경고/실패 카운트              │
│  - preset preview: 프리셋+종목   │   주요 경고: 누락, 중복, NaN, 불균형  │
│  - dataset: 생성된 데이터셋      │   [진단 실행] [리포트 저장]          │
├───────────────────────────────┴──────────────────────────────────┤
│ 상세 테이블                                                        │
│  종목, 타임프레임, 기간, 봉 수, 중복 timestamp, 시간 역전,           │
│  OHLC 불변식 위반, 거래량/거래대금 이상값, MA NaN 비율,              │
│  라벨 0/1/2 분포, split 배정, 경고 메시지                           │
└───────────────────────────────────────────────────────────────────┘
```

- 원천 캐시 진단:
  - timestamp 고유성/오름차순, 누락 구간, 중복 봉, OHLC 불변식(`Low <= Open/Close <= High`) 위반 확인
  - 거래량/거래대금 0 또는 급격한 이상값, `Amount` 근사 사용 여부 표시
  - MA 계산 가능 구간과 `5/20/120` NaN 비율 표시
- 라벨/프리셋 진단:
  - 선택한 프리셋으로 종목별 라벨 수와 클래스 `0/1/2` 비율 산출
  - 특정 클래스가 거의 없거나 `2`가 과도하게 많은 종목을 경고
  - `fractal.n`, `ma_source`, 필터 설정이 샘플 수에 미치는 영향 요약
- 데이터셋 진단:
  - 데이터셋 생성 후 샘플 수, 클래스 분포, 종목별 기여도, 길이 분포 확인
  - train/val/test split이 종목 단위로 누수 없이 나뉘었는지 검사
  - 학습 전 체크리스트 형태로 “진행 가능/주의/중단 권장” 상태 표시
- 리포트는 재현성을 위해 json으로 저장할 수 있지만, 원천 데이터 수정은 하지 않는다.
  수정이 필요하면 Watchlist 수집 갱신 또는 Lab/프리셋 조정으로 되돌아간다.

### 1.5 학습 & 평가 (Training)

구 `train_main.py`(학습) + `test_main*.py`(finplot 육안 평가)의 웹 버전.

```
┌───────────────────────────────┬──────────────────────────────────┐
│ Run 목록 (이력)                │  새 Run 설정                       │
│  - run_id, 데이터셋, 상태       │   데이터셋 선택 (프리셋 스냅샷 표시)  │
│  - best acc/F1, 생성일         │   모델: CNN1D | (추후 확장)         │
│  - 클릭 → 상세                 │   epochs, batch, lr, sampler,      │
│                               │   train/val 분리 방식(종목/기간)     │
│                               │   [학습 시작]                       │
├───────────────────────────────┴──────────────────────────────────┤
│ Run 상세                                                          │
│  ① 학습 곡선: epoch별 train/val loss·acc 실시간 갱신 (SSE)          │
│  ② 평가: confusion matrix, 클래스별 precision/recall/F1 (val 기준)  │
│  ③ 차트 검증: 종목 선택 → 캔들차트에 실제 프랙탈 마커 vs 모델 예측     │
│     마커를 겹쳐 표시 (맞춤/틀림 색 구분) ← 구 test_main.py 대체       │
│  ④ 체크포인트 목록: epoch별 .pt, "실시간 추론에 사용" 지정            │
└───────────────────────────────────────────────────────────────────┘
```

- **Run** = 데이터셋 + 하이퍼파라미터 + 결과(곡선/지표/체크포인트)의 묶음.
  메타를 json으로 저장해 run 간 비교 가능 (구 `models/saved/{name}.json` 방식 계승)
- 학습은 장기 job: **별도 프로세스**로 실행 (torch 학습이 FastAPI 이벤트 루프를 막지 않도록),
  진행 상황은 파일/큐 경유로 SSE 스트리밍, 중단 버튼 제공
- 평가 지표는 백로그 A5(train/val 분리)·A6(클래스별 지표) 반영이 전제
- ③ 차트 검증은 Lab과 같은 차트 컴포넌트 재사용: 실제 라벨(▲▼)과 예측(테두리/색 변형)을
  한 차트에 겹치고, 오분류만 필터해 보는 토글 제공

### 1.6 실시간 추론 (Live)

구 `main.py`(PyQt5 + KIS 웹소켓 + `infer_plot*.py`)의 웹 버전.

```
┌────────────────────┬──────────────────────────────────────────────┐
│ 구독 테이블          │  실시간 캔들차트 (선택 종목)                     │
│  종목 검색/추가       │   - 과거 봉 로드 후 체결가로 현재 봉 실시간 갱신   │
│  종목별:             │   - 모델 판정 마커: 저점/고점 확률 표시            │
│   현재가, 등락률      │  하단: 최근 판정 로그 (시각, 종목, 클래스, 확률)   │
│   최근 판정 결과      │                                              │
│  사용 모델(체크포인트) │                                              │
└────────────────────┴──────────────────────────────────────────────┘
```

- 데이터 경로: **증권사 WebSocket ↔ FastAPI(중계·집계·추론) ↔ 브라우저 WebSocket**
  - 증권사 연결·인증키는 서버에만 존재, 브라우저는 서버 WS만 구독
  - 서버가 체결 틱을 봉(현재 봉 갱신 + 봉 마감)으로 집계 → 봉 마감 시점마다 시퀀스 구성 → 추론
  - 브라우저는 `series.update()`로 현재 봉 갱신, 판정 발생 시 마커 추가
- 추론 입력은 전처리와 **동일한 `pivot` 패키지의 스케일링/시퀀스 구성 함수** 사용
  (학습-추론 전처리 불일치 방지 — 구 프로젝트에서 `infer_plot copy*.py`가 난립했던 원인 제거)
- 체크포인트 선택: Training 탭에서 "실시간 추론에 사용"으로 지정한 모델 기본 로드
- 장 마감/미개장 시간대엔 마지막 캐시 차트 + "장 종료" 상태 표시

## 2. 핵심 개념: 전처리 프리셋

Lab에서 튜닝한 파라미터 집합을 이름 붙여 저장. Lab(단건)과 일괄 처리(전체)가 **같은 프리셋을
공유**하므로 "몇 종목으로 확인한 그대로 전체에 적용"이 보장된다.

```jsonc
// data/meta/presets/day20_ma20120_cls3.json
{
  "name": "day20_ma20120_cls3",
  "timeframe": { "type": "day", "unit": 1 },       // day | minute | tick, 분/틱은 N 단위 (기본 1)
  "fractal": { "n": 20 },                          // center rolling window 크기
  "ma_windows": [5, 20, 60, 120],                  // 직접 계산할 이평선
  "ma_source": "self",                             // self(해당 타임프레임 rolling) | daily(일봉 이평 병합, 구 방식)
  "chart_indicators": {
    "preset": "기본 MA 5/20/60/120",
    "moving_averages": [
      { "window": 5, "color": "#009c62", "line_width": 1, "chart": true, "feature": false },
      { "window": 20, "color": "#e31b35", "line_width": 1, "chart": true, "feature": true },
      { "window": 60, "color": "#ff8a00", "line_width": 1, "chart": true, "feature": false },
      { "window": 120, "color": "#8a26b2", "line_width": 1, "chart": true, "feature": true }
    ],
    "volume": { "chart": true, "feature": false }
  },
  "features": ["Open", "High", "Low", "Close", "20", "120"],
  // 차트에 표시한 보조지표라도 features에서 제외하면 학습 데이터에는 들어가지 않는다.
  // 반대로 차트에서 숨겨도 features에 포함하면 전처리/학습 피처로 사용한다.
  // 입력 윈도우는 직전 반대 마커 ~ 현재 마커의 스윙 구간 (파라미터 없음, §1.2 참고)
  "labeling": {                                    // 백로그 B2 실험을 위해 모드화
    "mode": "cls3",                                // cls3 | cls2_drop | cls4 | with_negative
    "ignore_rule": "ma20 < ma120"
  },
  "filters": {
    "ma_alignment": null,                          // null | "20>120" | "5>20>120"
    "min_amount": null                             // 원 단위, null이면 미적용
  },
  "created_at": "...", "updated_at": "..."
}
```

데이터셋 메타에는 사용한 프리셋의 **스냅샷**을 통째로 저장한다 (프리셋을 나중에 수정해도
데이터셋 재현 가능 — 구 프로젝트의 `{dataset}.json` 메타 방식 계승).

## 3. API 설계

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/symbols/search?q=` | Supabase `public.domestic_master` 기반 종목명/종목코드 퍼지 검색 |
| POST | `/api/symbols/sync` | KIS KOSPI/KOSDAQ 보통주 종목마스터를 Supabase에 업서트 |
| GET/POST/DELETE | `/api/watchlist` | 관심종목 CRUD |
| POST | `/api/watchlist/bulk-top?n=` | 거래대금 상위 N 일괄 추가 |
| POST | `/api/ingest` | `{symbols[], timeframe, start?, end?}` 수집/갱신 (timeframe = `day`/`min{N}`/`tick{N}`, 날짜 범위는 선택) |
| GET | `/api/ingest/status` | 종목 × 타임프레임별 캐시 상태 |
| GET | `/api/chart/{symbol}?timeframe=&ma=` | 캔들 + 요청한 MA 기간 + 거래량 (lightweight-charts 데이터 형식으로 반환, `ma=5,20,60,120`) |
| POST | `/api/preprocess/preview` | `{symbol, params}` → 캔들/MA/거래량 + 프랙탈 마커 + 샘플 목록(윈도우 범위/라벨) + 클래스 통계 (Lab이 한 번에 차트를 그리도록 chart 응답 포맷 포함) |
| GET/POST/PUT/DELETE | `/api/presets` | 프리셋 CRUD |
| POST | `/api/preprocess/batch` | `{preset, symbols[]}` → job_id (BackgroundTask) |
| GET | `/api/jobs/{id}/events` | SSE: 진행률/종목별 결과 |
| GET | `/api/datasets` | 데이터셋 목록 + 메타 |
| GET | `/api/datasets/{name}/samples?label=&offset=&limit=` | 샘플 브라우저용 페이지 조회 |
| GET | `/api/datasets/{name}/samples/{i}` | 샘플 1건 상세 (시퀀스 원본/스케일링 값) |
| POST | `/api/diagnostics/cache` | `{symbols[], timeframe}` → 원천 캐시 품질 리포트 |
| POST | `/api/diagnostics/preview` | `{preset, symbols[]}` → 프리셋 적용 전 라벨/샘플 분포 리포트 |
| POST | `/api/diagnostics/datasets/{name}` | 데이터셋 품질·split 누수·클래스 분포 리포트 |
| GET/POST | `/api/runs` | 학습 run 목록 / 새 학습 시작 (`{dataset, hyperparams}` → job_id) |
| GET | `/api/runs/{id}` | run 상세 (설정, 곡선, 평가 지표, 체크포인트 목록) |
| GET | `/api/runs/{id}/events` | SSE: epoch별 loss/acc 실시간 스트리밍 |
| POST | `/api/runs/{id}/stop` | 학습 중단 |
| POST | `/api/runs/{id}/evaluate` | `{symbol}` → 차트 검증용 예측 결과 (실제 라벨 vs 예측) |
| PUT | `/api/live/model` | 실시간 추론에 사용할 체크포인트 지정 |
| GET/POST/DELETE | `/api/live/subscriptions` | 실시간 구독 종목 관리 |
| WS | `/ws/live` | 브라우저용 WebSocket: 현재 봉 갱신 + 모델 판정 이벤트 푸시 |

설계 원칙:

- `preview`와 `batch`는 **동일한 `pivot` 패키지 함수**를 호출 (단건/일괄이 같은 코드 경로 → 결과 불일치 방지)
- 수집(ingest)과 전처리(preprocess)는 완전 분리 — 전처리는 항상 로컬 캐시(parquet)만 읽음
- 장기 작업(수집, 일괄 전처리)은 job + SSE 패턴 통일

## 4. 저장 구조 (파일 기반, DB 없음)

```
data/
├─ raw/{broker}/{timeframe}/{symbol}.parquet    # 캔들 캐시, timeframe = day | min{N} | tick{N}
├─ meta/
│  ├─ watchlist.json
│  └─ presets/{name}.json
├─ diagnostics/
│  └─ {report_id}.json                          # 선택 저장한 데이터 품질 리포트
└─ datasets/{name}/
   ├─ samples.parquet                           # 시퀀스 샘플 (가변 길이 → list 컬럼)
   └─ meta.json                                 # 프리셋 스냅샷 + 통계

models/
└─ runs/{run_id}/
   ├─ config.json                               # 데이터셋명 + 프리셋 스냅샷 + 하이퍼파라미터
   ├─ history.json                              # epoch별 loss/acc/지표
   ├─ metrics.json                              # 최종 평가 (confusion matrix, 클래스별 P/R/F1)
   └─ checkpoints/epoch_{n}.pt
```

단일 사용자 로컬 도구이므로 DB 없이 파일로 시작. (필요해지면 SQLite로 승격)

## 5. lightweight-charts v5 연동 세부

```ts
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts';

const chart = createChart(container, options);
const candles = chart.addSeries(CandlestickSeries);           // v5: addSeries(타입, 옵션)
const ma20 = chart.addSeries(LineSeries, { color: '#5A639C' });
const ma120 = chart.addSeries(LineSeries, { color: '#A0937D' });

// 프랙탈/라벨 마커 — v5는 별도 프리미티브
const markers = createSeriesMarkers(candles, [
  { time: '2026-07-01', position: 'belowBar', shape: 'arrowUp',   color: '#26a69a', text: 'L' },  // 저점(0)
  { time: '2026-07-04', position: 'aboveBar', shape: 'arrowDown', color: '#ef5350', text: 'H' },  // 고점(1)
  { time: '2026-07-08', position: 'aboveBar', shape: 'circle',    color: '#9e9e9e' },             // 무시(2)
]);
markers.setMarkers(next);   // 파라미터 변경 시 갱신
```

- 시간값: 일봉은 `'yyyy-mm-dd'` 문자열, 분봉/틱봉은 unix timestamp(초) — 백엔드가 이 형식으로 내려줌.
  lightweight-charts는 시간값이 **고유하고 오름차순**이어야 하므로, 같은 초에 여러 봉이 생길 수
  있는 틱봉은 백엔드에서 시간 충돌을 해소해 내려준다 (열린 결정 참고)
- 라벨 표시 규약: 저점 `arrowUp/belowBar/초록`, 고점 `arrowDown/aboveBar/빨강`, 무시 `circle/회색`
- 샘플 입력 윈도우 하이라이트: 마커 클릭(`subscribeClick`) → 해당 구간을 반투명 배경으로 표시
  (v5 plugin primitive로 구현, 1차 구현은 간단히 해당 구간 라인/영역 시리즈 오버레이로 대체 가능)
- 미니 차트(샘플 브라우저): 옵션 축소판 차트 재사용 (스케일/그리드 최소화)

## 6. 리포지토리 구조

상세 구조와 설계 원칙은 **[05_package_layout.md](05_package_layout.md)**가 기준이다. 요약:

- `pivot/` — 순수 도메인 패키지 (ingestion → labeling → dataset → models → training → realtime).
  웹 비의존, 파이프라인 로직은 전부 여기에
- `server/` — FastAPI. `pivot/` 함수 호출·job 관리·직렬화만 (routers, jobs.py, live.py)
- `web/` — Vite + React + TS (api/, components/chart/, pages/{Watchlist,Lab,Datasets,Diagnostics,Training,Live})
- 개발: `uvicorn server.main:app --reload` + `vite dev` (proxy `/api` → 8000)
- 배포(로컬 사용): `vite build` 산출물을 FastAPI `StaticFiles`로 서빙 → 프로세스 하나로 실행

## 7. 구현 마일스톤

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| **M0** | 스캐폴딩: uv 프로젝트 + FastAPI + Vite/React + 차트 컴포넌트 | 더미 데이터 캔들차트가 브라우저에 뜬다 |
| **M1** | 수집: broker-modules 연동(Kiwoom 일/분/틱), 기간 선택 수집, watchlist, parquet 캐시, 차트+보조지표(MA/거래량) 표시/제거, MA 기간 추가/삭제 | 관심종목 추가 → 타임프레임/기간 선택 → 수집 → 실데이터 차트와 선택 보조지표 확인 |
| **M2** | 전처리 실험실: `pivot.preprocess` 재구현(백로그 A 반영), preview API, 파라미터 패널 + 마커 + 통계 diff, 원천/라벨 진단 기반 | 파라미터 조작 시 마커/통계가 실시간 갱신, 샘플 윈도우 하이라이트, 기본 데이터 경고 표시 |
| **M3** | 프리셋 + 일괄 처리 + 데이터 진단: preset CRUD, batch job + SSE, 데이터셋 저장 + 샘플 브라우저 + Diagnostics 탭 | 프리셋으로 전체 종목 일괄 전처리 → 데이터셋 생성/검수 → 품질 리포트 확인 |
| **M4** | 학습 & 평가: `pivot.train` (백로그 A5/A6/B1 베이스라인), run 관리 + 학습 곡선 SSE, 차트 검증 | 웹에서 학습 시작 → 곡선/지표 확인 → 차트에서 예측 vs 실제 비교 |
| **M5** | 실시간 추론: 증권사 WS 중계, 봉 집계 + 실시간 추론, Live 화면 | 장중에 구독 종목의 실시간 판정이 차트에 표시 |

M2에서 재구현하는 전처리는 [01_legacy_pipeline.md](01_legacy_pipeline.md)를 명세로 하되
[02_improvement_backlog.md](02_improvement_backlog.md)의 A그룹(정밀도, Time 저장, 검증 분리 등)을 반영한다.

## 8. 열린 결정 (구현하며 확정)

- 샘플 저장 시 스케일링 적용 시점 (백로그 A4): 데이터셋 생성 시 원본 저장 + 스케일링은 로더에서 vs 스케일링 완료본 저장 — 샘플 브라우저에서 원본 값도 보여줘야 하므로 **원본 저장** 우선
- 거래대금 상위 N 조회 소스: broker-modules 지원 여부 확인 필요 (미지원 시 KRX 모듈 또는 수집된 캐시 기준 계산)
- 타임프레임 구현 순서: 수집/전처리/학습 로직은 처음부터 타임프레임 무관(agnostic)하게 만들되,
  검증은 일봉 → 분봉 → 틱봉 순으로 진행 (분/틱은 수집량·보관 기간 실측 필요)
- 틱봉의 시간축 처리: M1 실측(2026-07-09, 005930 `tick30`)에서는 캐시와 `/api/chart` 응답 모두
  unix 초 timestamp 중복 0건. 다른 틱 단위에서 중복이 발견되면 ① 초 단위 오프셋 부여,
  ② 순번 기반 가상 시간축 중 택일 (프랙탈 계산은 순서만 중요해서 영향 없음)
- `Amount` 필드: Kiwoom `ChartBar.amount` 존재 확인. 표준 스키마의 `Amount`로 직접 매핑
- 학습 디바이스: 현재 개발 머신이 macOS이므로 MPS/CPU 기준 (구 프로젝트는 CUDA).
  현행 모델 크기(CNN1D)면 CPU로도 충분 — 디바이스 자동 감지(`mps > cpu`)로 시작
- 실시간 추론의 판정 주기: 봉 마감 시마다 vs 틱마다(현재 미완성 봉 포함) —
  학습 데이터가 "확정된 봉"으로 구성되므로 **봉 마감 시 판정**을 기본으로 하고,
  틱 단위 잠정 판정은 옵션으로 (구 프로젝트는 틱마다 추론 시도)
- 일봉 기준 실시간 추론의 의미: 일봉 모델이면 판정은 하루 1회(장 마감 무렵)가 자연스러움.
  장중 잠정 판정(현재까지의 당일 봉을 마지막 봉으로 간주)을 보조 표시할지 결정 필요
