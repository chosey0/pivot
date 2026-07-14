import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  type CacheStatus,
  type ChartResponse,
  type InstrumentRegion,
  type WatchItem,
} from '../api/client'
import type { OhlcPoint } from '../components/chart/CandleChart'
import { CandleChart } from '../components/chart/CandleChart'
import { ChartPanel } from '../components/chart/ChartPanel'
import { IndicatorSettingsPanel } from '../components/indicators/IndicatorSettingsPanel'
import { useIndicatorSettings } from '../components/indicators/useIndicatorSettings'
import { SymbolSearchBox } from '../components/symbols/SymbolSearchBox'
import { mergeChartPages } from '../lib/chart'
import {
  changeTone,
  formatDateTime,
  formatPercent,
  formatPrice,
  kstDateValue,
  percentChange,
} from '../lib/format'
import { chartLimitFor, MINUTE_UNITS, TICK_UNITS, toTimeframeCode } from '../lib/timeframe'
import { timeframeLabel, watchItemKey } from '../lib/watchlist'

function isMissingCacheError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error)
  return message.includes('404 Not Found') && message.includes('no cached data for')
}

function isEmptyRangeError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error)
  return message.includes('404 Not Found') && message.includes('no candles in requested chart range')
}

interface WatchlistProps {
  // 탭 전환 시에도 차트/선택 상태를 유지하기 위해 항상 마운트하고 표시만 제어한다
  active: boolean
  // App 헤더 부제에 현재 차트 정보를 표시하기 위한 최소한의 콜백
  onSubtitleChange: (subtitle: string | null) => void
}

type CollectionState = 'queued' | 'running'

