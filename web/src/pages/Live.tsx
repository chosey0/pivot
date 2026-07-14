import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  type ChartResponse,
  type TimeframeCode,
  type WatchItem,
} from '../api/client'
import {
  liveApi,
  type LiveConnectionStatus,
  type LiveHistoryResponse,
  type LiveSubscription,
} from '../api/live'
import {
  CandleChart,
  type ChartMarker,
  type OhlcPoint,
} from '../components/chart/CandleChart'
import { ChartPanel } from '../components/chart/ChartPanel'
import { IndicatorSettingsPanel } from '../components/indicators/IndicatorSettingsPanel'
import { useIndicatorSettings } from '../components/indicators/useIndicatorSettings'
import { ModelPanel } from '../features/live/ModelPanel'
import { PredictionLog } from '../features/live/PredictionLog'
import { formatEventTime } from '../features/live/time'
import {
  liveCandleKey,
  useLiveSocket,
  type SocketStatus,
} from '../features/live/useLiveSocket'
import { mergeChartPages } from '../lib/chart'
import {
  changeTone,
  formatDateTime,
  formatPercent,
  formatPrice,
  percentChange,
} from '../lib/format'
import './Live.css'

const CONNECTION_TEXT: Record<LiveConnectionStatus, string> = {
  connecting: '연결 중',
  connected: '연결됨',
  reconnecting: '재연결 중',
  stale: '지연',
  closed: '종료',
}

const SOCKET_TEXT: Record<SocketStatus, string> = {
  connecting: '연결 중',
  open: '실시간',
  reconnecting: '재연결 중',
  closed: '종료',
}

const SUBSCRIPTION_TEXT: Record<LiveSubscription['status'], string> = {
  pending: '대기',
  subscribed: '구독 중',
  error: '오류',
}

const INFERENCE_TEXT: Record<LiveSubscription['inference_status'], string> = {
  no_model: '모델 없음',
  warmup: '워밍업',
  ready: '추론 중',
}

