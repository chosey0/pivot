import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  type CacheStatus,
  type ChartResponse,
  type TimeframeCode,
  type WatchItem,
} from './api/client'
import type { OhlcPoint, VisibleIndicators } from './components/chart/CandleChart'
import { CandleChart } from './components/chart/CandleChart'
import './App.css'

type TabId = 'watchlist' | 'lab' | 'datasets' | 'diagnostics' | 'training' | 'live'

const TABS: { id: TabId; label: string }[] = [
  { id: 'watchlist', label: '종목 & 데이터' },
  { id: 'lab', label: '전처리 실험실' },
  { id: 'datasets', label: '데이터셋' },
  { id: 'diagnostics', label: '데이터 진단' },
  { id: 'training', label: '학습' },
  { id: 'live', label: '실시간' },
]

const MINUTE_UNITS = [1, 3, 5, 10, 15, 30, 45, 60]
const TICK_UNITS = [1, 3, 5, 10, 30]
type LineWidth = 1 | 2 | 3 | 4

interface MovingAverageSetting {
  id: string
  window: number
  color: string
  lineWidth: LineWidth
  chart: boolean
  feature: boolean
}

interface IndicatorPreset {
  name: string
  maSettings: MovingAverageSetting[]
  volumeChart: boolean
  volumeFeature: boolean
}

const INDICATOR_PRESET_STORAGE_KEY = 'pivot.indicatorPresets.v1'
const DEFAULT_INDICATOR_PRESET_NAME = '기본 MA 5/20/60/120'

const DEFAULT_MA_SETTINGS: MovingAverageSetting[] = [
  { id: 'ma-5', window: 5, color: '#009c62', lineWidth: 1, chart: true, feature: false },
  { id: 'ma-20', window: 20, color: '#e31b35', lineWidth: 1, chart: true, feature: true },
  { id: 'ma-60', window: 60, color: '#ff8a00', lineWidth: 1, chart: true, feature: false },
  { id: 'ma-120', window: 120, color: '#8a26b2', lineWidth: 1, chart: true, feature: true },
]

const KRW_FORMATTER = new Intl.NumberFormat('ko-KR', {
  maximumFractionDigits: 0,
})

function cloneMaSettings(settings: MovingAverageSetting[]) {
  return settings.map((setting) => ({ ...setting }))
}

function defaultIndicatorPreset(): IndicatorPreset {
  return {
    name: DEFAULT_INDICATOR_PRESET_NAME,
    maSettings: cloneMaSettings(DEFAULT_MA_SETTINGS),
    volumeChart: true,
    volumeFeature: false,
  }
}

function loadIndicatorPresets(): IndicatorPreset[] {
  if (typeof window === 'undefined') return [defaultIndicatorPreset()]
  const fallback = [defaultIndicatorPreset()]
  const raw = window.localStorage.getItem(INDICATOR_PRESET_STORAGE_KEY)
  if (!raw) return fallback
  try {
    const parsed = JSON.parse(raw) as IndicatorPreset[]
    return parsed.length > 0 ? parsed : fallback
  } catch {
    return fallback
  }
}

function saveIndicatorPresets(presets: IndicatorPreset[]) {
  window.localStorage.setItem(INDICATOR_PRESET_STORAGE_KEY, JSON.stringify(presets))
}

function featureColumnsFor(settings: MovingAverageSetting[], includeVolume: boolean) {
  return [
    'Open',
    'High',
    'Low',
    'Close',
    ...(includeVolume ? ['Volume'] : []),
    ...settings.filter((setting) => setting.feature).map((setting) => String(setting.window)),
  ]
}

function duplicateWindows(settings: MovingAverageSetting[]) {
  const seen = new Set<number>()
  const duplicated = new Set<number>()
  for (const setting of settings) {
    if (seen.has(setting.window)) duplicated.add(setting.window)
    seen.add(setting.window)
  }
  return [...duplicated].sort((a, b) => a - b)
}

function toTimeframeCode(kind: 'day' | 'minute' | 'tick', unit: number): TimeframeCode {
  if (kind === 'day') return 'day'
  return `${kind === 'minute' ? 'min' : 'tick'}${unit}` as TimeframeCode
}

function formatDateTime(value?: string) {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 19)
}

function formatPrice(value: number) {
  return `${KRW_FORMATTER.format(value)}원`
}

