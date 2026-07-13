# M5 Kiwoom WebSocket 실시간 추론 구현 계획

## 1. 목적과 완료 기준

M5는 Kiwoom WebSocket의 국내주식 실시간 체결을 FastAPI가 수신하고, 선택된 M4
체크포인트와 동일한 전처리 계약으로 봉 마감 시 추론한 결과를 Live 탭에 전달하는 단계다.
브라우저는 Kiwoom에 직접 연결하지 않으며 서버의 `/ws/live`만 구독한다.

완료 기준은 다음과 같다.

1. Training의 성공한 run에서 검증된 `best_checkpoint` 하나를 실시간 모델로 지정한다.
2. 관심종목을 실시간 구독하면 서버가 Kiwoom `주식체결(0B)`을 수신한다.
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
    async with client.realtime.session() as ws:
        await ws.subscribe_trades("005930")
        async for event in ws.stream():
            ...
```

- 채널: 국내주식 `주식체결(0B)`만 사용한다. 호가잔량 `0D`와 업종지수 `0J`는 구독하지 않는다.
- 반환 모델: `RealtimeTick`. 사용 필드는 `symbol`, `exchange_ts`, `received_at`,
  `received_seq`, `price`, `volume`, `amount`, `change`, `change_rate`, `total_volume`이다.
- SDK 책임: OAuth 토큰, WebSocket LOGIN, PING echo, 연결 재시도, 활성 구독 복원,
  Kiwoom 부호 가격의 절댓값 정규화.
- Pivot 책임: 종목 구독 상태, 이벤트 검증, KST 거래일 결합, 봉 집계, 누락 보정, 모델 추론,
  브라우저 fan-out.
- `received_seq`는 단일 세션 수신 순서다. 같은 수신 시각의 tie-breaker로만 사용하며
  재접속 전후의 전역 순번으로 해석하지 않는다.
- `price` 또는 `volume`이 없는 이벤트는 집계에서 제외하고 상태 로그에 계수한다.

## 3. 실행 구조와 소유권

```text
Kiwoom WebSocket 0B
  -> server/live.py                # SDK session, 구독 registry, REST reconcile
  -> pivot/realtime/aggregate.py   # RealtimeTrade -> day/min/tick CandleUpdate/CandleClosed
  -> pivot/realtime/infer.py       # 체크포인트 + 공용 transform으로 후보 시퀀스 추론
  -> server/routers/live.py        # model/subscription/state HTTP API
  -> /ws/live                      # 브라우저 read-only event stream
  -> web/src/pages/Live.tsx        # 차트, 연결/모델/구독/판정 UI
```

- `pivot/realtime/`은 브로커 SDK나 FastAPI를 import하지 않는다. 서버가 `RealtimeTick`을
  자체 `RealtimeTrade` 도메인 값으로 변환해 전달한다.
- Kiwoom client/session은 FastAPI lifespan에서 하나만 유지한다. 종목마다 WebSocket을 만들지 않는다.
- 여러 브라우저가 열려도 Kiwoom 구독은 종목당 하나이며, 서버가 각 브라우저 큐로 fan-out한다.
- 느린 브라우저는 제한된 큐에서 오래된 `candle_update`를 최신 값으로 합치고,
  `candle_closed`와 `prediction`은 버리지 않는다.
- 실시간 구독 종목은 로컬 운영 상태 `data/meta/live_subscriptions.json`에 저장한다.
- 활성 모델 지정은 검증된 artifact와 연결되는 학습 배포 메타데이터이므로 Supabase에
  단일 활성 deployment로 저장한다. API 응답에는 artifact object path를 제외한다.
- 최근 판정 로그는 M5에서 프로세스 메모리의 제한된 ring buffer다. 장기 성과 저장·백테스트는
  별도 마일스톤으로 둔다.

## 4. 봉 집계 계약

모든 집계는 `exchange_ts`를 Asia/Seoul 거래일과 결합해 처리한다. 가격은 `Decimal`에서
표준 candle 숫자로 변환하고, 거래량은 체결량의 절댓값을 합산한다.

| 모델 timeframe | 마감 규칙 |
|---|---|
| `min{N}` | KST 장중 시각을 N분 경계로 내림해 집계하고 다음 경계의 첫 체결에서 이전 봉 마감 |
| `tick{N}` | 유효한 `RealtimeTick` N개를 수신 순서대로 집계한 뒤 N번째 체결에서 마감 |
| `day` | 거래일 단위로 집계하고 정규장 종료 시 마감. 장중 값은 잠정 봉으로만 표시 |

- OHLC는 첫 가격/최고/최저/마지막 가격, Volume은 체결량 합, Amount는
  `price * volume` 합으로 계산한다. `RealtimeTick.amount`는 누적 거래대금일 수 있으므로
  봉 안에서 직접 합산하지 않고 단조성/누락 보정 검증에만 사용한다.
- 동일 봉 안에서는 `received_at, received_seq`로 처리 순서를 고정한다.
- 이미 마감한 시각보다 오래된 체결은 현재 봉을 되감지 않고 late-event 계수에 기록한다.
- 과거 차트는 로컬 parquet에서 읽고, 아직 마감하지 않은 봉은 메모리 overlay로만 유지한다.
  실시간 수신기가 raw parquet를 직접 수정하지 않는다.
- 앱 시작·모델 교체·장시간 idle 뒤 첫 체결 및 주기적 reconcile 시 기존 Kiwoom REST 수집
  경로로 마지막 마감 봉 이후를 갱신한다. SDK가 내부 재접속 경계를 외부에 노출하지 않으므로
  gap 보정은 재접속 callback에 의존하지 않는다. REST 캐시와 메모리 overlay를 timestamp 기준
  병합한 뒤 중복 없는 다음 봉부터 추론한다.

## 5. 추론 계약과 현재 한계

- 활성 artifact를 다운로드하고 SHA-256을 검증한 뒤 모델을 로드한다.
- run snapshot의 model type, feature columns, timeframe, scaling, padding, label mapping을
  런타임 계약으로 사용한다. UI 입력으로 이를 덮어쓰지 않는다.
- 각 마감 봉 뒤 캐시+overlay에 필요한 이동평균을 다시 계산한다. 피처에 NaN/무한값이 있거나
  history가 부족하면 추론하지 않고 `warmup` 상태를 보낸다.
- 새로 확정된 프랙탈은 `(n-1)//2` lag를 적용해 과거 시점에 기록한다. 현재 봉 후보는
  최근 확정 저점부터의 고점 후보 시퀀스와 최근 확정 고점부터의 저점 후보 시퀀스를 각각
  구성하고, 학습과 같은 `sample_standardize`를 적용한다.