function compareTimes(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

function mergeLiveHistory(
  current: LiveHistoryResponse,
  older: LiveHistoryResponse,
): LiveHistoryResponse {
  const chart = mergeChartPages(current, older)
  const fractalMarkers = new Map(
    [...older.fractal_markers, ...current.fractal_markers].map((row) => [
      `${row.time}:${row.kind}`,
      row,
    ]),
  )
  return { ...chart, fractal_markers: [...fractalMarkers.values()] }
}

export function Live() {
  const { state, socketStatus, applyState, applySubscriptions, dismissError } = useLiveSocket()
  const [stateError, setStateError] = useState<string | null>(null)
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [addSymbol, setAddSymbol] = useState('')
  const [mutating, setMutating] = useState(false)
  const [subscribeError, setSubscribeError] = useState<string | null>(null)
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null)
  const [chartTimeframe, setChartTimeframe] = useState<TimeframeCode>('day')
  const [chart, setChart] = useState<LiveHistoryResponse | null>(null)
  const [chartLoading, setChartLoading] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [chartError, setChartError] = useState<string | null>(null)
  const [chartMessage, setChartMessage] = useState<string | null>(null)
  const [selectedOhlc, setSelectedOhlc] = useState<OhlcPoint | null>(null)

  const indicators = useIndicatorSettings({ onMessage: setChartMessage })
  const {
    maSettings,
    setMaSettings,
    volumeChart,
    setVolumeChart,
    visibleIndicators,
    maWindows,
    legendText,
  } = indicators
  const maWindowsKey = maWindows.join(',')

  // WS snapshot 이전에도 화면을 채울 수 있도록 HTTP 상태를 한 번 읽는다
  useEffect(() => {
    let stale = false
    liveApi
      .state()
      .then((next) => {
        if (stale) return
        applyState(next)
        setStateError(null)
      })
      .catch((e: Error) => {
        if (!stale) setStateError(e.message)
      })
    api
      .watchlist()
      .then((items) => {
        if (!stale) setWatchlist(items.filter((item) => item.region === 'domestic'))
      })
      .catch(() => undefined) // 관심종목은 구독 추가 보조 UI라 실패해도 페이지는 동작한다
    return () => {
      stale = true
    }
  }, [applyState])

  // 구독 목록이 바뀌어도 선택 종목이 유효하게 유지되도록 보정한다
  useEffect(() => {
    setSelectedSymbol((current) => {
      if (current && state.subscriptions.some((row) => row.symbol === current)) return current
      return state.subscriptions[0]?.symbol ?? null
    })
  }, [state.subscriptions])

  // 과거 봉은 Live 전용 Kiwoom REST 조회로 읽는다. 재연결 snapshot마다 재조회해
  // 단절 동안의 마감 봉을 delta 재생 없이 복구한다 (docs/08 §6.2).
  useEffect(() => {
    if (!selectedSymbol) {
      setChart(null)
      setSelectedOhlc(null)
      return
    }
    let stale = false
    setChartLoading(true)
    setChartError(null)
    setChartMessage(null)
    liveApi
      .history(selectedSymbol, chartTimeframe, maWindows)
      .then((next) => {
        if (stale) return
        setChart(next)
        setSelectedOhlc(next.candles.at(-1) ?? null)
      })
      .catch((e: Error) => {
        if (stale) return
        setChart(null)
        setChartError(e.message)
      })
      .finally(() => {
        if (!stale) setChartLoading(false)
      })
    return () => {
      stale = true
    }
  }, [chartTimeframe, maWindows, maWindowsKey, selectedSymbol, state.snapshotNonce])

  const liveCandles = selectedSymbol
    ? state.candles[liveCandleKey(selectedSymbol, chartTimeframe)]
    : undefined
  const historicalChart = chart?.timeframe === chartTimeframe ? chart : null

  const merged = useMemo(() => {
    const candleByTime = new Map<string | number, ChartResponse['candles'][number]>()
    const volumeByTime = new Map<string | number, ChartResponse['volumes'][number]>()
    for (const candle of historicalChart?.candles ?? []) candleByTime.set(candle.time, candle)
    for (const point of historicalChart?.volumes ?? []) volumeByTime.set(point.time, point)
    for (const candle of liveCandles?.closed ?? []) {
      candleByTime.set(candle.time, candle)
      volumeByTime.set(candle.time, { time: candle.time, value: candle.volume })
    }
    const provisional = liveCandles?.provisional ?? null
    if (provisional) {
      // 잠정 봉이 이미 마감된 마지막 봉보다 오래됐으면 차트를 되감지 않는다
      const lastTime = [...candleByTime.keys()].reduce<string | number | null>(
        (max, time) => (max === null || compareTimes(time, max) > 0 ? time : max),
        null,
      )
      if (lastTime === null || compareTimes(provisional.time, lastTime) >= 0) {
        candleByTime.set(provisional.time, provisional)
        volumeByTime.set(provisional.time, { time: provisional.time, value: provisional.volume })
      }
    }
    const sortByTime = <T extends { time: string | number }>(rows: T[]) =>
      rows.sort((a, b) => compareTimes(a.time, b.time))
    return {
      candles: sortByTime([...candleByTime.values()]),
      volumes: sortByTime([...volumeByTime.values()]),
      ma: historicalChart?.ma ?? {},
    }
  }, [historicalChart, liveCandles])

  const loadOlderChart = useCallback(async () => {
    if (!historicalChart || !selectedSymbol || !historicalChart.has_more || loadingOlder) return
    const first = historicalChart.candles[0]
    if (!first) return
    setLoadingOlder(true)
    setChartError(null)
    try {
      const older = await liveApi.history(selectedSymbol, chartTimeframe, maWindows, {
        before: first.time,
      })
      setChart((current) => (current ? mergeLiveHistory(current, older) : older))
    } catch (e) {
      setChartError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingOlder(false)
    }
  }, [chartTimeframe, historicalChart, loadingOlder, maWindows, selectedSymbol])

  useEffect(() => {
    setSelectedOhlc((current) => {
      if (current && merged.candles.some((candle) => candle.time === current.time)) {
        return current
      }
      return merged.candles.at(-1) ?? null
    })
  }, [merged.candles])

  const displayedOhlc = selectedOhlc ?? merged.candles.at(-1) ?? null
  const displayedOhlcPreviousClose = useMemo(() => {
    if (!displayedOhlc) return null
    const index = merged.candles.findIndex((candle) => candle.time === displayedOhlc.time)
    return index > 0 ? merged.candles[index - 1].close : null
  }, [displayedOhlc, merged.candles])
  const ohlcItems = useMemo(() => {
    if (!displayedOhlc) return []
    return [
      { label: '시작', value: displayedOhlc.open },
      { label: '고가', value: displayedOhlc.high },
      { label: '저가', value: displayedOhlc.low },
      { label: '종가', value: displayedOhlc.close },
    ].map((item) => {
      const change = percentChange(item.value, displayedOhlcPreviousClose)
      return { ...item, change, tone: changeTone(change) }
    })
  }, [displayedOhlc, displayedOhlcPreviousClose])

  // 실제 프랙탈과 모델 판정을 같은 차트에 표시하되 모양과 문구를 구분한다.
  const markers = useMemo<ChartMarker[]>(() => {
    if (!selectedSymbol) return []
    const times = new Set(merged.candles.map((candle) => candle.time))
    const predictions = state.predictions.filter(
      (row) => row.symbol === selectedSymbol && row.timeframe === chartTimeframe,
    )
    const calculated = new Map<string, ChartMarker>()
    for (const row of predictions.flatMap((prediction) => prediction.candidate_windows)) {
      if (!times.has(row.anchor_time)) continue
      calculated.set(`${row.anchor_time}:${row.anchor_kind}`, {
        time: row.anchor_time,
        kind: row.anchor_kind,
        label: row.anchor_kind === 'low' ? 0 : 1,
        source: 'calculated',
      })
    }
    for (const row of historicalChart?.fractal_markers ?? []) {
      if (!times.has(row.time)) continue
      calculated.set(`${row.time}:${row.kind}`, { ...row, source: 'calculated' })
    }
    const inferred = predictions
      .filter(
        (row) => times.has(row.time),
      )
      .map<ChartMarker>((row) => ({
        time: row.time,
        kind: row.selected_class === 1 ? 'high' : 'low',
        label: row.selected_class,
        source: 'prediction',
      }))
    return [...calculated.values(), ...inferred].sort((a, b) =>
      compareTimes(a.time, b.time),
    )
  }, [chartTimeframe, historicalChart, merged.candles, selectedSymbol, state.predictions])

  const subscribedSymbols = useMemo(
    () => new Set(state.subscriptions.map((row) => row.symbol)),
    [state.subscriptions],
  )
  const addCandidates = watchlist.filter((item) => !subscribedSymbols.has(item.symbol))

  const subscribe = useCallback(async () => {
    if (!addSymbol) return
    setMutating(true)
    setSubscribeError(null)
    try {
      const rows = await liveApi.subscribe(addSymbol)
      applySubscriptions(rows)
      setSelectedSymbol(addSymbol)
      setAddSymbol('')
    } catch (e) {
      setSubscribeError(e instanceof Error ? e.message : String(e))
    } finally {
      setMutating(false)
    }
  }, [addSymbol, applySubscriptions])

  const unsubscribe = useCallback(
    async (row: LiveSubscription) => {
      if (!window.confirm(`${row.name ?? row.symbol} (${row.symbol}) 구독을 해제할까요?`)) return
      setMutating(true)
      setSubscribeError(null)
      try {
        const rows = await liveApi.unsubscribe(row.symbol)
        applySubscriptions(rows)
      } catch (e) {
        setSubscribeError(e instanceof Error ? e.message : String(e))
      } finally {
        setMutating(false)
      }
    },
    [applySubscriptions],
  )

  const selectedSubscription =
    state.subscriptions.find((row) => row.symbol === selectedSymbol) ?? null
  const selectedWarmup = selectedSymbol ? state.warmups[selectedSymbol] : undefined
  const provisional = liveCandles?.provisional ?? null
  const symbolPredictions = state.predictions.filter((row) => row.symbol === selectedSymbol)

  return (
    <>
      <aside className="side-panel live-side">
        <section className="control-section">
          <div className="section-title-row">
            <h2>연결 상태</h2>
            <span className={`live-ws-badge ${socketStatus}`}>
              {SOCKET_TEXT[socketStatus]}
            </span>
          </div>
          {stateError ? <p className="error">상태 조회 오류: {stateError}</p> : null}
          <dl className="live-conn-meta">
            <div>
              <dt>Kiwoom</dt>
              <dd>
                <span className={`live-chip ${state.connection.status}`}>
                  {CONNECTION_TEXT[state.connection.status]}
                </span>
                {state.connection.message ? (
                  <span className="live-conn-message">{state.connection.message}</span>
                ) : null}
              </dd>
            </div>
            <div>
              <dt>마지막 체결</dt>
              <dd>{formatDateTime(state.connection.last_tick_at ?? undefined)}</dd>
            </div>
            <div>
              <dt>heartbeat</dt>
              <dd>{formatDateTime(state.connection.last_heartbeat_at ?? undefined)}</dd>
            </div>
            <div>
              <dt>장 상태</dt>
              <dd>{state.connection.market_state ?? '-'}</dd>
            </div>
          </dl>
          {state.lastError ? (
            <p className={state.lastError.recoverable ? 'live-error recoverable' : 'live-error'}>
              [{state.lastError.scope}
              {state.lastError.symbol ? ` · ${state.lastError.symbol}` : ''}]{' '}
              {state.lastError.message}
              {state.lastError.recoverable ? ' (복구 가능)' : ''}
              <button className="ghost" onClick={dismissError} type="button">
                닫기
              </button>
            </p>
          ) : null}
        </section>

        <ModelPanel deployment={state.deployment} onActivated={applyState} />

        <section className="control-section grow">
          <h2>실시간 구독</h2>
          {!state.deployment && state.subscriptions.length > 0 ? (
            <p className="hint">활성 모델이 없어 모든 종목의 추론 상태가 no_model입니다.</p>
          ) : null}
          <div className="live-add-row">
            <select
              disabled={addCandidates.length === 0 || mutating}
              onChange={(event) => setAddSymbol(event.target.value)}
              value={addSymbol}
            >
              <option value="">관심종목에서 선택...</option>
              {addCandidates.map((item) => (
                <option key={item.symbol} value={item.symbol}>
                  {item.name || item.symbol} · {item.symbol}
                </option>
              ))}
            </select>
            <button
              className="primary"
              disabled={!addSymbol || mutating}
              onClick={subscribe}
              type="button"
            >
              구독
            </button>
          </div>
          {subscribeError ? <p className="error">구독 오류: {subscribeError}</p> : null}
          {state.subscriptions.length === 0 ? (
            <p className="empty">
              구독 중인 종목이 없습니다. 관심종목에서 선택해 실시간 구독을 시작하세요.
            </p>
          ) : (
            <div className="live-sub-list">
              {state.subscriptions.map((row) => {
                const warmup = state.warmups[row.symbol]
                return (
                  <div
                    className={
                      row.symbol === selectedSymbol ? 'live-sub-row selected' : 'live-sub-row'
                    }
                    key={row.symbol}
                  >
                    <button
                      className="live-sub-main"
                      onClick={() => setSelectedSymbol(row.symbol)}
                      type="button"
                    >
                      <div className="live-sub-head">
                        <strong>{row.name || row.symbol}</strong>
                        <span className={`live-chip sub-${row.status}`}>
                          {SUBSCRIPTION_TEXT[row.status]}
                        </span>
                        <span className={`live-chip inf-${row.inference_status}`}>
                          {INFERENCE_TEXT[row.inference_status]}
                        </span>
                      </div>
                      <span className="live-sub-meta">
                        {row.symbol}
                        {row.last_tick_at ? ` · 체결 ${formatDateTime(row.last_tick_at)}` : ''}
                      </span>
                      {warmup ? (
                        <span className="live-sub-meta warmup">
                          워밍업 {warmup.available_bars}/{warmup.required_bars}봉 ·{' '}
                          {warmup.reason}
                        </span>
                      ) : null}
                      {row.error ? <span className="live-sub-error">✗ {row.error}</span> : null}
                    </button>
                    <button
                      className="danger"
                      disabled={mutating}
                      onClick={() => unsubscribe(row)}
                      type="button"
                    >
                      해제
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </aside>

      <section className="live-main">
        <ChartPanel
          actions={
            <div className="live-chart-actions">
              <div className="live-timeframe" aria-label="차트 타임프레임">
                <button
                  className={chartTimeframe === 'day' ? 'selected' : ''}
                  onClick={() => setChartTimeframe('day')}
                  type="button"
                >
                  일봉
                </button>
                <button
                  className={chartTimeframe === 'min1' ? 'selected' : ''}
                  onClick={() => setChartTimeframe('min1')}
                  type="button"
                >
                  1분봉
                </button>
              </div>
              <button
                className="indicator-button"
                onClick={indicators.openIndicatorPanel}
                type="button"
              >
                + 보조지표
              </button>
            </div>
          }
          emptyText={
            state.subscriptions.length === 0
              ? '구독 종목이 없습니다. 왼쪽에서 종목을 구독하면 실시간 차트가 표시됩니다.'
              : chartTimeframe === 'min1'
                ? '오늘 수신된 분봉이 없습니다.'
                : '조회된 일봉이 없습니다.'
          }
          error={chartError}
          hasContent={merged.candles.length > 0}
          legend={
            displayedOhlc ? (
              <div className="chart-legend">
                <div className="ohlc-row">
                  <span className="ohlc-item">
                    <strong>{formatEventTime(displayedOhlc.time)}</strong>
                    {provisional && compareTimes(provisional.time, displayedOhlc.time) === 0 ? (
                      <span className="live-provisional-badge">잠정</span>
                    ) : null}
                  </span>
                  {ohlcItems.map((item) => (
                    <span className="ohlc-item" key={item.label}>
                      <strong>{item.label}</strong>
                      <span>{formatPrice(item.value)}</span>
                      {item.change === null ? null : (
                        <span className={`ohlc-change ${item.tone}`}>
                          ({formatPercent(item.change)})
                        </span>
                      )}
                    </span>
                  ))}
                </div>
                {maSettings.some((setting) => setting.chart) ? (
                  <div className="legend-row">
                    <span>이동평균선</span>
                    {maSettings
                      .filter((setting) => setting.chart)
                      .map((setting) => (
                        <span className="legend-chip" key={setting.id}>
                          <strong style={{ color: setting.color }}>{setting.window}</strong>
                          <button
                            aria-label={`MA${setting.window} 삭제`}
                            onClick={() =>
                              setMaSettings((current) =>
                                current.length <= 1
                                  ? current
                                  : current.filter((item) => item.id !== setting.id),
                              )
                            }
                            type="button"
                          >
                            ×
                          </button>
                        </span>
                      ))}
                  </div>
                ) : null}
                {volumeChart ? (
                  <div className="legend-row">
                    <span>거래량</span>
                    <button
                      aria-label="거래량 숨기기"
                      className="legend-mini-button"
                      onClick={() => setVolumeChart(false)}
                      type="button"
                    >
                      ×
                    </button>
                  </div>
                ) : null}
                {state.deployment?.timeframe === chartTimeframe ? (
                  <div className="legend-row">
                    <span>H/L 학습 기준 프랙탈</span>
                    <span>● 모델 예측</span>
                  </div>
                ) : null}
              </div>
            ) : null
          }
          loading={chartLoading}
          message={chartMessage}
          overlay={
            <>
              {loadingOlder && historicalChart ? (
                <p className="chart-loading-more">과거 데이터 불러오는 중...</p>
              ) : null}
              {indicators.indicatorPanelOpen ? (
                <IndicatorSettingsPanel
                  barCount={merged.candles.length}
                  settings={indicators}
                />
              ) : null}
            </>
          }
          subtitle={`${chartTimeframe} · 캔들${
            visibleIndicators.movingAverages.length > 0
              ? ` + 이동평균선 ${legendText}`
              : ''
          }${visibleIndicators.volume ? ' + 거래량' : ''} · 추론 ${
            state.deployment?.timeframe ?? '모델 없음'
          }`}
          title={
            selectedSubscription
              ? `${selectedSubscription.name || selectedSubscription.symbol} • ${selectedSubscription.symbol}`
              : '구독 종목을 선택하세요'
          }
        >
          {merged.candles.length > 0 ? (
            <CandleChart
              canLoadMoreOlder={Boolean(historicalChart?.has_more)}
              candles={merged.candles}
              fitContentKey={`${selectedSymbol}:${chartTimeframe}`}
              isLoadingOlder={loadingOlder}
              ma={merged.ma}
              markers={markers}
              onLoadMoreOlder={loadOlderChart}
              onOhlcChange={setSelectedOhlc}
              visibleIndicators={visibleIndicators}
              volumes={merged.volumes}
            />
          ) : null}
        </ChartPanel>

        {selectedWarmup ? (
          <p className="live-warmup-banner">
            {selectedSymbol} 워밍업 중 — 봉 {selectedWarmup.available_bars}/
            {selectedWarmup.required_bars} · {selectedWarmup.reason}
          </p>
        ) : null}

        <section className="control-section live-log-section">
          <div className="section-title-row">
            <h2>최근 판정 로그</h2>
            <span className="live-log-caption">
              실험적 후보 점수 · 매매 신호 아님
              {selectedSymbol ? ` · ${selectedSymbol} ${symbolPredictions.length}건` : ''}
            </span>
          </div>
          <PredictionLog predictions={state.predictions} />
        </section>
      </section>
    </>
  )
}
