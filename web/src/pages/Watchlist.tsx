import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  type CacheStatus,
  type ChartResponse,
  type InstrumentRegion,
  type TimeframeCode,
  type WatchItem,
} from '../api/client'
import type { OhlcPoint } from '../components/chart/CandleChart'
import { CandleChart } from '../components/chart/CandleChart'
import { ChartPanel } from '../components/chart/ChartPanel'
import { IndicatorSettingsPanel } from '../components/indicators/IndicatorSettingsPanel'
import { useIndicatorSettings } from '../components/indicators/useIndicatorSettings'
import { SymbolSearchBox } from '../components/symbols/SymbolSearchBox'
import { mergeChartPages } from '../lib/chart'
import { changeTone, formatDateTime, formatPercent, formatPrice, percentChange } from '../lib/format'
import { chartLimitFor, MINUTE_UNITS, TICK_UNITS, toTimeframeCode } from '../lib/timeframe'

function isMissingCacheError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error)
  return message.includes('404 Not Found') && message.includes('no cached data')
}

interface WatchlistProps {
  // 탭 전환 시에도 차트/선택 상태를 유지하기 위해 항상 마운트하고 표시만 제어한다
  active: boolean
  // App 헤더 부제에 현재 차트 정보를 표시하기 위한 최소한의 콜백
  onSubtitleChange: (subtitle: string | null) => void
}

