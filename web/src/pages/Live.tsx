import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  type ChartResponse,
  type InstrumentRegion,
  type SymbolSuggestion,
  type TimeframeCode,
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
import { SymbolSearchBox } from '../components/symbols/SymbolSearchBox'
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

const MARKET_TEXT: Record<InstrumentRegion, string> = {
  domestic: '국내',
  overseas: '해외',
}

function compareTimes(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

function timeKey(time: string | number): string {
  if (typeof time === 'number' || /^\d+$/.test(time)) return `number:${Number(time)}`
  return `string:${time}`
}

function PredictionMarkerControl({
  disabled,
  onApply,
  saving,
  threshold,
}: {
  disabled: boolean
  onApply: (threshold: number) => Promise<void>
  saving: boolean
  threshold: number
}) {
  const [value, setValue] = useState(String(threshold))
  useEffect(() => setValue(String(threshold)), [threshold])
  const parsed = Number(value)
  const valid = /\d/.test(value) && Number.isFinite(parsed) && parsed >= 0 && parsed <= 100

  return (
    <div className="live-prediction-threshold">
      <label htmlFor="live-prediction-threshold">판정 표시</label>
      <input
        id="live-prediction-threshold"
        aria-label="모델 판정 표시 최소 확률"
        disabled={disabled || saving}
        inputMode="decimal"
        onChange={(event) => {
          if (/^\d*\.?\d*$/.test(event.target.value)) setValue(event.target.value)
        }}
        pattern="[0-9]*[.]?[0-9]*"
        type="text"
        value={value}
      />
      <span>% 이상</span>
      <button
        disabled={disabled || saving || !valid || parsed === threshold}
        onClick={() => onApply(parsed)}
        type="button"
      >
        {saving ? '적용 중' : '적용'}
      </button>
    </div>
  )
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

export function Live({ active }: { active: boolean }) {
  const { state, socketStatus, applyState, applySubscriptions, dismissError } = useLiveSocket()
  const [stateError, setStateError] = useState<string | null>(null)
  const [addRegion, setAddRegion] = useState<InstrumentRegion>('domestic')
  const [addQuery, setAddQuery] = useState('')
  const [addSymbol, setAddSymbol] = useState('')
  const [addSuggestion, setAddSuggestion] = useState<SymbolSuggestion | null>(null)
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
  const [thresholdSaving, setThresholdSaving] = useState(false)
  const [anchorPicking, setAnchorPicking] = useState(false)
  const [anchorSaving, setAnchorSaving] = useState(false)
  const historyCacheRef = useRef(
    new Map<string, { snapshotNonce: number; data: LiveHistoryResponse }>(),
  )

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
  const historyCacheKey = selectedSymbol
    ? `${selectedSymbol}:${chartTimeframe}:${maWindowsKey}`
    : null
  const subscribedSymbols = useMemo(
    () => new Set(state.subscriptions.map((row) => row.symbol)),
    [state.subscriptions],
  )

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

  // 구독이 끊긴 종목의 history는 서버가 422로 막는다. 해제 직후 snapshot이 먼저 도착하면
  // 선택 종목 보정 전에 이 effect가 돌 수 있어, 구독 여부를 직접 확인한다.
  const selectionSubscribed =
    selectedSymbol !== null && state.subscriptions.some((row) => row.symbol === selectedSymbol)

  // 과거 봉은 Live 전용 Kiwoom REST 조회로 읽는다. 재연결 snapshot마다 재조회해
  // 단절 동안의 마감 봉을 delta 재생 없이 복구한다 (docs/08 §6.2).
  useEffect(() => {
    if (!selectedSymbol || !selectionSubscribed) {
      setChart(null)
      setSelectedOhlc(null)
      setChartError(null)
      setChartLoading(false)
      return
    }
    let stale = false
    const cached = historyCacheKey ? historyCacheRef.current.get(historyCacheKey) : undefined
    if (cached?.snapshotNonce === state.snapshotNonce) {
      setChart(cached.data)
      setSelectedOhlc(cached.data.candles.at(-1) ?? null)
      setChartError(null)
      setChartMessage(null)
      setChartLoading(false)
      return
    }
    setChartLoading(true)
    setChartError(null)
    setChartMessage(null)
    liveApi
      .history(selectedSymbol, chartTimeframe, maWindows)
      .then((next) => {
        if (stale) return
        if (historyCacheKey) {
          historyCacheRef.current.set(historyCacheKey, {
            snapshotNonce: state.snapshotNonce,
            data: next,
          })
        }
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
  }, [
    chartTimeframe,
    historyCacheKey,
    maWindows,
    maWindowsKey,
    selectedSymbol,
    selectionSubscribed,
    state.snapshotNonce,
  ])

  const liveCandles = selectedSymbol
    ? state.candles[liveCandleKey(selectedSymbol, chartTimeframe)]
    : undefined
  const selectedManualAnchor = state.manualAnchors.find(
    (row) => row.symbol === selectedSymbol && row.timeframe === chartTimeframe,
  )
  const historicalChart =
    chart?.symbol === selectedSymbol && chart.timeframe === chartTimeframe ? chart : null

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
      setChart((current) => {
        if (current?.symbol !== older.symbol || current.timeframe !== older.timeframe) {
          return current
        }
        const next = mergeLiveHistory(current, older)
        if (historyCacheKey) {
          historyCacheRef.current.set(historyCacheKey, {
            snapshotNonce: state.snapshotNonce,
            data: next,
          })
        }
        return next
      })
    } catch (e) {
      setChartError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingOlder(false)
    }
  }, [
    chartTimeframe,
    historicalChart,
    historyCacheKey,
    loadingOlder,
    maWindows,
    selectedSymbol,
    state.snapshotNonce,
  ])

  useEffect(() => {
    setSelectedOhlc((current) => {
      if (current && merged.candles.some((candle) => candle.time === current.time)) {
        return current
      }
      return merged.candles.at(-1) ?? null
    })
  }, [merged.candles])

  const displayedOhlc = selectedOhlc ?? merged.candles.at(-1) ?? null
  const predictionThreshold = Number((state.predictionThreshold * 100).toFixed(4))
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
    const times = new Map(merged.candles.map((candle) => [timeKey(candle.time), candle.time]))
    const predictions = state.predictions.filter(
      (row) => row.symbol === selectedSymbol && row.timeframe === chartTimeframe,
    )
    const calculated = new Map<string, ChartMarker>()
    const inferred = new Map<string, ChartMarker>()
    for (const row of predictions.flatMap((prediction) => prediction.candidate_windows)) {
      const candleTime = times.get(timeKey(row.anchor_time))
      if (candleTime === undefined) continue
      const marker = {
        time: candleTime,
        kind: row.anchor_kind,
        label: row.anchor_kind === 'low' ? 0 : row.anchor_kind === 'high' ? 1 : 2,
        source: row.anchor_source,
        confidence: row.anchor_confidence ?? undefined,
      } satisfies ChartMarker
      const key = `${timeKey(candleTime)}:${row.anchor_kind}`
      if (row.anchor_source === 'prediction') inferred.set(key, marker)
      else calculated.set(key, marker)
    }
    for (const row of historicalChart?.fractal_markers ?? []) {
      const candleTime = times.get(timeKey(row.time))
      if (candleTime === undefined) continue
      calculated.set(`${timeKey(candleTime)}:${row.kind}`, {
        ...row,
        time: candleTime,
        source: 'calculated',
      })
    }
    if (selectedManualAnchor) {
      const candleTime = times.get(timeKey(selectedManualAnchor.time))
      if (candleTime !== undefined) {
        calculated.set(`${timeKey(candleTime)}:manual`, {
          time: candleTime,
          kind: 'manual',
          label: 2,
          source: 'manual',
        })
      }
    }
    for (const row of predictions) {
      const candleTime = times.get(timeKey(row.time))
      const confidence = row.scores[row.selected_class]
      if (
        row.selected_class === 2 ||
        candleTime === undefined ||
        !Number.isFinite(confidence) ||
        confidence < predictionThreshold / 100
      ) {
        continue
      }
      const kind = row.selected_class === 1 ? 'high' : 'low'
      inferred.set(`${timeKey(candleTime)}:${kind}`, {
        time: candleTime,
        kind,
        label: row.selected_class,
        source: 'prediction',
        confidence,
      })
    }
    return [...calculated.values(), ...inferred.values()].sort((a, b) =>
      compareTimes(a.time, b.time),
    )
  }, [
    chartTimeframe,
    historicalChart,
    merged.candles,
    predictionThreshold,
    selectedSymbol,
    selectedManualAnchor,
    state.predictions,
  ])
  const visiblePredictionCount = markers.filter((marker) => marker.source === 'prediction').length

  const applyPredictionThreshold = useCallback(
    async (threshold: number) => {
      setThresholdSaving(true)
      setChartError(null)
      try {
        applyState(await liveApi.setPredictionThreshold(threshold / 100))
      } catch (error) {
        setChartError(error instanceof Error ? error.message : String(error))
      } finally {
        setThresholdSaving(false)
      }
    },
    [applyState],
  )

  useEffect(() => setAnchorPicking(false), [chartTimeframe, selectedSymbol])

  const applyManualAnchor = useCallback(
    async (time: string | number) => {
      if (!anchorPicking || !selectedSymbol) return
      setAnchorSaving(true)
      setChartError(null)
      try {
        applyState(await liveApi.setManualAnchor(selectedSymbol, chartTimeframe, time))
        setAnchorPicking(false)
        setChartMessage(`시작 앵커를 ${formatEventTime(time)}로 지정했습니다.`)
      } catch (error) {
        setChartError(error instanceof Error ? error.message : String(error))
      } finally {
        setAnchorSaving(false)
      }
    },
    [anchorPicking, applyState, chartTimeframe, selectedSymbol],
  )

  const clearManualAnchor = useCallback(async () => {
    if (!selectedSymbol) return
    setAnchorSaving(true)
    setChartError(null)
    try {
      applyState(await liveApi.clearManualAnchor(selectedSymbol))
      setAnchorPicking(false)
      setChartMessage('자동 앵커 선택으로 돌아갔습니다.')
    } catch (error) {
      setChartError(error instanceof Error ? error.message : String(error))
    } finally {
      setAnchorSaving(false)
    }
  }, [applyState, selectedSymbol])

  const subscribe = useCallback(async () => {
    if (!addSuggestion) return
    setMutating(true)
    setSubscribeError(null)
    try {
      const rows = await liveApi.subscribe({
        symbol: addSuggestion.symbol,
        name: addSuggestion.name,
        region: addRegion,
        exchange: addSuggestion.exchange,
      })
      for (const key of historyCacheRef.current.keys()) {
        if (key.startsWith(`${addSuggestion.symbol}:`)) historyCacheRef.current.delete(key)
      }
      applySubscriptions(rows)
      setSelectedSymbol(addSuggestion.symbol)
      setAddQuery('')
      setAddSymbol('')
      setAddSuggestion(null)
    } catch (e) {
      setSubscribeError(e instanceof Error ? e.message : String(e))
    } finally {
      setMutating(false)
    }
  }, [addRegion, addSuggestion, applySubscriptions])

  const unsubscribe = useCallback(
    async (row: LiveSubscription) => {
      if (!window.confirm(`${row.name ?? row.symbol} (${row.symbol}) 구독을 해제할까요?`)) return
      setMutating(true)
      setSubscribeError(null)
      try {
        const rows = await liveApi.unsubscribe(row.symbol)
        for (const key of historyCacheRef.current.keys()) {
          if (key.startsWith(`${row.symbol}:`)) historyCacheRef.current.delete(key)
        }
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
  const selectedCurrency = selectedSubscription?.region === 'overseas' ? 'USD' : 'KRW'
  const selectedWarmup = selectedSymbol ? state.warmups[selectedSymbol] : undefined
  const provisional = liveCandles?.provisional ?? null
  const symbolPredictions = state.predictions.filter((row) => row.symbol === selectedSymbol)

  if (!active) return null

  return (
    <>
      <aside className="side-panel live-side">
        {/* 접어도 연결 배지는 summary에 남아 상태를 계속 볼 수 있다 */}
        <details className="live-accordion" open>
          <summary>
            <h2>연결 상태</h2>
            <span className={`live-ws-badge ${socketStatus}`}>
              {SOCKET_TEXT[socketStatus]}
            </span>
          </summary>
          <div className="live-accordion-body">
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
          </div>
        </details>

        <ModelPanel deployment={state.deployment} onActivated={applyState} />

        <section className="control-section grow">
          <h2>실시간 구독</h2>
          {!state.deployment && state.subscriptions.length > 0 ? (
            <p className="hint">활성 모델이 없어 모든 종목의 추론 상태가 no_model입니다.</p>
          ) : null}
          <div className="live-add-row">
            <select
              aria-label="시장"
              disabled={mutating}
              onChange={(event) => {
                setAddRegion(event.target.value as InstrumentRegion)
                setAddQuery('')
                setAddSymbol('')
                setAddSuggestion(null)
                setSubscribeError(null)
              }}
              value={addRegion}
            >
              <option value="domestic">국내</option>
              <option value="overseas">해외</option>
            </select>
            <SymbolSearchBox
              disabled={mutating}
              excludeSymbols={subscribedSymbols}
              onError={setSubscribeError}
              onQueryChange={(query) => {
                setAddQuery(query)
                setAddSymbol('')
                setAddSuggestion(null)
                setSubscribeError(null)
              }}
              onSelect={(item) => {
                setAddQuery(`${item.name} · ${item.symbol} · ${item.exchange}`)
                setAddSymbol(item.symbol)
                setAddSuggestion(item)
              }}
              placeholder={addRegion === 'domestic' ? '삼성전자 또는 005930' : 'Apple 또는 AAPL'}
              query={addQuery}
              region={addRegion}
              selectedSymbol={addSymbol}
            />
            <button
              className="primary"
              disabled={!addSuggestion || mutating}
              onClick={subscribe}
              type="button"
            >
              구독
            </button>
          </div>
          {subscribeError ? <p className="error">구독 오류: {subscribeError}</p> : null}
          {state.subscriptions.length === 0 ? (
            <p className="empty">
              구독 중인 종목이 없습니다. 종목을 검색해 실시간 구독을 시작하세요.
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
                        {row.symbol} · {MARKET_TEXT[row.region]}
                        {row.exchange ? ` · ${row.exchange}` : ''}
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
              <PredictionMarkerControl
                disabled={state.deployment?.timeframe !== chartTimeframe}
                onApply={applyPredictionThreshold}
                saving={thresholdSaving}
                threshold={predictionThreshold}
              />
              <button
                className={`indicator-button live-anchor-button${anchorPicking ? ' selected' : ''}`}
                disabled={
                  anchorSaving ||
                  !selectedSymbol ||
                  state.deployment?.timeframe !== chartTimeframe
                }
                onClick={() => setAnchorPicking((current) => !current)}
                type="button"
              >
                {anchorPicking
                  ? '캔들을 선택하세요'
                  : selectedManualAnchor
                    ? '앵커 변경'
                    : '시작 앵커 지정'}
              </button>
              {selectedManualAnchor ? (
                <button
                  className="indicator-button"
                  disabled={anchorSaving}
                  onClick={clearManualAnchor}
                  type="button"
                >
                  자동 앵커
                </button>
              ) : null}
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
              <div className="chart-legend chart-legend-compact">
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
                      <span>{formatPrice(item.value, selectedCurrency)}</span>
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
                    <span>
                      ● 모델 판정 {visiblePredictionCount}건 · {predictionThreshold}% 이상
                    </span>
                  </div>
                ) : null}
                {selectedManualAnchor ? (
                  <div className="legend-row live-manual-anchor-legend">
                    <span>■ 시작 앵커</span>
                    <strong>{formatEventTime(selectedManualAnchor.time)}</strong>
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
              onTimeClick={anchorPicking ? applyManualAnchor : undefined}
              priceDecimals={selectedSubscription?.region === 'overseas' ? 2 : 0}
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
          <PredictionLog predictions={symbolPredictions} />
        </section>
      </section>
    </>
  )
}
