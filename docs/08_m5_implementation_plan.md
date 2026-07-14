# M5 Kiwoom WebSocket 실시간 추론 구현 계획

## 1. 목적과 완료 기준

M5는 Kiwoom WebSocket의 국내·미국주식 실시간 체결을 FastAPI가 수신하고, 선택된 M4
체크포인트와 동일한 전처리 계약으로 봉 마감 시 추론한 결과를 Live 탭에 전달하는 단계다.
브라우저는 Kiwoom에 직접 연결하지 않으며 서버의 `/ws/live`만 구독한다.

완료 기준은 다음과 같다.

1. Training의 성공한 run에서 검증된 `best_checkpoint` 하나를 실시간 모델로 지정한다.
2. 국내·미국 종목마스터 검색에서 선택한 종목을 구독하면 서버가 Kiwoom 국내 `주식체결(0B)`
   또는 미국 `해외주식체결(FE)`을 수신한다.
3. 선택 모델의 timeframe에 맞춰 현재 봉을 갱신하고 봉 마감 이벤트를 한 번만 생성한다.
4. 봉 마감 시 학습과 같은 피처 순서·이평 계산·`sample_standard_v1` 변환으로 추론한다.
5. Live 탭에서 연결 상태, 구독 종목, 현재 봉, 저점/고점/무시 점수와 최근 판정 로그를 본다.
6. Kiwoom 연결이 끊기면 SDK가 재접속·재구독하고, 서버는 시작·idle 복귀·주기적 REST
   reconcile로 누락 구간을 보정한 뒤 추론을 재개한다.
7. API 키, 접근토큰, private artifact 경로는 브라우저 응답과 로그에 노출되지 않는다.

## 2. 확정된 Kiwoom SDK 계약

M5의 증권사 실시간 기반은 `brokers.kiwoom` 하나로 고정한다. KIS WebSocket fallback이나
브로커 자동 전환은 M5 범위에 포함하지 않는다.

```python
async with KiwoomClient.from_env() as client:
    async with client.realtime.session(market="KRX") as krx_ws:
        async with client.realtime.session(market="US") as us_ws:
            await krx_ws.subscribe_trades("005930")
            await us_ws.subscribe_us_trades("AAPL", exchange="ND")
```

- 채널: 국내 `주식체결(0B)`과 미국 `해외주식체결(FE)`을 사용한다. 호가잔량·업종지수는 구독하지 않는다.
- 반환 모델: `RealtimeTick`. 사용 필드는 `symbol`, `exchange_ts`, `received_at`,
  `received_seq`, `price`, `volume`, `amount`, `change`, `change_rate`, `total_volume`이다.
- SDK 책임: OAuth 토큰, WebSocket LOGIN, PING echo, 연결 재시도, 활성 구독 복원,
  Kiwoom 부호 가격의 절댓값 정규화.
- Pivot 책임: 종목·지역·거래소 구독 상태, 이벤트 검증, 거래소 거래일 결합, 봉 집계, 누락 보정, 모델 추론,
  브라우저 fan-out.
- `received_seq`는 단일 세션 수신 순서다. 같은 수신 시각의 tie-breaker로만 사용하며
  재접속 전후의 전역 순번으로 해석하지 않는다.
- `price` 또는 `volume`이 없는 이벤트는 집계에서 제외하고 상태 로그에 계수한다.

## 3. 실행 구조와 소유권

```text
Kiwoom WebSocket KRX 0B / US FE
  -> server/live.py                # SDK session, 구독 registry, REST reconcile
  -> pivot/realtime/aggregate.py   # RealtimeTrade -> day/min/tick CandleUpdate/CandleClosed
  -> pivot/realtime/infer.py       # 체크포인트 + 공용 transform으로 후보 시퀀스 추론
  -> server/routers/live.py        # model/subscription/state HTTP API
  -> /ws/live                      # 브라우저 read-only event stream
  -> web/src/pages/Live.tsx        # 차트, 연결/모델/구독/판정 UI
```