- 모델 호출은 event loop를 막지 않도록 bounded executor에서 직렬화한다. 같은
  `(symbol, timeframe, closed_time, deployment_id)` 추론은 멱등 처리한다.

현재 cls3 데이터셋은 실제 프랙탈 지점만 샘플링하며 “프랙탈 아님” 음성 샘플을 포함하지 않는다.
따라서 M5의 출력은 **현재 봉이 프랙탈일 절대 확률이 아니라, 프랙탈 후보 시퀀스라는 조건에서의
클래스 점수**다. Live UI는 이를 `실험적 후보 점수`로 표시하고 자동매매 신호로 표현하지 않는다.
비프랙탈 음성 클래스/threshold calibration을 검증하기 전에는 주문 기능을 추가하지 않는다.

## 6. HTTP·WebSocket 계약

### 6.1 HTTP

| Method | Path | 계약 |
|---|---|---|
| GET | `/api/live/state` | 연결 상태, 활성 모델, timeframe, 구독 목록, 집계/오류 계수 |
| PUT | `/api/live/model` | `{run_id, artifact_id?}`. succeeded run의 검증된 best artifact만 활성화 |
| GET | `/api/live/subscriptions` | 저장된 구독 종목과 종목별 상태 |
| POST | `/api/live/subscriptions` | `{symbol}`. 활성 모델 timeframe으로 Kiwoom `0B` 구독 |
| DELETE | `/api/live/subscriptions/{symbol}` | Kiwoom 구독 해제 후 로컬 상태에서 제거 |

모델 교체는 새 artifact 검증·warmup 성공 후 원자적으로 활성 포인터를 바꾼다. 실패하면 기존
모델과 구독은 유지한다. 활성 모델이 없으면 구독은 저장할 수 있지만 추론 상태는 `no_model`이다.

### 6.2 `/ws/live` 서버 이벤트

모든 메시지는 `{type, sequence, emitted_at, data}` envelope을 사용한다.

| type | data 핵심 필드 |
|---|---|
| `snapshot` | connection, deployment, subscriptions, latest_candles, recent_predictions |
| `connection` | `connecting | connected | reconnecting | stale | closed`, message |
| `subscription` | symbol, status, error |
| `candle_update` | symbol, timeframe, candle, provisional=true |
| `candle_closed` | symbol, timeframe, candle, provisional=false |
| `prediction` | symbol, timeframe, time, scores, selected_class, candidate_windows, deployment_id |
| `warmup` | symbol, required_bars, available_bars, reason |
| `heartbeat` | server_time, market_state, last_tick_at |
| `error` | scope, symbol?, recoverable, message |

WebSocket은 read-only다. 모델·구독 변경은 HTTP로만 수행해 재시도와 오류 상태를 명확히 한다.
브라우저 재연결 시 첫 메시지 `snapshot`으로 전체 상태를 복원하며 sequence gap이 있으면
이전 delta를 재생하지 않고 최신 snapshot을 사용한다.

## 7. 구현 순서

1. **M5-A 도메인 기반**: trade/candle 타입, 분·틱·일봉 집계, late/duplicate 처리와 결정적 테스트.
2. **M5-B 추론 기반**: artifact 검증 로더, 공용 transform, warmup·후보 시퀀스·멱등 추론 테스트.
3. **M5-C Kiwoom gateway**: FastAPI lifespan 단일 session, 동적 subscribe/unsubscribe,
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
- 복구: WebSocket 강제 단절 후 구독 복원, REST gap 보정, 중복 prediction 0건.
- 보안: 브라우저 payload/log에 Kiwoom key/token과 Supabase secret/object path가 없다.
- 부하: 구독 종목 수 범위에서 이벤트 루프가 차단되지 않고 느린 브라우저가 수집을 막지 않는다.
- 브라우저: 1600x1000과 1280x800에서 차트·상태·로그가 겹치지 않고 콘솔 오류가 없다.
- 실측: 장중 005930에서 0B 수신, 현재 봉 update, 실제 봉 마감, 추론, 재접속을 각각 확인한다.

장외에는 recorded tick fixture로 모든 완료 기준을 검증할 수 있어야 한다. 장중 실측은 별도
최종 조건이며, 시장이 닫혀 있다는 이유로 결정적 테스트를 생략하지 않는다.