export function Watchlist({ active, onSubtitleChange }: WatchlistProps) {
  const today = kstDateValue()
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [statuses, setStatuses] = useState<Record<string, CacheStatus | null>>({})
  const [chart, setChart] = useState<ChartResponse | null>(null)
  const [selectedKey, setSelectedKey] = useState('')
  const [timeframeKind, setTimeframeKind] = useState<'day' | 'minute' | 'tick'>('day')
  const [timeframeUnit, setTimeframeUnit] = useState(1)
  const [symbolInput, setSymbolInput] = useState('005930')
  const [nameInput, setNameInput] = useState('삼성전자')
  const [region, setRegion] = useState<InstrumentRegion>('domestic')
  const [exchange, setExchange] = useState('')
  const [rangeEnabled, setRangeEnabled] = useState(false)
  const [startDate, setStartDate] = useState(today)
  const [endDate, setEndDate] = useState(today)
  const [selectedOhlc, setSelectedOhlc] = useState<OhlcPoint | null>(null)
  const [loading, setLoading] = useState(false)
  const [collectionStates, setCollectionStates] = useState<Record<string, CollectionState>>({})
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [chartFitKey, setChartFitKey] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const indicators = useIndicatorSettings({ onMessage: setMessage })
  const {
    maSettings,
    setMaSettings,
    volumeChart,
    setVolumeChart,
    visibleIndicators,
    maWindows,
    legendText,
  } = indicators

  const timeframe = useMemo(
    () => toTimeframeCode(timeframeKind, timeframeUnit),
    [timeframeKind, timeframeUnit],
  )
  const rangeInputType = timeframeKind === 'day' ? 'date' : 'datetime-local'
  const maWindowsKey = maWindows.join(',')
  const selectedSymbolLabel = useMemo(() => {
    const item = watchlist.find((row) => watchItemKey(row) === selectedKey)
    if (!item) return ''
    const symbol = item.name ? `${item.name} • ${item.symbol}` : item.symbol
    return `${symbol} • ${timeframeLabel(item.timeframe)}`
  }, [selectedKey, watchlist])
  const selectedItem = watchlist.find((item) => watchItemKey(item) === selectedKey) ?? null
  const selectedKeyRef = useRef(selectedKey)
  const collectionQueueRef = useRef<Promise<void>>(Promise.resolve())
  const queuedCollectionKeysRef = useRef(new Set<string>())
  const selectedCurrency = selectedItem?.region === 'overseas' ? 'USD' : 'KRW'
  const displayedOhlc = selectedOhlc ?? chart?.candles[chart.candles.length - 1] ?? null
  const displayedOhlcPreviousClose = useMemo(() => {
    if (!chart || !displayedOhlc) return null
    const index = chart.candles.findIndex((candle) => candle.time === displayedOhlc.time)
    return index > 0 ? chart.candles[index - 1].close : null
  }, [chart, displayedOhlc])
  const ohlcItems = useMemo(() => {
    if (!displayedOhlc) return []
    return [
      { label: '시작', value: displayedOhlc.open },
      { label: '고가', value: displayedOhlc.high },
      { label: '저가', value: displayedOhlc.low },
      { label: '종가', value: displayedOhlc.close },
    ].map((item) => {
      const change = percentChange(item.value, displayedOhlcPreviousClose)
      return {
        ...item,
        change,
        tone: changeTone(change),
      }
    })
  }, [displayedOhlc, displayedOhlcPreviousClose])

  useEffect(() => {
    selectedKeyRef.current = selectedKey
  }, [selectedKey])

  function selectTimeframeKind(next: 'day' | 'minute' | 'tick', unit: number) {
    setTimeframeKind(next)
    setTimeframeUnit(unit)
    setStartDate((current) =>
      next === 'day' ? current.slice(0, 10) : `${current.slice(0, 10)}T00:00:00`,
    )
    setEndDate((current) =>
      next === 'day' ? current.slice(0, 10) : `${current.slice(0, 10)}T23:59:59`,
    )
  }

  const refreshWatchlist = useCallback(async () => {
    const items = await api.watchlist()
    setWatchlist(items)
    setSelectedKey((current) =>
      current && items.some((item) => watchItemKey(item) === current)
        ? current
        : items[0]
          ? watchItemKey(items[0])
          : '',
    )
    return items
  }, [])

  const refreshStatus = useCallback(async (items: WatchItem[]) => {
    if (items.length === 0) {
      setStatuses({})
      return {}
    }
    const rows = await Promise.all(
      items.map(async (item) => {
        const result = await api.ingestStatus([item.symbol], item.timeframe, {
          region: item.region,
          exchange: item.exchange,
          ...(item.start ? { start: item.start } : {}),
          ...(item.end ? { end: item.end } : {}),
        })
        return [watchItemKey(item), result[item.symbol] ?? null] as const
      }),
    )
    const next = Object.fromEntries(rows)
    setStatuses(next)
    return next
  }, [])

  const loadChart = useCallback(async (
    item: WatchItem,
    nextMaWindows: number[],
  ) => {
    const { symbol } = item
    if (!symbol) return
    setSelectedKey(watchItemKey(item))
    setLoading(true)
    setError(null)
    try {
      const next = await api.chart(symbol, item.timeframe, nextMaWindows, {
        limit: chartLimitFor(item.timeframe),
        ...(item.start ? { start: item.start } : {}),
        ...(item.end ? { end: item.end } : {}),
        region: item.region,
        exchange: item.exchange,
      })
      if (next.candles.length === 0) {
        throw new Error('지정한 기간에 표시할 캔들 데이터가 없습니다.')
      }
      setChart(next)
      setSelectedOhlc(next.candles.at(-1) ?? null)
      setChartFitKey(`${watchItemKey(item)}:${Date.now()}`)
      setMessage(`${symbol} ${item.timeframe} 차트를 불러왔습니다.`)
    } catch (e) {
      setChart((current) =>
        current?.symbol === symbol && current.timeframe === item.timeframe ? current : null,
      )
      if (isMissingCacheError(e)) {
        setMessage(`${symbol} ${item.timeframe} 캐시가 없습니다. 수집 버튼을 눌러 먼저 데이터를 수집하세요.`)
      } else if (isEmptyRangeError(e)) {
        setMessage(`${symbol} ${item.timeframe} 지정 기간에는 수집된 데이터가 없습니다.`)
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setLoading(false)
    }
  }, [])

  const loadOlderChart = useCallback(async () => {
    if (!chart || !selectedItem || !chart.has_more || loadingOlder) return
    const first = chart.candles[0]
    if (!first) return
    setLoadingOlder(true)
    setError(null)
    try {
      const older = await api.chart(selectedItem.symbol, selectedItem.timeframe, maWindows, {
        limit: chartLimitFor(selectedItem.timeframe),
        before: first.time,
        region: selectedItem.region,
        exchange: selectedItem.exchange,
      })
      setChart((current) => (current ? mergeChartPages(current, older) : older))
    } catch (e) {
      if (!isMissingCacheError(e)) {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setLoadingOlder(false)
    }
  }, [chart, loadingOlder, maWindows, selectedItem])

  useEffect(() => {
    refreshWatchlist()
      .catch((e: Error) => setError(e.message))
  }, [refreshWatchlist])

  useEffect(() => {
    refreshStatus(watchlist)
      .then((next) => {
        if (!selectedItem) return
        if (next[watchItemKey(selectedItem)]) {
          return loadChart(selectedItem, maWindows)
        }
        setChart(null)
        setMessage(
          `${selectedItem.symbol} ${selectedItem.timeframe} 캐시가 없습니다. 수집 버튼을 눌러 먼저 데이터를 수집하세요.`,
        )
      })
      .catch((e: Error) => setError(e.message))
  }, [loadChart, maWindows, maWindowsKey, refreshStatus, selectedItem, watchlist])

  const openChart = useCallback(
    (item: WatchItem) => {
      const key = watchItemKey(item)
      setSelectedKey(key)
      if (!statuses[key]) {
        setChart(null)
        setError(null)
        setMessage(
          `${item.symbol} ${item.timeframe} 캐시가 없습니다. 수집 버튼을 눌러 먼저 데이터를 수집하세요.`,
        )
        return
      }
      void loadChart(item, maWindows)
    },
    [loadChart, maWindows, statuses],
  )

  useEffect(() => {
    setSelectedOhlc((current) => {
      if (!chart) return null
      if (current && chart.candles.some((candle) => candle.time === current.time)) {
        return current
      }
      return chart.candles.at(-1) ?? null
    })
  }, [chart])

  useEffect(() => {
    onSubtitleChange(
      chart
        ? `${chart.symbol} · ${chart.timeframe} · ${chart.candles.length.toLocaleString()} bars`
        : null,
    )
  }, [chart, onSubtitleChange])

  async function addWatchItem(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const symbol = symbolInput.trim().toUpperCase()
    if (!symbol) {
      setError('검색 결과에서 종목을 선택하세요.')
      return
    }
    if (rangeEnabled && startDate > endDate) {
      setError('수집 시작일은 종료일보다 늦을 수 없습니다.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const items = await api.addWatchItem({
        symbol,
        name: nameInput.trim(),
        region,
        exchange: region === 'overseas' ? exchange : '',
        timeframe,
        start: rangeEnabled ? startDate : null,
        end: rangeEnabled ? endDate : null,
      })
      const added = items.at(-1)!
      setWatchlist(items)
      setSelectedKey(watchItemKey(added))
      await refreshStatus(items)
      setMessage(`${symbol} ${timeframe} 데이터 항목을 추가했습니다.`)
    } catch (e) {
      setMessage(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function removeWatchItem(item: WatchItem) {
    if (
      !window.confirm(
        `${item.name || item.symbol} ${timeframeLabel(item.timeframe)} 항목과 로컬 수집 데이터를 삭제할까요?`,
      )
    )
      return
    setLoading(true)
    setError(null)
    try {
      const removedKey = watchItemKey(item)
      const items = await api.removeWatchItem(item)
      setWatchlist(items)
      setStatuses((prev) => {
        const next = { ...prev }
        delete next[removedKey]
        return next
      })
      if (selectedKey === removedKey) {
        setSelectedKey(items[0] ? watchItemKey(items[0]) : '')
        setChart(null)
      }
      setMessage(`${item.symbol} ${item.timeframe} 데이터 항목을 제거했습니다.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function enqueueIngest(item: WatchItem) {
    const queueKey = watchItemKey(item)
    if (queuedCollectionKeysRef.current.has(queueKey)) return
    queuedCollectionKeysRef.current.add(queueKey)
    setCollectionStates((current) => ({
      ...current,
      [queueKey]: 'queued',
    }))
    setMessage(`${item.symbol} ${item.timeframe} 수집 대기 중...`)
    collectionQueueRef.current = collectionQueueRef.current.then(() =>
      runIngest(item, queueKey),
    )
  }

  async function runIngest(item: WatchItem, queueKey: string) {
    const { symbol } = item
    setCollectionStates((current) => ({
      ...current,
      [queueKey]: 'running',
    }))
    setError(null)
    const rangeText = item.start || item.end
      ? ` (${item.start ?? ''} ~ ${item.end ?? ''})`
      : ''
    setMessage(`${symbol} ${item.timeframe}${rangeText} 수집 중...`)
    try {
      const response = await api.ingest(
        [symbol],
        item.timeframe,
        {
          ...(item.start ? { start: item.start } : {}),
          ...(item.end ? { end: item.end } : {}),
          region: item.region,
          exchange: item.exchange,
        },
      )
      const result = response.results[symbol]
      if (!result?.ok) throw new Error(result?.error ?? '수집 실패')
      const items = await refreshWatchlist()
      await refreshStatus(items)
      if (selectedKeyRef.current === queueKey) {
        await loadChart(item, maWindows)
      }
      setMessage(`${symbol} ${item.timeframe}${rangeText} 수집 완료: ${result.bars ?? 0}봉`)
    } catch (e) {
      setMessage(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      queuedCollectionKeysRef.current.delete(queueKey)
      setCollectionStates((current) => {
        const next = { ...current }
        delete next[queueKey]
        return next
      })
    }
  }

  if (!active) return null

  return (
    <>
      <aside className="side-panel watch-side">
        <form className="collection-target-form" onSubmit={addWatchItem}>
          <section className="control-section">
            <h2>종목 검색</h2>
            <div className="add-form">
              <label className="field">
                시장
                <select
                  onChange={(event) => {
                    const next = event.target.value as InstrumentRegion
                    setRegion(next)
                    setExchange('')
                    setSymbolInput(next === 'domestic' ? '005930' : '')
                    setNameInput(next === 'domestic' ? '삼성전자' : '')
                  }}
                  value={region}
                >
                  <option value="domestic">국내</option>
                  <option value="overseas">해외</option>
                </select>
              </label>
              <label className="field">
                종목명 또는 코드
                <SymbolSearchBox
                  onError={setError}
                  onQueryChange={(query) => {
                    setSymbolInput('')
                    setNameInput(query)
                    setError(null)
                  }}
                  onSelect={(item) => {
                    setSymbolInput(item.symbol)
                    setNameInput(item.name)
                    setExchange(item.exchange)
                  }}
                  placeholder={
                    region === 'domestic' ? '삼성전자 또는 005930' : 'Apple 또는 AAPL'
                  }
                  query={nameInput}
                  region={region}
                  selectedSymbol={symbolInput}
                />
              </label>
            </div>
          </section>

          <section className="control-section">
            <h2>수집 타임프레임</h2>
            <div className="segmented">
            <button
              className={timeframeKind === 'day' ? 'selected' : ''}
              onClick={() => selectTimeframeKind('day', 1)}
              type="button"
            >
              일봉
            </button>
            <button
              className={timeframeKind === 'minute' ? 'selected' : ''}
              onClick={() => selectTimeframeKind('minute', 1)}
              type="button"
            >
              분봉
            </button>
            <button
              className={timeframeKind === 'tick' ? 'selected' : ''}
              onClick={() => selectTimeframeKind('tick', 30)}
              type="button"
            >
              틱봉
            </button>
            </div>
            {timeframeKind !== 'day' && (
              <label className="field">
                단위
                <select
                  value={timeframeUnit}
                  onChange={(event) => setTimeframeUnit(Number(event.target.value))}
                >
                  {(timeframeKind === 'minute' ? MINUTE_UNITS : TICK_UNITS).map((unit) => (
                    <option key={unit} value={unit}>
                      {unit}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </section>

          <section className="control-section">
            <div className="section-title-row">
              <h2>수집 기간</h2>
              <label className="inline-check">
                <input
                  checked={rangeEnabled}
                  onChange={(event) => setRangeEnabled(event.target.checked)}
                  type="checkbox"
                />
                직접 지정
              </label>
            </div>
            <div className="range-grid">
              <label className="field">
                {timeframeKind === 'day' ? '시작일' : '시작 시각'}
                <input
                  disabled={!rangeEnabled}
                  max={endDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  step={timeframeKind === 'day' ? undefined : 1}
                  type={rangeInputType}
                  value={startDate}
                />
              </label>
              <label className="field">
                {timeframeKind === 'day' ? '종료일' : '종료 시각'}
                <input
                  disabled={!rangeEnabled}
                  min={startDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  step={timeframeKind === 'day' ? undefined : 1}
                  type={rangeInputType}
                  value={endDate}
                />
              </label>
            </div>
            <p className="hint">
              날짜와 시각은 대한민국 시간(KST) 기준입니다. 미지정 시 기존 캐시의 마지막 봉 이후를
              증분 수집하고, 지정 시 해당 기간을 조회해 캐시에 병합합니다.
            </p>
            <button className="primary collection-target-add" disabled={loading} type="submit">
              수집 대상 추가
            </button>
          </section>
        </form>

        <section className="control-section grow">
          <div className="section-title-row">
            <h2>수집 대상</h2>
            <button
              className="ghost"
              disabled={loading || watchlist.length === 0}
              onClick={() => refreshStatus(watchlist).catch((e: Error) => setError(e.message))}
              type="button"
            >
              상태 갱신
            </button>
          </div>
          <div className="watch-table">
            {watchlist.length === 0 ? (
              <p className="empty">종목과 수집 조건을 설정해 대상을 추가하세요.</p>
            ) : (
              watchlist.map((item) => {
                const key = watchItemKey(item)
                const status = statuses[key]
                const collectionStatus = collectionStates[key]
                return (
                  <div
                    className={[
                      'watch-row',
                      key === selectedKey ? 'selected' : '',
                      collectionStatus === 'running' ? 'collecting' : '',
                    ].filter(Boolean).join(' ')}
                    key={key}
                  >
                    <button
                      className="watch-main"
                      onClick={() => openChart(item)}
                      type="button"
                    >
                      <strong>
                        {item.name || item.symbol} - {timeframeLabel(item.timeframe)}
                      </strong>
                      <span>
                        {item.symbol} · {item.region === 'overseas' ? item.exchange : 'KRX'}
                      </span>
                      <small className="data-range">
                        {status
                          ? `${status.bars.toLocaleString()} · ${formatDateTime(status.first)} ~ ${formatDateTime(status.last)}`
                          : `null · ${item.timeframe} · ${formatDateTime(item.start ?? undefined)} ~ ${formatDateTime(item.end ?? undefined)}`}
                      </small>
                    </button>
                    <div className="row-actions">
                      <button
                        disabled={loading || Boolean(collectionStatus)}
                        onClick={() => enqueueIngest(item)}
                        type="button"
                      >
                        {collectionStatus === 'queued'
                          ? '대기 중...'
                          : collectionStatus === 'running'
                            ? '수집 중...'
                            : '수집'}
                      </button>
                      <button
                        className="danger"
                        disabled={loading || Boolean(collectionStatus)}
                        onClick={() => removeWatchItem(item)}
                        type="button"
                      >
                        삭제
                      </button>
                    </div>
                    {collectionStatus === 'running' ? (
                      <div aria-live="polite" className="watch-collection-overlay" role="status">
                        <span aria-hidden="true" className="watch-collection-spinner" />
                        <span>데이터 수집 중</span>
                      </div>
                    ) : null}
                  </div>
                )
              })
            )}
          </div>
        </section>
      </aside>

      <ChartPanel
        actions={
          <>
            <button
              className="indicator-button"
              onClick={indicators.openIndicatorPanel}
              type="button"
            >
              + 보조지표
            </button>
            {selectedItem && (
              <button
                className="ghost"
                disabled={loading}
                onClick={() => openChart(selectedItem)}
                type="button"
              >
                차트 새로고침
              </button>
            )}
          </>
        }
        emptyText="수집된 종목을 선택하면 실데이터 차트가 표시됩니다."
        error={error}
        hasContent={Boolean(chart)}
        legend={
          chart ? (
            <div className="chart-legend chart-legend-compact">
              <div className="ohlc-row">
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
              {maSettings.some((setting) => setting.chart) && (
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
              )}
              {volumeChart && (
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
              )}
            </div>
          ) : null
        }
        loading={loading}
        message={message}
        overlay={
          <>
            {loadingOlder && chart ? <p className="chart-loading-more">과거 데이터 불러오는 중...</p> : null}
            {indicators.indicatorPanelOpen && (
              <IndicatorSettingsPanel
                barCount={chart?.candles.length ?? 0}
                settings={indicators}
              />
            )}
          </>
        }
        subtitle={
          <>
            {chart?.timeframe ?? timeframe} · 캔들
            {visibleIndicators.movingAverages.length > 0
              ? ` + 이동평균선 ${legendText}`
              : ''}
            {visibleIndicators.volume ? ' + 거래량' : ''}
          </>
        }
        title={selectedSymbolLabel || '종목을 선택하세요'}
      >
        {chart ? (
          <CandleChart
            canLoadMoreOlder={Boolean(chart.has_more)}
            candles={chart.candles}
            fitContentKey={chartFitKey}
            isLoadingOlder={loadingOlder}
            ma={chart.ma}
            onLoadMoreOlder={loadOlderChart}
            onOhlcChange={setSelectedOhlc}
            priceDecimals={selectedItem?.region === 'overseas' ? 2 : 0}
            visibleIndicators={visibleIndicators}
            volumes={chart.volumes}
          />
        ) : null}
      </ChartPanel>
    </>
  )
}