- `pivot/realtime/`은 브로커 SDK나 FastAPI를 import하지 않는다. 서버가 `RealtimeTick`을
  자체 `RealtimeTrade` 도메인 값으로 변환해 전달한다.
- Kiwoom client는 FastAPI lifespan에서 하나만 유지하고, 구독 시장별로 KRX/US session을 하나씩
  유지한다. 종목마다 WebSocket을 만들지 않는다.
- lifespan 종료 시 브라우저 listener를 먼저 닫고 Kiwoom session의 WebSocket close를
  완료한 뒤 gateway/maintenance 태스크를 취소·대기한다. `Ctrl+C`를 포함한 정상 종료는
  열린 소켓을 정리한 후 Uvicorn 프로세스를 끝낸다.
- 여러 브라우저가 열려도 Kiwoom 구독은 종목당 하나이며, 서버가 각 브라우저 큐로 fan-out한다.
- 느린 브라우저는 제한된 큐에서 오래된 `candle_update`를 최신 값으로 합치고,
  `candle_closed`와 `prediction`은 버리지 않는다.
- 실시간 구독 종목은 로컬 운영 상태 `data/meta/live_subscriptions.json`에 저장한다.
- 활성 모델 지정은 검증된 artifact와 연결되는 학습 배포 메타데이터이므로 Supabase에
  단일 활성 deployment로 저장한다. API 응답에는 artifact object path를 제외한다.
- 최근 판정 로그는 서버 ring buffer와 브라우저 `localStorage`를 병합해 구독 종목별 최대
  200건을 유지한다. 페이지 새로고침에는 보존하고 구독 해제 시 해당 종목 로그를 제거한다.
  이는 표시 전용이며 추론 상태나 장기 성과 저장·백테스트를 대체하지 않는다.

## 4. 봉 집계 계약

모든 집계는 `exchange_ts`를 거래소 현지 거래일과 결합해 처리한다. 국내는 Asia/Seoul,
미국은 America/New_York에서 봉 경계와 거래일을 계산한다. 미국 분봉·틱봉은 응답 직전에 KST로
변환하고, 일봉은 정규장 종료가 속하는 KST 날짜로 변환해 국내 차트와 같은 축으로 표시한다.
미국 FE와 REST 분봉·틱봉 중 데이마켓(20:00~익일 04:00 ET)은 집계·추론에서 제외한다.
가격은 `Decimal`에서 표준 candle 숫자로 변환하고,
거래량은 체결량의 절댓값을 합산한다.

| 모델 timeframe | 마감 규칙 |
|---|---|
| `min{N}` | 거래소 현지 장중 시각을 N분 경계로 내림해 집계하고 다음 경계의 첫 체결에서 이전 봉 마감 |
| `tick{N}` | 유효한 `RealtimeTick` N개를 수신 순서대로 집계한 뒤 N번째 체결에서 마감 |
| `day` | 거래일 단위로 집계하고 정규장 종료 시 마감. 장중 값은 잠정 봉으로만 표시 |

- OHLC는 첫 가격/최고/최저/마지막 가격, Volume은 체결량 합, Amount는
  `price * volume` 합으로 계산한다. `RealtimeTick.amount`는 누적 거래대금일 수 있으므로
  봉 안에서 직접 합산하지 않고 단조성/누락 보정 검증에만 사용한다.
- 동일 봉 안에서는 `exchange_ts, received_seq`로 처리 순서를 고정한다. `received_at`은 연결
  상태와 지연 관측에만 사용한다.
- 이미 마감한 시각보다 오래된 체결은 현재 봉을 되감지 않고 late-event 계수에 기록한다.
- 구독·재접속·모델 교체가 분 중간에 시작되면 첫 분봉은 부분 봉으로 간주해 REST 현재 봉을
  덮어쓰지 않는다. 다음 분 경계부터 완전한 WebSocket 봉을 표시·추론한다.