function percentChange(value: number, previousClose: number | null) {
  if (!previousClose) return null
  return ((value - previousClose) / previousClose) * 100
}

function formatPercent(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function changeTone(value: number | null) {
  if (value === null) return 'neutral'
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'neutral'
}

function App() {
  const today = new Date().toISOString().slice(0, 10)
  const [activeTab, setActiveTab] = useState<TabId>('watchlist')
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [statuses, setStatuses] = useState<Record<string, CacheStatus | null>>({})
  const [chart, setChart] = useState<ChartResponse | null>(null)
  const [selectedSymbol, setSelectedSymbol] = useState<string>('')
  const [timeframeKind, setTimeframeKind] = useState<'day' | 'minute' | 'tick'>('day')
  const [timeframeUnit, setTimeframeUnit] = useState(1)
  const [symbolInput, setSymbolInput] = useState('005930')
  const [nameInput, setNameInput] = useState('삼성전자')
  const [rangeEnabled, setRangeEnabled] = useState(false)
  const [startDate, setStartDate] = useState(today)
  const [endDate, setEndDate] = useState(today)
  const [indicatorPanelOpen, setIndicatorPanelOpen] = useState(false)
  const [maSettings, setMaSettings] = useState<MovingAverageSetting[]>(DEFAULT_MA_SETTINGS)
  const [volumeChart, setVolumeChart] = useState(true)
  const [volumeFeature, setVolumeFeature] = useState(false)
  const [draftMaSettings, setDraftMaSettings] = useState<MovingAverageSetting[]>(DEFAULT_MA_SETTINGS)
  const [draftVolumeChart, setDraftVolumeChart] = useState(true)
  const [draftVolumeFeature, setDraftVolumeFeature] = useState(false)
  const [indicatorPresets, setIndicatorPresets] = useState<IndicatorPreset[]>(loadIndicatorPresets)
  const [selectedIndicatorPreset, setSelectedIndicatorPreset] = useState(DEFAULT_INDICATOR_PRESET_NAME)
  const [presetNameInput, setPresetNameInput] = useState(DEFAULT_INDICATOR_PRESET_NAME)
  const [selectedOhlc, setSelectedOhlc] = useState<OhlcPoint | null>(null)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const timeframe = useMemo(
    () => toTimeframeCode(timeframeKind, timeframeUnit),
    [timeframeKind, timeframeUnit],
  )
  const visibleIndicators = useMemo<VisibleIndicators>(
    () => ({
      movingAverages: maSettings
        .filter((setting) => setting.chart)
        .map((setting) => ({
          window: String(setting.window),
          color: setting.color,
          lineWidth: setting.lineWidth,
        })),
      volume: volumeChart,
    }),
    [maSettings, volumeChart],
  )
  const maWindows = useMemo(
    () => Array.from(new Set(maSettings.map((setting) => setting.window))).sort((a, b) => a - b),
    [maSettings],
  )
  const maWindowsKey = maWindows.join(',')
  const legendText = useMemo(
    () =>
      maSettings
        .filter((setting) => setting.chart)
        .map((setting) => setting.window)
        .join(' '),
    [maSettings],
  )
  const draftFeatureColumns = useMemo(
    () => featureColumnsFor(draftMaSettings, draftVolumeFeature),
    [draftMaSettings, draftVolumeFeature],
  )
  const draftDuplicateWindows = useMemo(() => duplicateWindows(draftMaSettings), [draftMaSettings])
  const draftFeatureDimension = draftFeatureColumns.length
  const chartOnlyIndicators = useMemo(
    () => [
      ...draftMaSettings
        .filter((setting) => setting.chart && !setting.feature)
        .map((setting) => `MA${setting.window}`),
      ...(draftVolumeChart && !draftVolumeFeature ? ['Volume'] : []),
    ],
    [draftMaSettings, draftVolumeChart, draftVolumeFeature],
  )
  const featureOnlyIndicators = useMemo(
    () => [
      ...draftMaSettings
        .filter((setting) => setting.feature && !setting.chart)
        .map((setting) => `MA${setting.window}`),
      ...(draftVolumeFeature && !draftVolumeChart ? ['Volume'] : []),
    ],
    [draftMaSettings, draftVolumeChart, draftVolumeFeature],
  )
  const nanRiskIndicators = useMemo(() => {
    const barCount = chart?.candles.length ?? 0
    return draftMaSettings
      .filter((setting) => setting.feature)
      .map((setting) => ({
        label: `MA${setting.window}`,
        missingBars: Math.max(setting.window - 1, 0),
        tooLong: barCount > 0 && setting.window > barCount,
      }))
      .filter((item) => item.missingBars > 0)
  }, [chart, draftMaSettings])
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
    const next = await api.ingestStatus(
      items.map((item) => item.symbol),
      nextTimeframe,
    )
    setStatuses(next)
  }, [])

  const loadChart = useCallback(async (
    symbol: string,
    nextTimeframe: TimeframeCode,
    nextMaWindows: number[],
  ) => {
    if (!symbol) return
    setLoading(true)
    setError(null)
    try {
      const next = await api.chart(symbol, nextTimeframe, nextMaWindows)
      setChart(next)
      setSelectedSymbol(symbol)
      setMessage(`${symbol} ${nextTimeframe} 차트를 불러왔습니다.`)
    } catch (e) {
      setChart(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshWatchlist()
      .catch((e: Error) => setError(e.message))
  }, [refreshWatchlist])

  useEffect(() => {
    refreshStatus(watchlist, timeframe)
      .catch((e: Error) => setError(e.message))
    if (selectedSymbol) loadChart(selectedSymbol, timeframe, maWindows)
  }, [loadChart, maWindows, maWindowsKey, refreshStatus, selectedSymbol, timeframe, watchlist])

  useEffect(() => {
    saveIndicatorPresets(indicatorPresets)
  }, [indicatorPresets])

  useEffect(() => {
    setSelectedOhlc(chart?.candles[chart.candles.length - 1] ?? null)
  }, [chart])

  async function addWatchItem(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const symbol = symbolInput.trim()
    if (!symbol) return
    setLoading(true)
    setError(null)
    try {
      const items = await api.addWatchItem({ symbol, name: nameInput.trim() })
      setWatchlist(items)
      setSelectedSymbol(symbol)
      await refreshStatus(items, timeframe)
      setMessage(`${symbol}을 관심종목에 추가했습니다.`)
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
      setMessage(`${symbol}을 관심종목에서 제거했습니다.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function ingest(symbol: string) {
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
        rangeEnabled ? { start: startDate, end: endDate } : {},
      )
      const result = response.results[symbol]
      if (!result?.ok) throw new Error(result?.error ?? '수집 실패')
      await refreshStatus(watchlist, timeframe)
      await loadChart(symbol, timeframe, maWindows)
      setMessage(`${symbol} ${timeframe}${rangeText} 수집 완료: ${result.bars ?? 0}봉`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function openIndicatorPanel() {
    setDraftMaSettings(cloneMaSettings(maSettings))
    setDraftVolumeChart(volumeChart)
    setDraftVolumeFeature(volumeFeature)
    setPresetNameInput(selectedIndicatorPreset)
    setIndicatorPanelOpen(true)
  }

  function applyIndicatorDraft() {
    setMaSettings(cloneMaSettings(draftMaSettings))
    setVolumeChart(draftVolumeChart)
    setVolumeFeature(draftVolumeFeature)
    setIndicatorPanelOpen(false)
  }

  function cancelIndicatorDraft() {
    setDraftMaSettings(cloneMaSettings(maSettings))
    setDraftVolumeChart(volumeChart)
    setDraftVolumeFeature(volumeFeature)
    setIndicatorPanelOpen(false)
  }

  function resetIndicatorDraft() {
    setDraftMaSettings(cloneMaSettings(DEFAULT_MA_SETTINGS))
    setDraftVolumeChart(true)
    setDraftVolumeFeature(false)
    setPresetNameInput(DEFAULT_INDICATOR_PRESET_NAME)
  }

  function updateDraftMaSetting(id: string, patch: Partial<Omit<MovingAverageSetting, 'id'>>) {
    setDraftMaSettings((current) =>
      current.map((setting) => (setting.id === id ? { ...setting, ...patch } : setting)),
    )
  }

  function removeDraftMaSetting(id: string) {
    setDraftMaSettings((current) => current.filter((setting) => setting.id !== id))
  }

  function addDraftMaSetting() {
    setDraftMaSettings((current) => {
      const used = new Set(current.map((setting) => setting.window))
      const nextWindow = [5, 10, 20, 60, 120, 240].find((window) => !used.has(window)) ?? 20
      return [
        ...current,
        {
          id: `ma-${Date.now()}`,
          window: nextWindow,
          color: '#60a5fa',
          lineWidth: 1,
          chart: true,
          feature: false,
        },
      ]
    })
  }

  function applyIndicatorPreset(name: string) {
    const preset = indicatorPresets.find((item) => item.name === name)
    if (!preset) return
    setSelectedIndicatorPreset(name)
    setPresetNameInput(name)
    setDraftMaSettings(cloneMaSettings(preset.maSettings))
    setDraftVolumeChart(preset.volumeChart)
    setDraftVolumeFeature(preset.volumeFeature)
  }

  function saveCurrentIndicatorPreset() {
    const name = presetNameInput.trim()
    if (!name) return
    const nextPreset: IndicatorPreset = {
      name,
      maSettings: cloneMaSettings(draftMaSettings),
      volumeChart: draftVolumeChart,
      volumeFeature: draftVolumeFeature,
    }
    setIndicatorPresets((current) => {
      const withoutSameName = current.filter((preset) => preset.name !== name)
      return [...withoutSameName, nextPreset]
    })
    setSelectedIndicatorPreset(name)
    setMessage(`보조지표 프리셋 '${name}'을 저장했습니다.`)
  }

  function deleteSelectedIndicatorPreset() {
    if (selectedIndicatorPreset === DEFAULT_INDICATOR_PRESET_NAME) return
    setIndicatorPresets((current) =>
      current.filter((preset) => preset.name !== selectedIndicatorPreset),
    )
    applyIndicatorPreset(DEFAULT_INDICATOR_PRESET_NAME)
  }

  function renderPlaceholder(title: string) {
    return (
      <section className="placeholder">
        <h2>{title}</h2>
        <p>M1 범위에서는 화면 자리만 잡아 둡니다. 이후 마일스톤에서 실제 기능을 연결합니다.</p>
      </section>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>pivot</h1>
          <span className="app-subtitle">
            {chart
              ? `${chart.symbol} · ${chart.timeframe} · ${chart.candles.length.toLocaleString()} bars`
              : 'M1 data ingestion'}
          </span>
        </div>
        <nav className="tabs" aria-label="주요 화면">
          {TABS.map((tab) => (
            <button
              className={tab.id === activeTab ? 'tab active' : 'tab'}
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="app-main">
        {activeTab === 'watchlist' && (
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
                <h2>관심종목 추가</h2>
                <form className="add-form" onSubmit={addWatchItem}>
                  <label className="field">
                    종목코드
                    <input
                      maxLength={12}
                      onChange={(event) => setSymbolInput(event.target.value)}
                      placeholder="005930"
                      value={symbolInput}
                    />
                  </label>
                  <label className="field">
                    종목명
                    <input
                      onChange={(event) => setNameInput(event.target.value)}
                      placeholder="삼성전자"
                      value={nameInput}
                    />
                  </label>
                  <button className="primary" disabled={loading} type="submit">
                    추가
                  </button>
                </form>
              </section>

              <section className="control-section grow">
                <div className="section-title-row">
                  <h2>관심종목</h2>
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
                    <p className="empty">종목코드와 이름을 직접 입력해 관심종목을 추가하세요.</p>
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
                            onClick={() => loadChart(item.symbol, timeframe, maWindows)}
                            type="button"
                          >
                            <strong>{item.name || item.symbol}</strong>
                            <span>{item.symbol}</span>
                            <small>
                              {status
                                ? `${status.bars.toLocaleString()}봉 · ${formatDateTime(status.last)}`
                                : `${timeframe} 미수집`}
                            </small>
                          </button>
                          <div className="row-actions">
                            <button disabled={loading} onClick={() => ingest(item.symbol)} type="button">
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

            <section className="chart-panel">
              <div className="chart-toolbar">
                <div>
                  <h2>{selectedSymbol || '종목을 선택하세요'}</h2>
                  <span>
                    {timeframe} · 캔들
                    {visibleIndicators.movingAverages.length > 0
                      ? ` + 이동평균선 ${legendText}`
                      : ''}
                    {visibleIndicators.volume ? ' + 거래량' : ''}
                  </span>
                </div>
                <button
                  className="indicator-button"
                  onClick={openIndicatorPanel}
                  type="button"
                >
                  + 보조지표
                </button>
                {selectedSymbol && (
                  <button
                    className="ghost"
                    disabled={loading}
                    onClick={() => loadChart(selectedSymbol, timeframe, maWindows)}
                    type="button"
                  >
                    차트 새로고침
                  </button>
                )}
              </div>
              {error && <p className="error">오류: {error}</p>}
              {message && !error && <p className="message">{message}</p>}
              <div className="chart-area">
                {loading && !chart ? <p className="empty">불러오는 중...</p> : null}
                {chart && (
                  <div className="chart-legend">
                    <div className="ohlc-row">
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
                )}
                {chart ? (
                  <CandleChart
                    candles={chart.candles}
                    ma={chart.ma}
                    onOhlcChange={setSelectedOhlc}
                    visibleIndicators={visibleIndicators}
                    volumes={chart.volumes}
                  />
                ) : (
                  !loading && <p className="empty">수집된 종목을 선택하면 실데이터 차트가 표시됩니다.</p>
                )}
                {indicatorPanelOpen && (
                  <div className="indicator-overlay" role="dialog" aria-modal="true">
                    <div className="indicator-modal">
                      <aside className="indicator-menu">
                        <h3>상단 지표</h3>
                        <button className="indicator-menu-item active" type="button">
                          <span>이동평균선</span>
                          <span className="check-dot">✓</span>
                        </button>
                        {['일목균형표', '볼린저 밴드', '슈퍼트렌드', '매물대분석', '엔벨로프', '윌리엄스 프랙탈'].map(
                          (item) => (
                            <button className="indicator-menu-item muted" disabled key={item} type="button">
                              <span>{item}</span>
                              <span>⌄</span>
                            </button>
                          ),
                        )}
                      </aside>
                      <section className="indicator-editor">
                        <div className="indicator-editor-head">
                          <div>
                            <h3>이동평균선</h3>
                            <p>지난 n일 동안 주가 평균값을 이은 선</p>
                          </div>
                          <button
                            className="modal-close"
                            onClick={cancelIndicatorDraft}
                            type="button"
                            aria-label="보조지표 닫기"
                          >
                            ×
                          </button>
                        </div>

                        <div className="preset-bar">
                          <label>
                            프리셋
                            <select
                              onChange={(event) => applyIndicatorPreset(event.target.value)}
                              value={selectedIndicatorPreset}
                            >
                              {indicatorPresets.map((preset) => (
                                <option key={preset.name} value={preset.name}>
                                  {preset.name}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label>
                            이름
                            <input
                              onChange={(event) => setPresetNameInput(event.target.value)}
                              placeholder="프리셋 이름"
                              value={presetNameInput}
                            />
                          </label>
                          <button className="secondary-action" onClick={saveCurrentIndicatorPreset} type="button">
                            저장
                          </button>
                          <button
                            className="secondary-action"
                            disabled={selectedIndicatorPreset === DEFAULT_INDICATOR_PRESET_NAME}
                            onClick={deleteSelectedIndicatorPreset}
                            type="button"
                          >
                            삭제
                          </button>
                        </div>

                        <div className="ma-settings">
                          {draftMaSettings.map((setting, index) => (
                            <div className="ma-setting-row" key={setting.id}>
                              <span className="period-label">기간{index + 1}</span>
                              <label className="swatch-field">
                                <input
                                  aria-label={`기간${index + 1} 색상`}
                                  onChange={(event) =>
                                    updateDraftMaSetting(setting.id, { color: event.target.value })
                                  }
                                  type="color"
                                  value={setting.color}
                                />
                              </label>
                              <select
                                aria-label={`기간${index + 1} 선 굵기`}
                                onChange={(event) =>
                                  updateDraftMaSetting(setting.id, {
                                    lineWidth: Number(event.target.value) as LineWidth,
                                  })
                                }
                                value={setting.lineWidth}
                              >
                                {[1, 2, 3, 4].map((width) => (
                                  <option key={width} value={width}>
                                    {width}px
                                  </option>
                                ))}
                              </select>
                              <select aria-label={`기간${index + 1} 기준값`} disabled value="Close">
                                <option value="Close">종가</option>
                              </select>
                              <input
                                aria-label={`기간${index + 1} 값`}
                                min={1}
                                onChange={(event) =>
                                  updateDraftMaSetting(setting.id, {
                                    window: Math.max(1, Number(event.target.value) || 1),
                                  })
                                }
                                type="number"
                                value={setting.window}
                              />
                              <label className="compact-check">
                                <input
                                  checked={setting.chart}
                                  onChange={() => updateDraftMaSetting(setting.id, { chart: !setting.chart })}
                                  type="checkbox"
                                />
                                차트
                              </label>
                              <label className="compact-check">
                                <input
                                  checked={setting.feature}
                                  onChange={() => updateDraftMaSetting(setting.id, { feature: !setting.feature })}
                                  type="checkbox"
                                />
                                학습
                              </label>
                              <button
                                className="icon-button"
                                disabled={draftMaSettings.length <= 1}
                                onClick={() => removeDraftMaSetting(setting.id)}
                                type="button"
                                aria-label={`기간${index + 1} 삭제`}
                              >
                                ×
                              </button>
                            </div>
                          ))}
                        </div>

                        <div className="indicator-actions">
                          <button className="add-period" onClick={addDraftMaSetting} type="button">
                            <span>＋</span>
                            기간 추가
                          </button>
                          <label className="compact-check volume-toggle">
                            <input
                              checked={draftVolumeChart}
                              onChange={() => setDraftVolumeChart((current) => !current)}
                              type="checkbox"
                            />
                            거래량 표시
                          </label>
                          <label className="compact-check volume-toggle">
                            <input
                              checked={draftVolumeFeature}
                              onChange={() => setDraftVolumeFeature((current) => !current)}
                              type="checkbox"
                            />
                            거래량 학습 피처
                          </label>
                        </div>

                        <div className="indicator-diagnostics">
                          <div className="feature-preview">
                            <strong>전처리 프리셋 features</strong>
                            <span>{draftFeatureColumns.join(', ')}</span>
                            <em>입력 차원 {draftFeatureDimension}</em>
                          </div>
                          {draftDuplicateWindows.length > 0 && (
                            <p className="warning">중복 기간: {draftDuplicateWindows.join(', ')}. 같은 MA 컬럼은 하나로 병합하는 편이 안전합니다.</p>
                          )}
                          {chartOnlyIndicators.length > 0 && (
                            <p className="notice">차트에만 표시: {chartOnlyIndicators.join(', ')}</p>
                          )}
                          {featureOnlyIndicators.length > 0 && (
                            <p className="notice">학습에만 포함: {featureOnlyIndicators.join(', ')}</p>
                          )}
                          {nanRiskIndicators.length > 0 && (
                            <p className="notice">
                              NaN 주의: {nanRiskIndicators.map((item) => `${item.label} 앞 ${item.missingBars}봉`).join(', ')}
                              {nanRiskIndicators.some((item) => item.tooLong) ? ' · 현재 봉 수보다 긴 기간이 있습니다.' : ''}
                            </p>
                          )}
                        </div>

                        <div className="modal-actions">
                          <button className="secondary-action" onClick={resetIndicatorDraft} type="button">
                            초기화
                          </button>
                          <button className="secondary-action" onClick={cancelIndicatorDraft} type="button">
                            취소
                          </button>
                          <button className="apply-action" onClick={applyIndicatorDraft} type="button">
                            적용
                          </button>
                        </div>
                      </section>
                    </div>
                  </div>
                )}
              </div>
            </section>
          </>
        )}

        {activeTab === 'lab' &&
          renderPlaceholder('전처리 실험실: M2에서 프랙탈 마커와 파라미터 미리보기를 연결합니다.')}
        {activeTab === 'datasets' && renderPlaceholder('데이터셋: M3에서 일괄 처리와 샘플 브라우저를 연결합니다.')}
        {activeTab === 'diagnostics' && renderPlaceholder('데이터 진단: M3에서 품질 리포트를 연결합니다.')}
        {activeTab === 'training' && renderPlaceholder('학습: M4에서 run 관리와 평가 지표를 연결합니다.')}
        {activeTab === 'live' && renderPlaceholder('실시간: M5에서 WebSocket 추론을 연결합니다.')}
      </main>
    </div>
  )
}

export default App