export function Watchlist({ active, onSubtitleChange }: WatchlistProps) {
  const today = new Date().toISOString().slice(0, 10)
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [statuses, setStatuses] = useState<Record<string, CacheStatus | null>>({})
  const [chart, setChart] = useState<ChartResponse | null>(null)
  const [selectedSymbol, setSelectedSymbol] = useState<string>('')
  const [timeframeKind, setTimeframeKind] = useState<'day' | 'minute' | 'tick'>('day')
  const [timeframeUnit, setTimeframeUnit] = useState(1)
  const [symbolInput, setSymbolInput] = useState('005930')
  const [nameInput, setNameInput] = useState('삼성전자')
  const [region, setRegion] = useState<InstrumentRegion>('domestic')
  const [exchange, setExchange] = useState('ND')
  const [rangeEnabled, setRangeEnabled] = useState(false)
  const [startDate, setStartDate] = useState(today)
  const [endDate, setEndDate] = useState(today)
  const [selectedOhlc, setSelectedOhlc] = useState<OhlcPoint | null>(null)
  const [loading, setLoading] = useState(false)
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
  const maWindowsKey = maWindows.join(',')
  const selectedSymbolLabel = useMemo(() => {
    if (!selectedSymbol) return ''
    const name = watchlist.find((item) => item.symbol === selectedSymbol)?.name
    return name ? `${name} • ${selectedSymbol}` : selectedSymbol
  }, [selectedSymbol, watchlist])
  const selectedItem = watchlist.find((item) => item.symbol === selectedSymbol) ?? null
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
  const refreshWatchlist = useCallback(async () => {
    const items = await api.watchlist()
    setWatchlist(items)
    setSelectedSymbol((current) => current || items[0]?.symbol || '')
    return items
  }, [])

  const refreshStatus = useCallback(async (items: WatchItem[], nextTimeframe: TimeframeCode) => {
    if (items.length === 0) {
      setStatuses({})
      return
    }
    const rows = await Promise.all(
      items.map(async (item) => {
        const result = await api.ingestStatus([item.symbol], nextTimeframe, item)
        return [item.symbol, result[item.symbol] ?? null] as const
      }),
    )
    setStatuses(Object.fromEntries(rows))
  }, [])

  const loadChart = useCallback(async (
    item: WatchItem,
    nextTimeframe: TimeframeCode,
    nextMaWindows: number[],
  ) => {
    const { symbol } = item
    if (!symbol) return
    setSelectedSymbol(symbol)
    setLoading(true)
    setError(null)
    try {
      const next = await api.chart(symbol, nextTimeframe, nextMaWindows, {
        limit: chartLimitFor(nextTimeframe),
        region: item.region,
        exchange: item.exchange,
      })
      setChart(next)
      setSelectedOhlc(next.candles.at(-1) ?? null)
      setChartFitKey(`${symbol}:${nextTimeframe}:${Date.now()}`)
      setMessage(`${symbol} ${nextTimeframe} 차트를 불러왔습니다.`)
    } catch (e) {
      setChart(null)
      if (isMissingCacheError(e)) {
        setMessage(`${symbol} ${nextTimeframe} 캐시가 없습니다. 수집 버튼을 눌러 먼저 데이터를 수집하세요.`)
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
      const older = await api.chart(selectedItem.symbol, timeframe, maWindows, {
        limit: chartLimitFor(timeframe),
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
  }, [chart, loadingOlder, maWindows, selectedItem, timeframe])

  useEffect(() => {
    refreshWatchlist()
      .catch((e: Error) => setError(e.message))
  }, [refreshWatchlist])

  useEffect(() => {
    refreshStatus(watchlist, timeframe)
      .catch((e: Error) => setError(e.message))
    if (selectedItem) loadChart(selectedItem, timeframe, maWindows)
  }, [loadChart, maWindows, maWindowsKey, refreshStatus, selectedItem, timeframe, watchlist])

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
    setLoading(true)
    setError(null)
    try {
      const items = await api.addWatchItem({
        symbol,
        name: nameInput.trim(),
        region,
        exchange: region === 'overseas' ? exchange : '',
      })
      setWatchlist(items)
      setSelectedSymbol(symbol)
      await refreshStatus(items, timeframe)
      setMessage(`${symbol}을 종목 목록에 추가했습니다.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function removeWatchItem(symbol: string) {
    setLoading(true)
    setError(null)
    try {
      const items = await api.removeWatchItem(symbol)
      setWatchlist(items)
      setStatuses((prev) => {
        const next = { ...prev }
        delete next[symbol]
        return next
      })
      if (selectedSymbol === symbol) {
        setSelectedSymbol(items[0]?.symbol ?? '')
        setChart(null)
      }
      setMessage(`${symbol}을 종목 목록에서 제거했습니다.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function ingest(item: WatchItem) {
    const { symbol } = item
    setLoading(true)
    setError(null)
    const rangeText = rangeEnabled ? ` (${startDate} ~ ${endDate})` : ''
    setMessage(`${symbol} ${timeframe}${rangeText} 수집 중...`)
    try {
      if (rangeEnabled && startDate > endDate) {
        throw new Error('수집 시작일은 종료일보다 늦을 수 없습니다.')
      }
      const response = await api.ingest(
        [symbol],
        timeframe,
        {
          ...(rangeEnabled ? { start: startDate, end: endDate } : {}),
          region: item.region,
          exchange: item.exchange,
        },
      )
      const result = response.results[symbol]
      if (!result?.ok) throw new Error(result?.error ?? '수집 실패')
      await refreshStatus(watchlist, timeframe)
      await loadChart(item, timeframe, maWindows)
      setMessage(`${symbol} ${timeframe}${rangeText} 수집 완료: ${result.bars ?? 0}봉`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  if (!active) return null

  return (
    <>
      <aside className="side-panel">
        <section className="control-section">
          <h2>타임프레임</h2>
          <div className="segmented">
            <button
              className={timeframeKind === 'day' ? 'selected' : ''}
              onClick={() => {
                setTimeframeKind('day')
                setTimeframeUnit(1)
              }}
              type="button"
            >
              일봉
            </button>
            <button
              className={timeframeKind === 'minute' ? 'selected' : ''}
              onClick={() => {
                setTimeframeKind('minute')
                setTimeframeUnit(1)
              }}
              type="button"
            >
              분봉
            </button>
            <button
              className={timeframeKind === 'tick' ? 'selected' : ''}
              onClick={() => {
                setTimeframeKind('tick')
                setTimeframeUnit(30)
              }}
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
              시작일
              <input
                disabled={!rangeEnabled}
                max={endDate}
                onChange={(event) => setStartDate(event.target.value)}
                type="date"
                value={startDate}
              />
            </label>
            <label className="field">
              종료일
              <input
                disabled={!rangeEnabled}
                min={startDate}
                onChange={(event) => setEndDate(event.target.value)}
                type="date"
                value={endDate}
              />
            </label>
          </div>
          <p className="hint">
            미지정 시 기존 캐시의 마지막 봉 이후를 증분 수집합니다. 지정 시 해당 기간을 조회해
            캐시에 병합합니다.
          </p>
        </section>

        <section className="control-section">
          <h2>종목 추가</h2>
          {region === 'overseas' ? (
            <div className="range-grid">
              <label className="field">
                거래소
                <select onChange={(event) => setExchange(event.target.value)} value={exchange}>
                  <option value="ND">NASDAQ</option>
                  <option value="NY">NYSE</option>
                  <option value="NA">AMEX</option>
                </select>
              </label>
            </div>
          ) : null}
          <form className="add-form" onSubmit={addWatchItem}>
            <label className="field">
              시장
              <select
                onChange={(event) => {
                  const next = event.target.value as InstrumentRegion
                  setRegion(next)
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
                  if (item.exchange) setExchange(item.exchange)
                }}
                placeholder={
                  region === 'domestic' ? '삼성전자 또는 005930' : 'Apple 또는 AAPL'
                }
                query={nameInput}
                region={region}
                selectedSymbol={symbolInput}
              />
            </label>
            <button className="primary" disabled={loading} type="submit">
              추가
            </button>
          </form>
        </section>

        <section className="control-section grow">
          <div className="section-title-row">
            <h2>종목</h2>
            <button
              className="ghost"
              disabled={loading || watchlist.length === 0}
              onClick={() => refreshStatus(watchlist, timeframe).catch((e: Error) => setError(e.message))}
              type="button"
            >
              상태 갱신
            </button>
          </div>
          <div className="watch-table">
            {watchlist.length === 0 ? (
              <p className="empty">종목을 검색해 목록에 추가하세요.</p>
            ) : (
              watchlist.map((item) => {
                const status = statuses[item.symbol]
                return (
                  <div
                    className={item.symbol === selectedSymbol ? 'watch-row selected' : 'watch-row'}
                    key={item.symbol}
                  >
                    <button
                      className="watch-main"
                      onClick={() => loadChart(item, timeframe, maWindows)}
                      type="button"
                    >
                      <strong>{item.name || item.symbol}</strong>
                      <span>
                        {item.symbol} · {item.region === 'overseas' ? item.exchange : 'KRX'}
                      </span>
                      <small>
                        {status
                          ? `${status.bars.toLocaleString()}봉 · ${formatDateTime(status.last)}`
                          : `${timeframe} 미수집`}
                      </small>
                    </button>
                    <div className="row-actions">
                      <button disabled={loading} onClick={() => ingest(item)} type="button">
                        수집
                      </button>
                      <button
                        className="danger"
                        disabled={loading}
                        onClick={() => removeWatchItem(item.symbol)}
                        type="button"
                      >
                        삭제
                      </button>
                    </div>
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
                onClick={() => loadChart(selectedItem, timeframe, maWindows)}
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
            <div className="chart-legend">
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
            {timeframe} · 캔들
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
            visibleIndicators={visibleIndicators}
            volumes={chart.volumes}
          />
        ) : null}
      </ChartPanel>
    </>
  )
}