- Live 화면의 과거 차트는 Kiwoom REST에서 직접 조회하고, 아직 마감하지 않은 봉은 메모리
  overlay로 유지한다. 최초 `min1` 조회는 당일 분봉만 표시하고, 좌측 끝 접근 시 이전 7일
  구간을 반복 조회한다. 이 화면 조회는 raw parquet를 읽거나 수정하지 않는다.
- 활성 모델 timeframe의 Live 이력은 checkpoint의 프리셋 스냅샷으로 계산한
  `fractal_markers`를 함께 반환한다. 실제 프랙탈은 `H/L` 화살표, 모델 판정은 `판정 H/L`
  원형 마커로 구분한다. class `2`는 최근 판정 로그에서만 확인한다.
- 앱 시작·모델 교체·장시간 idle 뒤 첫 체결 및 주기적 reconcile 시 기존 Kiwoom REST 수집
  경로로 마지막 마감 봉 이후를 갱신한다. SDK가 내부 재접속 경계를 외부에 노출하지 않으므로
  gap 보정은 재접속 callback에 의존하지 않는다. REST 캐시와 메모리 overlay를 timestamp 기준
  병합한 뒤 중복 없는 다음 봉부터 추론한다.

## 5. 추론 계약과 현재 한계

- 활성 artifact를 다운로드하고 SHA-256을 검증한 뒤 모델을 로드한다.
- run snapshot의 model type, feature columns, timeframe, scaling, padding, label mapping과
  `labeling.sample_pairing`을 런타임 계약으로 사용한다. UI 입력으로 이를 덮어쓰지 않는다.
  서버/repository 계층은 저장 snapshot을 공통 `resolve_stored_preset()`으로 hydrate하며,
  pairing 필드가 없으면 `latest_opposite_v1`로 읽는다. `pivot/realtime/`에는 materialize된
  도메인 설정만 전달해 저장소 의존성을 만들지 않는다.
- 학습 데이터셋의 `datasets.timeframe`이 `mixed`여도 활성화할 수 있다. 이때 실시간 엔진은
  checkpoint의 프리셋 기본 `timeframe`과 그 타임프레임에 대응하는 fractal 설정을 사용한다.
  공개 deployment에도 실제 엔진 타임프레임을 반환하며 `mixed`를 봉 집계 코드로 노출하지 않는다.
- 각 마감 봉 뒤 캐시+overlay에 필요한 이동평균을 다시 계산한다. 피처에 NaN/무한값이 있거나
  history가 부족하면 추론하지 않고 `warmup` 상태를 보낸다.
- 새로 확정된 프랙탈은 `(n-1)//2` lag를 적용해 과거 시점에 기록한다. 후보 윈도우는 활성
  run snapshot의 pairing 전략에 따라 학습과 동일하게 구성하고 `sample_standardize`를 적용한다.
  - `latest_opposite_v1`: 현재 계약처럼 고점 후보는 최근 확정 저점, 저점 후보는 최근 확정
    고점을 anchor로 삼아 두 candidate window를 독립 구성한다. 최종 후보 점수는 저점 window의
    class 0, 고점 window의 class 1, 두 window 중 큰 class 2 점수를 사용하며 세 값 중 최댓값을
    `selected_class`로 선택한다. 이는 합이 1인 단일 확률분포가 아니라 독립 후보 점수다.
  - `adjacent_markers_v1`: 종류와 무관하게 시간상 최신 retained marker 하나를 anchor로 삼아
    `latest retained marker .. current closed bar` 단일 shared window를 구성하고 추론도 한 번만
    수행한다. low/high는 서로 다른 window가 아니라 동일 출력의 class `0/1` score다.
  - adjacent에서 incoming label `2`라 `cls2_drop`으로 샘플이 제외됐던 marker도 다음 후보의
    anchor로 유지한다. 프랙탈 탐지·plateau·point filter·클리닝 경계는 학습과 동일해야 한다.
