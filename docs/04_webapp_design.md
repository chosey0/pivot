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
| 저장소 | 원천 캔들/watchlist는 로컬, 학습 메타데이터는 Supabase Postgres, 데이터셋/모델 파일은 private Storage |

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
  - 종목 추가 폼은 별도 종목코드 입력 없이 `시장 + 종목명 또는 코드` 검색 입력을 사용한다.
    국내/해외 시장을 선택하고, 해외는 NASDAQ/NYSE/AMEX 거래소를 선택하며 검색 결과가
    실제 종목코드와 거래소를 채운다.
  - 국내 종목마스터는 Supabase `public.domestic_master`에 저장한다.
  - 원천은 `broker-modules`의 `brokers.kis.symbols` KOSPI/KOSDAQ master이며, 우선 범위는 보통주다.
  - 검색은 Postgres `pg_trgm` 기반 RPC `search_domestic_master(query, match_limit)`로 수행한다.
  - 미국 종목마스터는 같은 SDK의 NASDAQ/NYSE/AMEX 전체 master를 `public.overseas_master`에 저장한다.
    기본키는 `(market, symbol)`이며 원천 `raw` JSON은 저장하지 않는다.
    `search_overseas_master(query, match_limit)`로 검색하고 `scripts/update-overseas-master.sh`로
    전체 스냅샷을 갱신한다.
  - 종목코드/종목명 입력 중 가장 유사한 후보를 드롭다운으로 표시하고, 방향키/Enter로 선택한다.
- **타임프레임 선택 수집**: 일봉 / N분봉 / N틱봉 중 선택해 수집
  - 타입(일/분/틱) + 단위 드롭다운 (분: 1,3,5,10,15,30,45,60 / 틱: 1,3,5,10,30 — 기본값 1)
  - 같은 종목이라도 타임프레임별로 캐시가 분리됨 (`data/raw/{broker}/{timeframe}/{symbol}.parquet`)
  - 국내는 `kiwoom`, 해외는 `kiwoom-overseas-{nd|ny|na}` broker 경로를 사용한다.
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

M1의 해외 지원 범위는 관심종목 검색·수집·캐시·차트다. 전처리 실험실, 데이터셋,
데이터 진단은 현재 국내 종목 식별자 계약을 유지하므로 국내 관심종목만 표시한다. 실시간 탭도
국내 종목만 지원하지만 관심종목 목록이 아니라 국내 종목마스터 검색에서 직접 구독을 추가한다.

### 1.2 전처리 실험실 (Lab) — 핵심 화면

