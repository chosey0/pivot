import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  type PreviewParams,
  type PreviewResponse,
  type PreviewSample,
  type PreviewStats,
  type TimeframeCode,
  type WatchItem,
} from '../api/client'
import type { TimeRange, VisibleIndicators } from '../components/chart/CandleChart'
import { CandleChart } from '../components/chart/CandleChart'

const MINUTE_UNITS = [1, 3, 5, 10, 15, 30, 45, 60]
const TICK_UNITS = [1, 3, 5, 10, 30]
const PREVIEW_DEBOUNCE_MS = 400

// Watchlist 기본 보조지표와 같은 색상 (App.tsx DEFAULT_MA_SETTINGS)
const LAB_MA_LINES = [
  { window: 5, color: '#009c62' },
  { window: 20, color: '#e31b35' },
  { window: 60, color: '#ff8a00' },
  { window: 120, color: '#8a26b2' },
]

const LABEL_TEXT: Record<number, string> = {
  0: '저점 (0)',
  1: '고점 (1)',
  2: '무시 (2)',
}

function toTimeframeCode(kind: 'day' | 'minute' | 'tick', unit: number): TimeframeCode {
  if (kind === 'day') return 'day'
  return `${kind === 'minute' ? 'min' : 'tick'}${unit}` as TimeframeCode
}

function diffText(current: number, previous: number | undefined) {
  if (previous === undefined || previous === current) return null
  const delta = current - previous
  return `${previous.toLocaleString()} → ${current.toLocaleString()} (${delta > 0 ? '+' : ''}${delta.toLocaleString()})`
}