- 모델 호출은 event loop를 막지 않도록 bounded executor에서 직렬화한다. 같은
  `(symbol, timeframe, closed_time, deployment_id)` 추론은 멱등 처리한다.
- 서버의 `prediction_threshold` 기본값은 `0.7`이다. 선택 클래스가 `0` 또는 `1`이고
  `scores[selected_class] >= prediction_threshold`인 판정은 각각 예측 저점·고점으로 저장하고,
  다음 추론부터 같은 종류의 계산 프랙탈보다 최신이면 후보 anchor로 사용한다. class `2`는
  차트 포인트나 anchor가 되지 않는다. 임계값 변경 시 최근 판정 로그는 유지하고, 엔진이 보유한
  종목별 판정 이력에서 새 기준을 통과하는 최신 저점·고점을 다시 선택해 표시와 anchor 기준을 맞춘다.

현재 cls3 데이터셋은 실제 프랙탈 지점만 샘플링하며 “프랙탈 아님” 음성 샘플을 포함하지 않는다.
따라서 M5의 출력은 **현재 봉이 프랙탈일 절대 확률이 아니라, 프랙탈 후보 시퀀스라는 조건에서의
클래스 점수**다. Live UI는 이를 `실험적 후보 점수`로 표시하고 자동매매 신호로 표현하지 않는다.
비프랙탈 음성 클래스/threshold calibration을 검증하기 전에는 주문 기능을 추가하지 않는다.

## 6. HTTP·WebSocket 계약

### 6.1 HTTP

| Method | Path | 계약 |
|---|---|---|
| GET | `/api/live/state` | `{connection, deployment, prediction_threshold, subscriptions, counters}` |
| PUT | `/api/live/model` | `{run_id, artifact_id?}`. 활성화 후 갱신된 state 전체 반환 |
| DELETE | `/api/live/model` | 현재 활성 deployment를 비활성화하고 갱신된 state 전체 반환 |
| PUT | `/api/live/prediction-threshold` | `{threshold}` (`0..1`). 예측 anchor를 초기화하고 갱신된 state 전체 반환 |
| GET | `/api/live/subscriptions` | 저장된 구독 종목과 종목별 상태 |
| POST | `/api/live/subscriptions` | `{symbol,name,region,exchange}`. 구독 후 갱신된 구독 목록 반환 |
| DELETE | `/api/live/subscriptions/{symbol}` | 해제 후 갱신된 구독 목록 반환 |
| GET | `/api/live/history/{symbol}` | `timeframe=day|min1`, `ma`, `before`로 Kiwoom REST 과거 봉을 직접 페이지 조회. 로컬 parquet 미사용 |

모델 교체는 새 artifact 검증·모델 forward warmup 성공 후 원자적으로 활성 포인터를 바꾼다. 실패하면 기존
모델과 구독은 유지한다. 활성 모델이 없으면 구독은 저장할 수 있지만 추론 상태는 `no_model`이다.
모델 비활성화는 활성 deployment 이력을 삭제하지 않고 `active=false`로 전환한 뒤 메모리 추론 엔진과
최근 예측을 해제한다. 이후 API 재시작에서도 해당 모델을 복원하지 않는다.
`connection`은 `status/message/last_tick_at/last_heartbeat_at/market_state`, deployment는
공개 run·dataset·artifact id, model/timeframe/features/pairing만 포함한다. 구독 행은
`symbol/name/region/exchange`, 전송 상태
`pending|subscribed|error`와 추론 상태 `no_model|warmup|ready`를 분리한다. `counters`는 invalid,
reconcile/inference 오류, accepted/duplicate/late 합계를 숫자로 반환한다.

활성 모델이 있는 상태에서 종목을 구독하면 해당 timeframe의 Kiwoom REST 차트 보정을 즉시 시도해
로컬 parquet 캐시를 만들거나 갱신한다. 모델이 없으면 timeframe을 결정할 수 없으므로 구독만
저장하고, 이후 모델 활성화 시 전체 구독 종목을 보정한다. `best_checkpoint` artifact 행은 Storage
재다운로드 SHA-256 검증에 성공한 뒤에만 생성되며, 활성화 시 다시 검증한다. 별도 `verified` 응답
필드는 두지 않는다.

