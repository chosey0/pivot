# 데이터 수집 설계 — broker-modules SDK

구 프로젝트는 한국투자증권 HTS에서 수동으로 다운로드한 CSV를 원천 데이터로 사용했다.
새 프로젝트는 [broker-modules](https://github.com/chosey0/broker-modules) 저장소에 구현된
증권사 OpenAPI SDK로 캔들 데이터를 직접 조회한다.
이로써 HTS CSV 포맷에 묶여 있던 파싱 로직(콤마 제거, 분봉 연도 추정, 역순 정렬 등)이 모두 불필요해진다.

## 1. broker-modules 개요

증권사 OpenAPI 인증, REST 조회, WebSocket 실시간 시세, 응답 파싱을 제공하는 순수 Python SDK.

| 브로커 | 모듈 | 상태 | 캔들 관련 기능 |
|---|---|---|---|
| 키움증권 | `brokers.kiwoom` | 구현 중 | 국내 틱/분/일/주/월/년봉, 수정주가, 전체 페이지 조회 |
| 한국투자증권 | `brokers.kis` | 구현 중 | 국내 분봉(1분, 120건 단위 페이지네이션), 해외 일봉/분봉, 실시간 WebSocket |
| 토스증권 | `brokers.toss` | 구현됨 | 현재가/캔들/종목정보, 장 운영정보 |
| KRX | `brokers.krx` | 초기 구현 | 지수 일별 가격 |

- Python **3.12+**, HTTPX + WebSockets 기반, **전체 API가 async** (`async with` 컨텍스트 매니저 필수)
- 패키지 관리는 uv 기준

## 2. 타임프레임과 브로커 선택

학습 데이터의 타임프레임은 **일봉 / N분봉 / N틱봉** 중 선택할 수 있어야 한다 (N 기본값 1).
국내 캔들은 세 종류 모두 Kiwoom 모듈로 조회한다.

| 타임프레임 | Kiwoom 메서드 | 지원 단위 (N) | 코드 표기 |
|---|---|---|---|
| 일봉 | `daily(symbol, base_date)` | — | `day` |
| N분봉 | `minute(symbol, interval_minutes, base_date)` | 1, 3, 5, 10, 15, 30, 45, 60 (기본 1) | `min{N}` (예: `min1`, `min5`) |
| N틱봉 | `tick(symbol, tick_scope)` | 1, 3, 5, 10, 30 (기본 1) | `tick{N}` (예: `tick1`, `tick30`) |

- 공통 옵션: `adjusted=True`(수정주가), `max_pages=None`(전체 페이지 조회)
- 단위 N은 **SDK가 지원하는 값 목록에서 선택** (UI는 드롭다운). 지원 외의 N이 필요해지면
  1단위 캐시에서 로컬 리샘플링으로 합성하는 방안을 추후 검토
- 내부적으로 타임프레임은 `{type: day|minute|tick, unit: int}` 객체로 다루고,
  경로/API 문자열로는 `day`/`min{N}`/`tick{N}` 코드를 사용
- 수집 기간은 선택적으로 지정할 수 있다. UI/API는 `start`/`end` 날짜(`YYYY-MM-DD`)를 받고,
  일봉·분봉은 `end`를 Kiwoom `base_date`로, 세 타임프레임 모두 `start`를 SDK `start_date`로 넘긴다.
  틱봉은 SDK에 `base_date` 인자가 없으므로 조회 후 `end` 날짜 이내로 필터링해 캐시에 병합한다.
  기간을 지정하지 않으면 기존 캐시의 마지막 봉 이후만 증분 수집한다.

| 용도 | 사용 모듈 | 근거 |
|---|---|---|
| **국내 캔들 (일/분/틱)** — 학습 데이터 | `brokers.kiwoom` | 위 표. KIS 모듈은 국내는 1분봉만 지원 |
| 실시간 체결 (추론 단계) | `brokers.kis` 또는 `brokers.kiwoom` | 둘 다 실시간 체결 WebSocket 지원. 구 프로젝트가 KIS 웹소켓 기반이었으므로 KIS 우선 검토 |

**주의 (분봉/틱봉)**:

- 조회량이 일봉 대비 크게 늘어 rate limit·수집 시간 부담이 큼 — 캐시 필수, 증분 갱신 설계
- M1 실측(2026-07-09, 005930):
  - `min1`: 95,639봉, 2025-07-01 09:00:00 ~ 2026-07-09 15:30:00, 최초 전체 수집 약 28초
  - `tick30`: 240,762봉, 2026-06-11 09:00:14 ~ 2026-07-09 15:19:57, 최초 전체 수집 약 95초
  - 두 캐시는 timestamp 오름차순이며 중복 timestamp 0건. `/api/chart` 응답도 분/틱 time을 unix 초 숫자로 반환
- 이평선 기준 결정 필요: 해당 타임프레임 기준 rolling(기본) vs 일봉 이평선 병합(구 프로젝트의
  `*_ma.csv` merge 방식) — 프리셋 옵션 `ma_source: self | daily`로 둘 다 지원

## 3. 설치 및 인증

`pyproject.toml`:

```toml
[project]
requires-python = ">=3.12"
dependencies = ["broker-modules"]

[tool.uv.sources]
broker-modules = { git = "https://github.com/chosey0/broker-modules.git" }
# 로컬 개발 시: broker-modules = { path = "../broker-modules", editable = true }
```

인증은 환경변수로 (구 프로젝트의 `env.yaml` 방식 대체, `.env`는 git 미추적):

```bash
export KIWOOM_APP_KEY="..."
export KIWOOM_SECRET_KEY="..."
export KIS_APP_KEY="..."        # KIS 사용 시
export KIS_APP_SECRET="..."
```

토큰 발급/캐시/만료 전 갱신은 SDK가 자동 처리한다.

## 4. 조회 예시 (국내 일봉, Kiwoom)

```python
import asyncio
from brokers.kiwoom import Credentials, KiwoomClient

async def fetch_daily(symbol: str, base_date: str):
    async with KiwoomClient(credentials=Credentials.from_env()) as client:
        bars = await client.domestic.chart.daily(
            symbol, base_date=base_date, adjusted=True, max_pages=None
        )
    return bars  # list[ChartBar]

bars = asyncio.run(fetch_daily("005930", "2026-07-09"))
```

반환 모델 `ChartBar`: `market, symbol, interval, timestamp, open, high, low, close, volume, amount, raw(원본 응답)`

## 5. 내부 스키마 매핑

`ChartBar` 리스트 → 파이프라인 표준 DataFrame:

| 표준 컬럼 | 출처 | 비고 |
|---|---|---|
| `Time` | `timestamp` | DatetimeIndex, 과거 → 최근 오름차순 정렬 |
| `Open/High/Low/Close` | `open/high/low/close` | |
| `Volume` | `volume` | |
| `Amount` (거래대금) | `ChartBar.amount` | 유동성 필터(백로그 B5)에 사용 |
| 이평선 컬럼(예: `5`, `20`, `60`, `120`) | **직접 계산** — `Close.rolling(n).mean()` | ⚠ HTS CSV에는 포함돼 있었지만 SDK 응답에는 없음. 기간 n은 차트 요청/전처리 프리셋에서 선택 |

**이평선 직접 계산에 따른 이점**:

- 분봉 데이터에 일봉 이평선을 붙이던 `*_ma.csv` merge 핵이 필요 없어짐 (일봉을 함께 조회해 계산)
- HTS 값과 달리 계산 기준(단순/지수, 수정주가 여부)을 우리가 통제

**주의**: 이평선을 직접 계산하면 시퀀스 앞쪽 `n-1`개 봉은 NaN이다.
가장 긴 이평선 기준 최소 `max(n)`봉 이전 데이터까지 여유 있게 조회해야 학습 구간이 잘리지 않는다.

## 6. 수집 파이프라인 구성 (구현 예정)

```
종목 리스트 (KOSPI/KOSDAQ 심볼 마스터 — brokers.kis 심볼 마스터 또는 별도 정의)
   │  fetch (async, rate limit 고려)
   ▼
ChartBar 리스트 → 표준 DataFrame 변환 + 이평선 계산
   │  캐시 저장: data/raw/{broker}/{timeframe}/{symbol}.parquet   # timeframe = day | min{N} | tick{N}
   ▼
이후 단계는 기존 전처리와 동일: calc_fractal() → create_dataset()
```

- **로컬 캐시 필수**: API rate limit과 재현성 때문에, 조회 결과를 저장해 두고
  전처리는 항상 캐시에서 읽는다 (수집과 전처리의 분리)
- 저장 포맷은 parquet 우선 검토 (백로그 C 참조)
- 구 `read_data()`의 역할 축소: CSV 파싱/정제가 사라지고 "캐시 로드 + 표준 스키마 검증"만 남는다

## 7. 구 파이프라인 대비 변경 요약

| 구 (Fractal) | 신 (pivot) |
|---|---|
| HTS 수동 CSV 다운로드 | broker-modules SDK 자동 조회 |
| 한국투자 HTS 포맷 고정 | 브로커 중립 (`ChartBar` → 표준 스키마) |
| 콤마 제거/타입 변환/역순 정렬 파싱 | 불필요 (SDK가 파싱 완료된 dataclass 반환) |
| 분봉 연도 추정 핵 (백로그 A8) | 불필요 — SDK가 완전한 timestamp 제공 → **A8 해소** |
| 이평선 5/20/120 HTS 제공값 사용 | 프리셋/차트 요청 기간 기준 직접 계산 |
| `env.yaml` 자격증명 | 환경변수 (`Credentials.from_env()`) |
| 원시가 (수정주가 여부 불명확) | `adjusted=True` 수정주가 명시 |