export function Lab() {
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [timeframeKind, setTimeframeKind] = useState<'day' | 'minute' | 'tick'>('day')
  const [timeframeUnit, setTimeframeUnit] = useState(1)
  const [fractalN, setFractalN] = useState(20)
  const [maxLen, setMaxLen] = useState(20)
  const [labelMode, setLabelMode] = useState<'cls3' | 'cls2_drop'>('cls3')
  const [ignoreRuleOn, setIgnoreRuleOn] = useState(true)
  const [maAlignment, setMaAlignment] = useState<'' | '20>120' | '5>20>120'>('')
  const [minAmountInput, setMinAmountInput] = useState('')
  const [featureWindows, setFeatureWindows] = useState<number[]>([20, 120])
  const [volumeFeature, setVolumeFeature] = useState(false)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [prevStats, setPrevStats] = useState<PreviewStats | null>(null)
  const [selectedSample, setSelectedSample] = useState<PreviewSample | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lastStatsRef = useRef<PreviewStats | null>(null)
  const requestSeqRef = useRef(0)

  const timeframe = useMemo(
    () => toTimeframeCode(timeframeKind, timeframeUnit),
    [timeframeKind, timeframeUnit],
  )

  const featureColumns = useMemo(
    () => [
      'Open',
      'High',
      'Low',
      'Close',
      ...(volumeFeature ? ['Volume'] : []),
      ...[...featureWindows].sort((a, b) => a - b).map(String),
    ],
    [featureWindows, volumeFeature],
  )

  const params = useMemo<PreviewParams>(
    () => ({
      timeframe: { type: timeframeKind, unit: timeframeKind === 'day' ? 1 : timeframeUnit },
      fractal: { n: fractalN },
      ma_windows: LAB_MA_LINES.map((line) => line.window),
      features: featureColumns,
      sample: { max_len: maxLen },
      labeling: {
        mode: labelMode,
        ignore_rule: ignoreRuleOn ? 'ma20<ma120' : 'none',
      },
      filters: {
        ma_alignment: maAlignment === '' ? null : maAlignment,
        min_amount: minAmountInput.trim() === '' ? null : Number(minAmountInput),
      },
    }),
    [
      featureColumns,
      fractalN,
      ignoreRuleOn,
      labelMode,
      maAlignment,
      maxLen,
      minAmountInput,
      timeframeKind,
      timeframeUnit,
    ],
  )

  const visibleIndicators = useMemo<VisibleIndicators>(
    () => ({
      movingAverages: LAB_MA_LINES.map((line) => ({
        window: String(line.window),
        color: line.color,
        lineWidth: 1,
      })),
      volume: false,
    }),
    [],
  )

  const selectedSymbolLabel = useMemo(() => {
    if (!selectedSymbol) return ''
    const name = watchlist.find((item) => item.symbol === selectedSymbol)?.name
    return name ? `${name} • ${selectedSymbol}` : selectedSymbol
  }, [selectedSymbol, watchlist])

  const highlightRange = useMemo<TimeRange | null>(
    () =>
      selectedSample
        ? { from: selectedSample.start_time, to: selectedSample.end_time }
        : null,
    [selectedSample],
  )

  useEffect(() => {
    api
      .watchlist()
      .then((items) => {
        setWatchlist(items)
        setSelectedSymbol((current) => current || items[0]?.symbol || '')
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    if (!selectedSymbol) return
    const seq = ++requestSeqRef.current
    setLoading(true)
    const timer = setTimeout(async () => {
      try {
        const next = await api.preprocessPreview(selectedSymbol, params)
        if (seq !== requestSeqRef.current) return
        setPrevStats(lastStatsRef.current)
        lastStatsRef.current = next.stats
        setPreview(next)
        setSelectedSample(null)
        setError(null)
      } catch (e) {
        if (seq !== requestSeqRef.current) return
        setPreview(null)
        setSelectedSample(null)
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (seq === requestSeqRef.current) setLoading(false)
      }
    }, PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [params, selectedSymbol])

  const selectSampleByTime = useCallback(
    (time: string | number) => {
      if (!preview) return
      const matches = preview.samples.filter((sample) => sample.end_time === time)
      if (matches.length === 0) return
      setSelectedSample((current) => {
        // 같은 봉 재클릭 시 고점/저점 등 겹친 샘플을 순환 선택
        const index = current ? matches.findIndex((m) => m.index === current.index) : -1
        return matches[(index + 1) % matches.length]
      })
    },
    [preview],
  )

  const moveSample = useCallback(
    (delta: number) => {
      if (!preview || preview.samples.length === 0) return
      setSelectedSample((current) => {
        const position = current
          ? preview.samples.findIndex((sample) => sample.index === current.index)
          : -1
        const next =
          position === -1
            ? delta > 0
              ? 0
              : preview.samples.length - 1
            : (position + delta + preview.samples.length) % preview.samples.length
        return preview.samples[next]
      })
    },
    [preview],
  )

  function toggleFeatureWindow(window: number) {
    setFeatureWindows((current) =>
      current.includes(window)
        ? current.filter((item) => item !== window)
        : [...current, window],
    )
  }

  const stats = preview?.stats ?? null
  const classCount = (label: number) => stats?.class_counts?.[String(label)] ?? 0
  const prevClassCount = (label: number) => prevStats?.class_counts?.[String(label)]

  return (
    <>
      <aside className="side-panel lab-symbols">
        <section className="control-section grow">
          <h2>종목</h2>
          <div className="watch-table">
            {watchlist.length === 0 ? (
              <p className="empty">종목 & 데이터 탭에서 종목을 먼저 추가하세요.</p>
            ) : (
              watchlist.map((item) => (
                <div
                  className={item.symbol === selectedSymbol ? 'watch-row selected' : 'watch-row'}
                  key={item.symbol}
                >
                  <button
                    className="watch-main"
                    onClick={() => setSelectedSymbol(item.symbol)}
                    type="button"
                  >
                    <strong>{item.name || item.symbol}</strong>
                    <span>{item.symbol}</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </section>
      </aside>

      <section className="chart-panel">
        <div className="chart-toolbar">
          <div>
            <h2>{selectedSymbolLabel ? `${selectedSymbolLabel} 전처리 미리보기` : '종목을 선택하세요'}</h2>
            <span>
              {timeframe} · 프랙탈 n={fractalN} · max_len={maxLen}
              {loading ? ' · 계산 중...' : ''}
            </span>
          </div>
        </div>
        {error && (
          <p className="error">
            오류: {error}
            {error.includes('404') ? ' — 종목 & 데이터 탭에서 해당 타임프레임을 먼저 수집하세요.' : ''}
          </p>
        )}
        <div className="chart-area">
          {preview ? (
            <CandleChart
              candles={preview.candles}
              highlightRange={highlightRange}
              ma={preview.ma}
              markers={preview.markers}
              onTimeClick={selectSampleByTime}
              visibleIndicators={visibleIndicators}
              volumes={preview.volumes}
            />
          ) : (
            <p className="empty">{loading ? '계산 중...' : '미리보기 결과가 없습니다.'}</p>
          )}
        </div>
        {stats && (
          <div className="lab-stats-bar">
            <div className="lab-stat">
              <strong>샘플 {stats.samples.toLocaleString()}</strong>
              {diffText(stats.samples, prevStats?.samples) && (
                <em>{diffText(stats.samples, prevStats?.samples)}</em>
              )}
            </div>
            <div className="lab-stat low">
              <strong>저점(0) {classCount(0).toLocaleString()}</strong>
              {diffText(classCount(0), prevClassCount(0)) && (
                <em>{diffText(classCount(0), prevClassCount(0))}</em>
              )}
            </div>
            <div className="lab-stat high">
              <strong>고점(1) {classCount(1).toLocaleString()}</strong>
              {diffText(classCount(1), prevClassCount(1)) && (
                <em>{diffText(classCount(1), prevClassCount(1))}</em>
              )}
            </div>
            <div className="lab-stat ignore">
              <strong>무시(2) {classCount(2).toLocaleString()}</strong>
              {diffText(classCount(2), prevClassCount(2)) && (
                <em>{diffText(classCount(2), prevClassCount(2))}</em>
              )}
            </div>
            <div className="lab-stat muted">
              <span>
                라벨 지점 {stats.points.toLocaleString()} · NaN 제외 {stats.dropped_nan.toLocaleString()}
                {stats.dropped_filters > 0 ? ` · 필터 제외 ${stats.dropped_filters.toLocaleString()}` : ''}
                {stats.dropped_ignore > 0 ? ` · 역배열 제외 ${stats.dropped_ignore.toLocaleString()}` : ''}
              </span>
              <span>미확정: 마지막 {stats.confirmation_lag}봉 (미래 확인 대기)</span>
            </div>
          </div>
        )}
        {preview && (
          <div className="lab-sample-bar">
            <button onClick={() => moveSample(-1)} type="button">
              ◀ 이전 샘플
            </button>
            <button onClick={() => moveSample(1)} type="button">
              다음 샘플 ▶
            </button>
            {selectedSample ? (
              <span>
                #{selectedSample.index} · {LABEL_TEXT[selectedSample.label]} ·{' '}
                {selectedSample.kind === 'low' ? '프랙탈 저점' : '프랙탈 고점'} · 윈도우{' '}
                {selectedSample.length}봉 ({String(selectedSample.start_time)} ~{' '}
                {String(selectedSample.end_time)})
              </span>
            ) : (
              <span className="muted-text">
                마커가 있는 봉을 클릭하면 해당 샘플의 입력 윈도우가 하이라이트됩니다.
              </span>
            )}
          </div>
        )}
      </section>

      <aside className="side-panel lab-params">
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
                onChange={(event) => setTimeframeUnit(Number(event.target.value))}
                value={timeframeUnit}
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
          <h2>프랙탈 파라미터</h2>
          <label className="field">
            fractal n (center window)
            <input
              min={3}
              onChange={(event) => setFractalN(Math.max(3, Number(event.target.value) || 3))}
              type="number"
              value={fractalN}
            />
          </label>
          <p className="hint">
            확정에 미래 {Math.floor((fractalN - 1) / 2)}봉이 필요합니다. 마지막{' '}
            {Math.floor((fractalN - 1) / 2)}봉은 라벨되지 않습니다.
          </p>
          <label className="field">
            max_len (입력 윈도우 봉 수)
            <input
              min={1}
              onChange={(event) => setMaxLen(Math.max(1, Number(event.target.value) || 1))}
              type="number"
              value={maxLen}
            />
          </label>
        </section>

        <section className="control-section">
          <h2>라벨 모드</h2>
          <label className="field">
            모드
            <select
              onChange={(event) => setLabelMode(event.target.value as 'cls3' | 'cls2_drop')}
              value={labelMode}
            >
              <option value="cls3">cls3 — 역배열을 무시(2)로 라벨</option>
              <option value="cls2_drop">cls2_drop — 역배열 샘플 제외</option>
            </select>
          </label>
          <label className="inline-check">
            <input
              checked={ignoreRuleOn}
              onChange={(event) => setIgnoreRuleOn(event.target.checked)}
              type="checkbox"
            />
            역배열(MA20&lt;MA120) 무시 규칙 적용
          </label>
        </section>

        <section className="control-section">
          <h2>필터</h2>
          <label className="field">
            정배열 필터
            <select
              onChange={(event) =>
                setMaAlignment(event.target.value as '' | '20>120' | '5>20>120')
              }
              value={maAlignment}
            >
              <option value="">사용 안 함</option>
              <option value="20>120">20 &gt; 120</option>
              <option value="5>20>120">5 &gt; 20 &gt; 120</option>
            </select>
          </label>
          <label className="field">
            최소 거래대금 (원)
            <input
              min={0}
              onChange={(event) => setMinAmountInput(event.target.value)}
              placeholder="미입력 시 미적용"
              type="number"
              value={minAmountInput}
            />
          </label>
        </section>

        <section className="control-section">
          <h2>학습 피처</h2>
          <p className="hint">Open/High/Low/Close는 항상 포함됩니다.</p>
          {LAB_MA_LINES.map((line) => (
            <label className="inline-check" key={line.window}>
              <input
                checked={featureWindows.includes(line.window)}
                onChange={() => toggleFeatureWindow(line.window)}
                type="checkbox"
              />
              <strong style={{ color: line.color }}>MA{line.window}</strong>
            </label>
          ))}
          <label className="inline-check">
            <input
              checked={volumeFeature}
              onChange={(event) => setVolumeFeature(event.target.checked)}
              type="checkbox"
            />
            거래량 (Volume)
          </label>
          <div className="feature-preview lab-feature-preview">
            <strong>features</strong>
            <span>{(preview?.features.columns ?? featureColumns).join(', ')}</span>
            <em>입력 차원 {(preview?.features.columns ?? featureColumns).length}</em>
          </div>
        </section>
      </aside>
    </>
  )
}