Live 화면 이력과 추론 이력은 목적을 분리한다. 화면은 `/api/live/history`로 Kiwoom REST를 직접
조회하며 결과를 로컬 캐시에 저장하지 않는다. 추론은 재현 가능한 기존 parquet와 메모리 overlay를
병합하고, reconcile만 기존 캐시를 갱신한다. 분봉 응답은 당일을 최초 페이지로 하고 `before`마다
직전 7일 구간, 일봉은 365일 구간을 반환한다. MA 계산에 필요한 선행 봉은 내부 조회에만 포함하고
화면 응답에서는 해당 페이지 범위로 다시 자른다. 최초 일봉 조회에 당일 봉이 있으면 그 OHLCV를
실시간 일봉 집계기의 현재 봉으로 주입하고, 이후 수신 체결은 이 기준값에 누적한다. 따라서 구독
시점 이후 체결만으로 당일 일봉을 다시 만들지 않는다.
Live 탭은 최초 진입 후 탭 전환에도 마운트를 유지한다. 선택 종목·REST 이력과 브라우저 WebSocket
연결은 다른 탭을 보는 동안 보존하며, Live 탭 재진입만으로 재조회·재연결하지 않는다.
앱의 활성 탭 ID도 `localStorage`에 저장해 페이지 새로고침 후 직전 탭을 복원한다.
페이지 새로고침 시 브라우저와 FastAPI 사이의 `/ws/live` 연결은 새로 생성되지만, FastAPI
lifespan이 소유한 Kiwoom 세션과 gateway task는 브라우저 listener 수명과 독립적으로 유지한다.
브라우저가 연결되지 않은 동안에도 서버는 체결 수신·봉 집계·추론을 계속하며, 새 브라우저
연결은 첫 `snapshot`에서 그 사이의 최신 상태를 복원한다.

### 6.2 `/ws/live` 서버 이벤트

모든 메시지는 `{type, sequence, emitted_at, data}` envelope을 사용한다.

| type | data 핵심 필드 |
|---|---|
| `snapshot` | HTTP state + `latest_candles` 배열 + `recent_predictions` 배열 |
| `connection` | `connecting | connected | reconnecting | stale | closed`, message |
| `subscription` | symbol, name, region, exchange, status, inference_status, error, last_tick_at |
| `candle_update` | symbol, timeframe, candle, provisional=true |
| `candle_closed` | symbol, timeframe, candle, provisional=false |
| `prediction` | symbol, timeframe, time, scores, selected_class, candidate_windows(anchor_source/confidence 포함), deployment_id |
| `warmup` | symbol, required_bars, available_bars, reason |
| `heartbeat` | server_time, market_state, last_tick_at |
| `error` | scope, symbol?, recoverable, message |

WebSocket은 read-only다. 모델·구독 변경은 HTTP로만 수행해 재시도와 오류 상태를 명확히 한다.
브라우저 재연결 시 첫 메시지 `snapshot`으로 전체 상태를 복원하며 sequence gap이 있으면
이전 delta를 재생하지 않고 최신 snapshot을 사용한다.

`sequence`는 서버 프로세스에서 단조 증가하며 연결마다 첫 snapshot 이후 수신 순서도 단조 증가한다.
새 연결에서 1로 초기화되는 값은 아니며, 느린 클라이언트의 update 병합 때문에 중간 번호가 빠질 수
있다. 클라이언트는 값의 연속성 대신 첫 snapshot 여부와 역행·중복 여부만 검사한다.