```
┌────────────┬──────────────────────────────────────┬───────────────┐
│ 관심종목    │  캔들차트 (lightweight-charts)         │ 파라미터 패널   │
│ 리스트      │   - 캔들 + MA 라인(5/20/120)          │  fractal n     │
│            │                                      │  tie policy    │
│ (클릭 시    │   - 프랙탈 마커:                       │  피처 선택      │
│  차트 로드) │     ▲ 저점(0)  ▼ 고점(1)  ● 무시(2)   │  필터 토글:     │
│            │   - 마커 클릭 → 해당 샘플의            │   정배열/거래대금│
│            │     입력 윈도우(인접 pair) 하이라이트    │  라벨/페어링 모드│
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
- `labeling.sample_pairing`은 샘플의 두 endpoint를 결정한다.
  - `adjacent_markers_v1` (**신규 기본값**): 필터·plateau·클리닝 후 남은 마커를 시간순으로
    정렬하고 바로 이웃한 `(i-1, i)`를 연결한다. 입력 윈도우는 양 끝을 포함하는 가변 길이다.
    같은 종류 pair는 base label `2`, 다른 종류 pair는 도착 마커 기준 저점 `0`/고점 `1`이다.
    예: `L1→L2→H`는 `[2,1]`, `L1→L2→L3→H`는 `[2,2,1]`이다.
  - `latest_opposite_v1` (**저장 legacy fallback**): 종류별 최신 반대 마커부터 현재 마커까지
    연결한다. 저장 schema v1 프리셋에서 필드가 누락된 경우에만 이 규칙으로 읽는다.
  - 새 preview 요청과 새 프리셋은 `adjacent_markers_v1`을 사용한다. 구 방식의 `max_len` 고정
    길이 윈도우는 사용하지 않는다.
- `fractal.tie_policy` 기본값은 `plateau_last`다. 같은 종류·같은 가격의 연속 프랙탈 후보를
  하나의 plateau event로 보고 마지막 봉만 대표 라벨로 남긴다. `all`은 legacy 비교용이며,
  필드가 없는 기존 schema v1 저장 프리셋은 재현성을 위해 `all`로 읽는다.
- `labeling.ignore_swing_pct` (선택): 선택된 pair의 시작과 끝 가격 변화율이 이 값(%) 미만이면
  잔진동으로 보고 base `0/1`을 **무시(2)**로 덮어쓴다. 역배열 무시 규칙과 독립적으로
  조합된다. `adjacent_markers_v1 + cls2_drop`은 label `2` 샘플만 제외하고 도착 마커는 다음
  pair의 anchor로 유지한다. `latest_opposite_v1`은 기존 point 제거·anchor 제외 순서를 보존한다.
  필드가 없는 기존 프리셋은 미적용(null)으로 읽는다.
- preview marker의 `kind`와 기존 `label`은 구조적 마커/표시 의미를 유지한다. pair 관계는
  `incoming_sample_label`, `incoming_sample_included`, `incoming_sample_index`,
  `incoming_sample_drop_reason`으로 별도 반환하며 UI는 시간 추론 대신 index로 샘플을 선택한다.
- 기존 top-level `dropped_ignore`/`dropped_unpaired`는 호환을 위해 유지하고, adjacent 상세는
  `pairing_stats={rule, adjacent_edges, unpaired_markers, dropped_invalid_position,
  dropped_label2}`로 반환한다. 각 세그먼트에서 다음 식이 성립해야 한다.

  ```text
  points = adjacent_edges + unpaired_markers
  adjacent_edges = samples + dropped_label2 + dropped_nan + dropped_invalid_position
  ```
- 샘플 상세 패널(선택): 해당 시퀀스의 스케일링 후 값 미리보기

### 1.3 일괄 처리 & 데이터셋 (Datasets)

- 프리셋 선택 + 대상(관심종목 전체 또는 선택) → **일괄 전처리 실행**
- 진행 상황: SSE로 종목별 진행률/성공/실패/샘플 수 스트리밍
- 완료 시 데이터셋 카드 생성: 이름, 생성일, 사용 프리셋(파라미터 스냅샷), 샘플 수, 클래스 분포
- 데이터셋 상세 = **샘플 브라우저**: 샘플을 페이지 단위로 넘기며 미니 차트로 개별 검수
  (라벨별 필터, 무작위 샘플링 보기)
- 구현 상태 (M3 완료): 프리셋 목록/저장(버전 증가)/보관/보관함 조회/미참조 버전 영구 삭제, 종목 선택 → batch 실행,
  SSE 진행률(종목별 성공/실패 칩) + **취소 버튼**, 데이터셋 목록(상태·샘플 수·클래스 분포)
  + **삭제**, **샘플 브라우저**(라벨 필터, 페이지/←→ 키 이동, 무작위 선택, 미니 차트 +
  원본 피처 테이블)까지 동작. 프리셋 저장 UI는 Lab 파라미터 패널에 있다.
  샘플의 전역 순번은 shard 정렬(symbol asc, shard_index asc) 기준으로 안정적이다.
  미니 차트의 x축은 봉 순번이다 — 샘플 피처에 Time이 없으므로(백로그 A2) 시간축을 숨긴다.

### 1.4 데이터 진단 (Diagnostics)

원천 캔들, 전처리 결과, 데이터셋이 학습 가능한 품질인지 확인하는 독립 품질 게이트.
Lab은 개별 종목을 눈으로 확인하는 화면이고, Diagnostics는 전체 관심종목/데이터셋의 이상 징후를
한 번에 찾는 화면이다.

```
┌──────────────────────┬──────────────────────────┬─────────────────────┐
│ 진단 대상             │  진단 결과 요약           │ 리포트 이력(접기 가능) │
│  - raw cache: 종목/타임프레임    │   통과/경고/실패 카운트              │
│  - preset preview: 프리셋+종목   │   주요 경고: 누락, 중복, NaN, 불균형  │
│  - dataset: 생성된 데이터셋      │   [진단 실행] [리포트 저장]          │
├──────────────────────┴──────────────────────────┴─────────────────────┤
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
  - [Kronos Appendix B](https://arxiv.org/abs/2508.02739)의 K-line 품질 절차를 적용해
    가격 결측/불변식 위반, 시가-직전 종가 구조적 점프, 장기 비유동, 비활성 가격 정체를 탐지
- 라벨/프리셋 진단:
  - 선택한 프리셋으로 종목별 라벨 수와 클래스 `0/1/2` 비율 산출
  - 특정 클래스가 거의 없거나 `2`가 과도하게 많은 종목을 경고
  - plateau 정규화로 제거한 후보 수와, 정규화 후에도 남은 90% 이상 overlap cluster 수를 표시
  - `fractal.n`, `ma_source`, 필터 설정이 샘플 수에 미치는 영향 요약
- 데이터셋 진단:
  - 데이터셋 생성 후 샘플 수, 클래스 분포, 종목별 기여도, 길이 분포 확인
  - shard의 샘플 메타데이터로 종목별 overlap cluster, 군집 샘플 수, 중복 추정 수,
    최대 cluster 크기를 재계산한다. 통계는 경고이며 자동 삭제나 재라벨링은 하지 않는다.
  - train/val/test split이 종목 단위로 누수 없이 나뉘었는지 검사
  - 학습 전 체크리스트 형태로 “진행 가능/주의/중단 권장” 상태 표시
- 리포트는 재현성을 위해 Supabase `diagnostic_reports`에 저장하지만, 원천 데이터 수정은 하지 않는다.
  수정이 필요하면 Watchlist 수집 갱신 또는 Lab/프리셋 조정으로 되돌아간다.
- 구현 상태 (M3 완료): 검사 로직은 `pivot/diagnostics/quality.py`(순수 함수),
  리포트 저장은 `pivot/storage/diagnostics.py`. 각 검사는 `{id, symbol?, status, message, data}`
  형식이고 리포트 전체 상태는 최악 검사 결과를 따른다(passed/warning/failed —
  `diagnostic_reports.status` 값과 동일). 데이터셋 진단의 split 검사는 `preset_snapshot.split`의
  seed/규칙으로 배정을 재계산해 실제 배정과 비교한다(누수/규칙 위반 검출).
  리포트 이력은 결과 오른쪽의 접이식 사이드바에서 조회한다. 진단은 동기 실행이다(로컬 단일
  사용자 규모) — 오래 걸리면 job+SSE로 전환한다.

#### Kronos 적응형 클리닝 정책

- 정책 ID는 `kronos_adapted_v1`이며 기본 모드는 `report_only`다. 원천 parquet는 수정하지 않고,
  경계와 제외 후보만 진단/프리셋/데이터셋 provenance에 기록한다.
- `filter` 모드에서만 정상 세그먼트를 남긴다. 이동평균, 프랙탈 라벨과 스윙 샘플은 세그먼트마다
  독립적으로 다시 계산해 이상 경계를 가로지르는 학습 샘플을 만들지 않는다.
- 논문의 일봉·1/5/10/15/30/60분 임계값을 기본값으로 사용한다. 프로젝트의 3분봉은 5분,
  45분봉은 60분 기준을 차용하고 `source_frequency`로 기록한다. 최소 세그먼트 길이는 논문의
  사전학습 길이가 아니라 현재 프리셋의 최대 MA/fractal 요구 길이를 사용한다.
- 틱봉은 논문에 대응 주기가 없으므로 기본값으로 OHLC 필드 무결성만 검사한다. 틱 임계값은
  실측 후 프리셋 override로만 활성화한다.
- 논문의 Volume/Amount 5% 무작위 마스킹은 품질 정제가 아니라 학습 증강이다. M4에서 train
  split에만 적용하는 별도 실험으로 검증하며, 현재 데이터셋 생성에는 적용하지 않는다.
- 국내 주식의 가격제한·분할·거래정지 특성에 맞춘 적응형 정책이므로 성능 향상을 전제하지 않는다.
  M4에서 `off/report_only` 기준 데이터셋과 `filter` 데이터셋을 동일 split으로 비교한다.

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
- 학습은 장기 job: **별도 spawn 프로세스**로 실행해 torch 학습이 FastAPI 이벤트 루프를
  막지 않게 한다. epoch와 상태 이벤트는 `training_epochs`와 `job_events`에 먼저 기록하고
  SSE는 이를 폴링 전달한다. 중단은 durable job 상태를 바꾸고 worker가 batch 경계에서 확인한다.
- 학습 입력은 `ready` 데이터셋의 저장된 종목 split만 사용한다. 세 split이 비었거나 shard
  누락·SHA-256 불일치·최신 데이터셋 진단 `failed`이면 run 시작을 거부한다.
- 샘플별 표준화 후 0 padding하며 length/mask로 padding이 모델 표현에 섞이지 않게 한다.
  기본 선택 지표는 validation macro F1이고 3클래스 confusion matrix와 클래스별 P/R/F1을 저장한다.
- 모델은 구 1x1 CNN 구조를 재현한 `cnn1d_legacy_v1`과 시간축 kernel을 사용하는
  `cnn1d_temporal_v1`을 제공한다.
- 평가 지표는 백로그 A5(train/val 분리)·A6(클래스별 지표) 반영이 전제
- ③ 차트 검증은 Lab과 같은 차트 컴포넌트 재사용: 실제 라벨(▲▼)과 예측(테두리/색 변형)을
  한 차트에 겹치고, 오분류만 필터해 보는 토글 제공
- terminal run은 목록에서 삭제할 수 있다. 배포 이력에서 참조 중인 run은 보존하며, 삭제는
  checkpoint 객체 성공 후 epoch/evaluation/artifact/run 메타데이터를 제거한다.

#### 베이스라인 학습 계약 (M4 착수 전 확정)

코드보다 먼저 확정하는 계약. 변경 시 이 문서를 먼저 고친다.

- **입력 변환**: shard에는 원본 피처 값이 저장돼 있으므로, 로더가 **샘플(윈도우) 단위
  StandardScaler**로 변환한다. 추론도 동일한 `pivot` 변환 함수를 재사용한다 (§1.6).
- **가변 길이**: 0 패딩 + `mask`/`length`를 함께 전달해 패딩이 손실·지표에 영향을
  주지 않게 한다 (백로그 A3).
- **split**: 데이터셋에 저장된 **종목 단위 split을 그대로 사용**한다. 학습 코드는
  재분할하지 않는다 (백로그 A5).
- **손실/선택 기준**: 3클래스 CrossEntropy 기본, 모델 선택은 클래스 불균형에 강한
  **validation macro F1** (백로그 A6).
- **run snapshot**: seed, scaler 설정, 피처 순서, 라벨 매핑을 `training_runs`에
  불변 스냅샷으로 저장한다 (docs/06 §2).
- Volume/Amount 무작위 마스킹, Kronos `filter` 데이터셋 비교는 **베이스라인 이후
  별도 실험**으로 분리한다 (§1.4.1).

#### 학습 전 품질 게이트

학습 시작 요청 시 서버가 검사한다. 진단 탭(§1.3)과 별개로 학습 직전에 재검증한다.

- 데이터셋 status가 `ready`가 아니면 차단
- 데이터셋 진단 리포트가 `failed`면 차단
- shard 존재 여부 + SHA-256 재검증 실패 시 차단
- train/validation/test split이 하나라도 비어 있으면 차단
- 클래스별 샘플 수·종목 기여도 이상은 경고로 표시 (차단하지 않음)

#### M4 회귀 테스트 명세

- **split 누수**: 같은 종목이 두 split에 나타나면 로더가 거부
- **padding 불변성**: 패딩 길이를 바꿔도 모델 출력·손실이 동일
- **transform 동일성**: 학습 로더와 추론 경로의 변환 결과가 동일
- **클래스별 지표**: 알려진 confusion matrix에서 per-class precision/recall/F1 검증
- **체크포인트 무결성**: 저장 → 업로드 → 다운로드 → 로드 후 출력 동일, SHA-256 일치
- **durable 상태 전이**: 학습 프로세스 실패·취소 시 run이 terminal 상태로 남고,
  ready 데이터셋은 영향받지 않음

### 1.6 실시간 추론 (Live)

구 `main.py`(PyQt5 + KIS 웹소켓 + `infer_plot*.py`)의 웹 버전이지만, 신규 구현의
증권사 연결은 **broker-modules Kiwoom WebSocket `주식체결(0B)`**로 고정한다.

```
┌────────────────────┬──────────────────────────────────────────────┐
│ 구독 테이블          │  실시간 캔들차트 (선택 종목)                     │
│  종목 검색/추가       │   - 과거 봉 로드 후 체결가로 현재 봉 실시간 갱신   │
│  종목별:             │   - 모델 후보 마커: 저점/고점 점수 표시            │
│   현재가, 등락률      │  하단: 최근 판정 로그 (시각, 종목, 클래스, 후보 점수) │
│   최근 판정 결과      │                                              │
│  사용 모델(체크포인트) │                                              │
└────────────────────┴──────────────────────────────────────────────┘
```

- 데이터 경로: **Kiwoom WebSocket ↔ FastAPI(중계·집계·추론) ↔ 브라우저 WebSocket**
  - 증권사 연결·인증키는 서버에만 존재, 브라우저는 서버 WS만 구독
  - FastAPI lifespan에서 Kiwoom session 하나를 유지하고 종목별 `subscribe_trades`를 동적으로 관리
  - SDK의 자동 재접속·구독 복원 후 Kiwoom REST 캐시를 갱신해 누락된 마감 봉을 보정
  - 화면 차트 이력은 캐시와 분리된 `/api/live/history`에서 Kiwoom REST를 직접 조회. `min1`은
    당일 분봉부터 표시하고 좌측 이동 시 이전 7일 구간을 반복해서 이어 붙임
  - 서버가 체결 틱을 봉(현재 봉 갱신 + 봉 마감)으로 집계 → 봉 마감 시점마다 시퀀스 구성 → 추론
  - 브라우저는 `series.update()`로 현재 봉 갱신, 판정 발생 시 마커 추가
- 추론 입력은 전처리와 **동일한 `pivot` 패키지의 스케일링/시퀀스 구성 함수** 사용
  (학습-추론 전처리 불일치 방지 — 구 프로젝트에서 `infer_plot copy*.py`가 난립했던 원인 제거)
- 활성 run snapshot의 `labeling.sample_pairing`을 읽어 후보 윈도우도 학습과 동일하게 구성한다.
  legacy는 최근 low/high별 두 후보, adjacent는 시간상 최신 retained marker부터 현재 bar까지
  단일 shared 후보를 사용한다. snapshot에 필드가 없으면 legacy로 읽는다.
- 체크포인트 선택: Training 탭에서 "실시간 추론에 사용"으로 지정한 모델 기본 로드
- 장 마감/미개장 시간대에도 Kiwoom REST에서 조회한 과거 차트 + "장 종료" 상태 표시
- cls3 모델은 비프랙탈 음성 샘플을 학습하지 않았으므로 출력은 `실험적 후보 점수`로 표시한다.
  자동매매 신호나 프랙탈 절대 확률로 표현하지 않는다.
- 세부 봉 집계·추론·HTTP/WS 메시지 계약은 [08_m5_implementation_plan.md](08_m5_implementation_plan.md)를 따른다.

## 2. 핵심 개념: 전처리 프리셋

Lab에서 튜닝한 파라미터 집합을 이름 붙여 저장. Lab(단건)과 일괄 처리(전체)가 **같은 프리셋을
공유**하므로 "몇 종목으로 확인한 그대로 전체에 적용"이 보장된다.

```jsonc
// Supabase public.training_presets.preset
{
  "name": "day20_ma20120_cls3",
  "timeframe": { "type": "day", "unit": 1 },       // day | minute | tick, 분/틱은 N 단위 (기본 1)
  "fractal": {
    "n": 20,                                      // center rolling window 크기
    "tie_policy": "plateau_last"                 // 동률 plateau의 마지막 봉만 라벨
  },
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
  // 입력 윈도우 endpoint는 labeling.sample_pairing으로 결정한다 (§1.2 참고).
  "labeling": {                                    // 백로그 B2 실험을 위해 모드화
    "mode": "cls3",                                // cls3 | cls2_drop | cls4 | with_negative
    "sample_pairing": "adjacent_markers_v1",       // adjacent_markers_v1 | latest_opposite_v1
    "ignore_rule": "ma20 < ma120",
    "ignore_swing_pct": null                       // 선택된 pair 변화율(%)이 미만이면 무시(2), null이면 미적용
  },
  "filters": {
    "ma_alignment": null,                          // null | "20>120" | "5>20>120"
    "min_amount": null                             // 원 단위, null이면 미적용
  },
  "cleaning": {
    "mode": "report_only",                       // off | report_only | filter
    "policy": "kronos_adapted_v1",
    "price_jump_threshold": null,                 // null이면 timeframe 기본값
    "max_illiquid_bars": null,
    "max_stagnant_bars": null,
    "min_segment_bars": null                      // null이면 MA/fractal 요구 길이
  },
  "created_at": "...", "updated_at": "..."
}
```

데이터셋 메타에는 사용한 프리셋의 **스냅샷**을 통째로 저장한다 (프리셋을 나중에 수정해도
데이터셋 재현 가능 — 구 프로젝트의 `{dataset}.json` 메타 방식 계승).
저장 프리셋·dataset/run snapshot의 `labeling.sample_pairing` 누락은 서버의 단일 compatibility
resolver에서 `latest_opposite_v1`로 materialize한다. 신규 raw preview/preset 요청은 resolver를
거치지 않고 Pydantic의 새 기본값 `adjacent_markers_v1`을 사용한다.

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
| GET | `/api/live/history/{symbol}?timeframe=&ma=&before=` | Live 전용 Kiwoom REST 이력 페이지. 로컬 parquet 미사용 |
| POST | `/api/preprocess/preview` | `{symbol, params}` → 캔들/MA/거래량 + 구조 마커(`incoming_sample_*` 포함) + 샘플 목록(윈도우 범위/라벨) + 클래스/`pairing_stats` 통계 |
| GET/POST | `/api/presets` | 프리셋 목록/생성. 저장 legacy row는 서버에서 pairing fallback을 materialize해 반환 |
| PUT/DELETE | `/api/presets/{id}` | 새 프리셋 버전 생성 / 보관 |
| DELETE | `/api/presets/{id}/permanent` | 데이터셋이 참조하지 않는 프리셋 버전 영구 삭제 |
| POST | `/api/preprocess/batch` | `{preset_id, dataset_name, symbols[]}` → durable job_id |
| GET | `/api/jobs/{id}/events` | SSE: 진행률/종목별 결과 |
| POST | `/api/jobs/{id}/cancel` | queued/running job을 cancelled로 전이 (worker는 종목/shard 경계에서 협조적 중단) |
| GET | `/api/datasets` | 데이터셋 목록 + 메타 |
| DELETE | `/api/datasets/{id}` | 객체 목록 확정 → Storage 삭제 → 메타데이터 삭제. 시도는 `dataset_delete` job으로 기록, 부분 실패는 같은 호출로 재시도 |
| POST | `/api/datasets/cleanup` | orphan 객체 / stale building 데이터셋 / stale job 정리 (멱등) |
| GET | `/api/datasets/{id}/samples?label=&offset=&limit=` | 샘플 브라우저용 페이지 조회 (ready 데이터셋만, 전역 안정 순번) |
| GET | `/api/datasets/{id}/samples/{i}` | 샘플 1건 상세 (원본 피처 시퀀스 — 스케일링은 로더 책임, §8 참고) |
| POST | `/api/diagnostics/cache` | `{symbols[], timeframe}` → 원천 캐시 품질 리포트 |
| POST | `/api/diagnostics/preview` | `{preset_id, symbols[]}` → 프리셋 적용 전 라벨/샘플 분포 리포트 |
| POST | `/api/diagnostics/datasets/{id}` | 데이터셋 품질·split 누수·클래스 분포 리포트 |
| GET | `/api/diagnostics?target_type=` / `/api/diagnostics/{id}` | 저장된 진단 리포트 목록(요약)/상세 |
| GET/POST | `/api/runs` | 학습 run 목록 / 새 학습 시작 (`{name, dataset_id, config}` → run_id, job_id) |
| GET | `/api/runs/{id}` | run 상세 (설정, 곡선, 평가 지표, 체크포인트 목록) |
| GET | `/api/runs/{id}/events` | SSE: epoch별 loss/acc 실시간 스트리밍 |
| POST | `/api/runs/{id}/stop` | 학습 중단 |
| DELETE | `/api/runs/{id}` | terminal·미배포 run의 Storage 객체 삭제 후 학습 메타데이터 삭제. `run_delete` job으로 기록 |
| POST | `/api/runs/{id}/evaluate` | `{symbol, split}` → 차트 검증용 예측 결과 (실제 라벨 vs 예측) |
| GET | `/api/live/state` | Kiwoom 연결, 활성 모델/timeframe, 구독·집계·오류 상태 snapshot |
| PUT | `/api/live/model` | 실시간 추론에 사용할 체크포인트 지정 |
| GET/POST | `/api/live/subscriptions` | Kiwoom `주식체결(0B)` 구독 종목 조회/추가 |
| DELETE | `/api/live/subscriptions/{symbol}` | 종목 구독 해제 |
| WS | `/ws/live` | 브라우저용 WebSocket: 현재 봉 갱신 + 모델 판정 이벤트 푸시 |

설계 원칙:

- `preview`와 `batch`는 **동일한 `pivot` 패키지 함수**를 호출 (단건/일괄이 같은 코드 경로 → 결과 불일치 방지)
- 수집(ingest)과 전처리(preprocess)는 완전 분리 — 전처리는 항상 로컬 캐시(parquet)만 읽음
- 장기 작업(수집, 일괄 전처리)은 job + SSE 패턴을 통일한다. 학습 관련 job 상태와 이벤트는
  Supabase에 영속화하고, SSE는 전달 계층으로만 사용한다.

## 4. 저장 구조 (로컬 + Supabase)

```
data/
├─ raw/{broker}/{timeframe}/{symbol}.parquet    # 캔들 캐시, timeframe = day | min{N} | tick{N}
├─ meta/watchlist.json                          # 로컬 UI 운영 상태
└─ tmp/                                         # 재생성 가능한 작업 캐시

Supabase Postgres
├─ training_presets, jobs, job_events
├─ datasets, dataset_symbols, dataset_shards
├─ diagnostic_reports
└─ training_runs, training_epochs, evaluations, training_artifacts

Supabase Storage (private)
├─ pivot-datasets/datasets/{dataset_id}/{symbol}/part-*.parquet
└─ pivot-models/runs/{run_id}/...
```

원천 캔들 수집과 차트 조회는 대용량·구간 접근에 유리한 로컬 parquet를 유지한다. 반면
프리셋부터 파생되는 학습 관련 데이터는 Supabase를 단일 원본으로 사용한다. Postgres에는
검색·상태·관계·재현성 메타데이터를 저장하고, 가변 길이 시퀀스 parquet와 체크포인트 같은
바이너리는 private Storage에 저장한다. 로컬 복사본은 업로드/다운로드 중 임시 캐시일 뿐이며
복구 근거가 아니다. 상세 계약은 [06_supabase_training_storage.md](06_supabase_training_storage.md)를 따른다.

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

- 시간값: 일봉은 `'yyyy-mm-dd'` 문자열, 분봉/틱봉은 KST 벽시계를 UTC 필드로 복원하는
  unix timestamp(초) — REST 이력과 실시간 WS 이벤트가 모두 이 형식으로 내려줌.
  lightweight-charts는 시간값이 **고유하고 오름차순**이어야 하므로, 같은 초에 여러 봉이 생길 수
  있는 틱봉은 백엔드에서 시간 충돌을 해소해 내려준다 (열린 결정 참고)
- 라벨 표시 규약: 저점 `arrowUp/belowBar/초록`, 고점 `arrowDown/aboveBar/빨강`, 무시 `circle/회색`
- 같은 종류 pair의 sample label `2`를 도착 마커 자체의 low/high 종류로 오인하지 않는다.
  마커 도형은 기존 marker-level `kind`/`label`, pair 결과는 `incoming_sample_*`가 source of truth다.
- 샘플 입력 윈도우 하이라이트: 마커 클릭(`subscribeClick`) → `incoming_sample_index`로 해당
  구간을 찾아 반투명 배경으로 표시
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
| **M3** | 프리셋 + 일괄 처리 + 데이터 진단: Supabase preset CRUD, durable job + SSE, private Storage 데이터셋 shard, 샘플 브라우저 + Diagnostics 탭, batch 취소·데이터셋 삭제·orphan/stale 정리. **M3-A·M3-B 완료** (`20260711064111` 삭제 job 마이그레이션 적용 완료) | 프리셋으로 전체 종목 일괄 전처리 → Supabase 데이터셋 생성/검수 → 품질 리포트 확인 |
| **M4** | 학습 & 평가: `pivot.training` (백로그 A5/A6/B1 베이스라인), Postgres run/epoch/평가 관리 + Storage 체크포인트 + 학습 곡선 SSE, 차트 검증. **완료** (2026-07-12) | 웹에서 학습 시작 → 곡선/지표 확인 → 차트에서 예측 vs 실제 비교 |
| **M5** | 실시간 추론: Kiwoom WebSocket `0B` 중계, timeframe별 봉 집계 + 실시간 추론, Live 화면. **core+UI·원격 migration·무모델 구독/재접속 검증 완료, 모델 활성화·장중 검증 대기** (2026-07-13) | 장중에 구독 종목의 현재 봉과 실험적 후보 점수가 차트에 표시되고 재접속 후 누락 없이 복구 |

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
- 실시간 추론의 판정 주기: **봉 마감 시 한 번**으로 확정. 미완성 봉은 차트에만 잠정 표시하고
  모델 판정을 만들지 않는다.
- 일봉 모델은 정규장 종료 시 한 번 판정한다. 장중 당일 봉은 잠정 차트 데이터이며
  `prediction` 이벤트를 만들지 않는다.