`candle_update`, `candle_closed`, `snapshot.latest_candles`의 candle은
`{time, open, high, low, close, volume}`이고, time은 `/api/chart`와 동일하게 일봉은 `yyyy-mm-dd`,
분봉·틱봉은 KST 벽시계를 나타내는 unix 초를 사용한다. 미국 REST 이력과 FE 실시간 봉도
America/New_York에서 KST로 변환한 뒤 같은 형식으로 내리며, 미국 일봉 날짜는 KST 정규장
종료일을 사용한다. `prediction.time`과 candidate window의 `anchor_time/start/end`도 같은
거래소→KST 변환을 적용해 차트 candle과 정확히 같은 time key를 사용한다. prediction의
`scores`는 고정 순서
`[class0_low, class1_high, class2_ignore]`인 길이 3 배열이다. snapshot의 최근 prediction 항목은
일반 `prediction` 이벤트의 data와 동일하다.

`prediction.data.candidate_windows`의 각 항목은 `pairing_rule`, `anchor_position`,
`anchor_time`, `anchor_kind`, `start`, `end`, `shared_window`를 포함한다. legacy 두 후보는
`shared_window=false`, adjacent 단일 후보는 `shared_window=true`다.

## 7. 구현 순서

1. **M5-A 도메인 기반**: trade/candle 타입, 분·틱·일봉 집계, late/duplicate 처리와 결정적 테스트.
2. **M5-B 추론 기반**: artifact 검증 로더, 공용 transform, snapshot pairing hydration,
   legacy 두 후보/adjacent 단일 shared 후보, warmup·멱등 추론 테스트.
3. **M5-C Kiwoom gateway**: FastAPI lifespan 시장별 KRX/US session, 동적 subscribe/unsubscribe,
   자동 재접속 후 REST gap 보정, bounded fan-out.
4. **M5-D API·저장**: active deployment migration/repository, subscription JSON, HTTP·WS 계약.
5. **M5-E Live UI**: 기존 CandleChart 재사용, 연결/모델/구독 상태, 현재 봉과 후보 점수·로그.
6. **M5-F 통합 검증**: recorded tick replay로 장외 결정적 E2E 후 장중 005930 실측.

UI와 core를 병렬 구현할 경우 이 문서의 §6을 먼저 main에 커밋하고, M4와 동일하게 별도
worktree를 사용한다. core는 `pivot/**`, `server/**`, migration/tests를 소유하고 UI는
`web/src/pages/Live*`, `web/src/features/live/**`, `web/src/api/live.ts`만 소유한다.

## 8. 검증 기준

- 단위: 분 경계, tick N개, 일자 전환, OHLCV/Amount, late event, 같은 초 다중 체결.
- 회귀: recorded event를 두 번 재생해 동일 candle/prediction sequence가 생성된다.
- 추론: training loader와 live 입력의 feature 순서·표준화 결과가 byte 허용오차 내 동일하다.
- pairing: snapshot 누락/`latest_opposite_v1`은 최근 low/high별 두 후보, `adjacent_markers_v1`은
  시간상 최신 retained marker부터 현재 봉까지 단일 shared 후보를 만든다. label-2 incoming
  sample이 제외된 marker도 adjacent anchor로 유지하며 event의 rule/anchor/start/end가 일치한다.
- 복구: WebSocket 강제 단절 후 구독 복원, REST gap 보정, 중복 prediction 0건.
- 보안: 브라우저 payload/log에 Kiwoom key/token과 Supabase secret/object path가 없다.
- 부하: 구독 종목 수 범위에서 이벤트 루프가 차단되지 않고 느린 브라우저가 수집을 막지 않는다.
- 브라우저: 1600x1000과 1280x800에서 차트·상태·로그가 겹치지 않고 콘솔 오류가 없다.
- 실측: 장중 005930의 0B와 미국 정규장 AAPL의 FE에서 현재 봉 update, 실제 봉 마감,
  KST 표시, 추론, 재접속을 각각 확인한다.

장외에는 recorded tick fixture로 모든 완료 기준을 검증할 수 있어야 한다. 장중 실측은 별도
최종 조건이며, 시장이 닫혀 있다는 이유로 결정적 테스트를 생략하지 않는다.

## 9. 현재 구현 상태 (2026-07-14)

M5-A~E의 core와 Live UI 통합은 완료했다. core는 실시간 봉 집계, 학습 snapshot 기반 추론,
Kiwoom KRX/US 시장별 session, REST cache reconcile, 단일 활성 deployment, HTTP/WS fan-out을 제공한다.
Live UI는 모델 선택, 종목 구독, Kiwoom REST 과거+실시간 candle 표시, warmup/연결 상태와 최근 prediction을
§6 계약으로 소비한다. `snapshot`은 HTTP state 전체(`counters` 포함)를 먼저 전달하며 이후
sequence가 증가하는 delta를 보낸다.
차트 표시 timeframe은 모델 추론 timeframe과 분리해 `day`와 `min1` 중 선택한다. 서버는 두
표시 timeframe을 항상 집계하되 prediction은 활성 모델의 timeframe에서만 생성한다. 차트는
종목 & 데이터 탭과 같은 캔들·거래량·이동평균선·크로스헤어 OHLC를 사용하되, 이력은 로컬 캐시가
아닌 `/api/live/history`에서 읽고 좌측 끝 접근 시 이전 구간을 추가 로딩한다.
모델 판정은 해당 캔들에 `판정 L/H`와 선택 클래스 확률을 마커로 표시한다. 서버 임계값은
기본 70%이며 `scores[selected_class]`가 임계값 이상인 판정만 차트에 표시한다. class 0/1
표시 포인트는 다음 추론의 예측 anchor로 승격하고 class 2는 로그에만 남긴다. 임계값 입력은 `%`
단위의 숫자·소수점 text field이며 적용 시 서버 확률값으로 변환한다.
실시간 구독 추가는 관심종목 목록에 의존하지 않고 국내·미국 종목마스터 퍼지 검색을 직접 사용한다.
자동완성에서 선택된 종목코드와 이름, 지역, 거래소를 구독 API로 전달한다. 국내 0B는 KST,
미국 FE는 America/New_York에서 집계하고 미국 REST/WS 분봉은 차트 응답에서 KST로 통일한다.

로컬 검증은 Python 전체 테스트, Ruff, frontend lint와 production build까지 통과했다. 통합
브라우저 smoke에서도 Live 탭 렌더, HTTP 오류 0건, 콘솔 오류·경고 0건, `/ws/live` 첫 snapshot과
1600x1000·1280x800 overflow 0건을 확인했다.

원격 Supabase에는 `20260713100000_live_deployments.sql`을 `20260713111114` 버전으로 적용했다.
RLS 활성화, `anon`·`authenticated` 접근 차단, `service_role` 접근을 확인했다. 장 마감 상태에서
005930 구독은 `pending → subscribed`, 추론은 `no_model`로 전이했고, 로컬 일봉 캐시 차트와 거래량을
Live 화면에 표시했다. 페이지를 두 번 새로 연결했을 때 각 `/ws/live` 연결의 첫 프레임이 모두
현재 구독을 포함한 `snapshot`이었으며 콘솔 오류·경고는 없었다. 실패한 run 활성화 요청은 422를
반환하고 deployment 0건을 유지했다.

현재 저장된 run은 취소 상태이고 `best_checkpoint` artifact가 없어 활성화 성공 경로는 실행할 수
없다. M5 완료 표시 전 남은 운영 조건은 다음 두 가지다.

1. succeeded run과 검증된 `best_checkpoint`를 보존한 뒤 모델 활성화·REST reconcile·차트 overlay를
   브라우저에서 확인한다.
2. 장중 005930으로 실제 `0B` 수신, 봉 마감 추론, 강제 재접속 후 REST 보정을 실측한다.
3. 미국 정규장 AAPL로 실제 `FE` 수신, KST 분봉 표시, 봉 마감과 강제 재접속을 실측한다.
