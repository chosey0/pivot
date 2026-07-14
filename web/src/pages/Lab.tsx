import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  type CleaningMode,
  type FractalTiePolicy,
  type PresetJson,
  type PreviewParams,
  type PreviewResponse,
  type SamplePairing,
  type TimeframeCode,
  type PreviewSample,
  type PreviewStats,
  type WatchItem,
} from '../api/client'
import type { TimeRange, VisibleIndicators } from '../components/chart/CandleChart'
import { CandleChart } from '../components/chart/CandleChart'
import { ChartPanel } from '../components/chart/ChartPanel'
import { formatDateTime } from '../lib/format'
import { fromTimeframeCode } from '../lib/timeframe'
import { timeframeLabel, watchItemKey } from '../lib/watchlist'

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

function diffText(current: number, previous: number | undefined) {
  if (previous === undefined || previous === current) return null
  const delta = current - previous
  return `${previous.toLocaleString()} → ${current.toLocaleString()} (${delta > 0 ? '+' : ''}${delta.toLocaleString()})`
}

function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

export function Lab() {
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [selectedKey, setSelectedKey] = useState('')
  const [fractalByTimeframe, setFractalByTimeframe] = useState<
    Partial<Record<TimeframeCode, number>>
  >({})
  const [fractalInputByTimeframe, setFractalInputByTimeframe] = useState<
    Partial<Record<TimeframeCode, string>>
  >({})
  const [tiePolicy, setTiePolicy] = useState<FractalTiePolicy>('plateau_last')
  const [labelMode, setLabelMode] = useState<'cls3' | 'cls2_drop'>('cls3')
  const [samplePairing, setSamplePairing] = useState<SamplePairing>('adjacent_markers_v1')
  const [ignoreRuleOn, setIgnoreRuleOn] = useState(false)
  const [ignoreSwingPctInput, setIgnoreSwingPctInput] = useState('')
  const [maAlignment, setMaAlignment] = useState<'' | '20>120' | '5>20>120'>('')
  const [minAmountInput, setMinAmountInput] = useState('')
  const [cleaningMode, setCleaningMode] = useState<CleaningMode>('report_only')
  const [featureWindows, setFeatureWindows] = useState<number[]>([5, 20, 60, 120])
  const [volumeFeature, setVolumeFeature] = useState(true)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [prevStats, setPrevStats] = useState<PreviewStats | null>(null)
  const [selectedSample, setSelectedSample] = useState<PreviewSample | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [presetName, setPresetName] = useState('')
  const [presetSaving, setPresetSaving] = useState(false)
  const [presetStatus, setPresetStatus] = useState<string | null>(null)
  const lastStatsRef = useRef<PreviewStats | null>(null)
  const requestSeqRef = useRef(0)

  const selectedItem = useMemo(
    () => watchlist.find((item) => watchItemKey(item) === selectedKey) ?? null,
    [selectedKey, watchlist],
  )
  const timeframe = selectedItem?.timeframe ?? 'day'
  const timeframeConfig = useMemo(() => fromTimeframeCode(timeframe), [timeframe])
  const fractalN = fractalByTimeframe[timeframe] ?? 3
  const fractalNInput = fractalInputByTimeframe[timeframe] ?? ''

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
      timeframe: timeframeConfig,
      fractal: { n: fractalN, tie_policy: tiePolicy },
      fractal_windows: { ...fractalByTimeframe, [timeframe]: fractalN },
      ma_windows: LAB_MA_LINES.map((line) => line.window),
      features: featureColumns,
      labeling: {
        mode: labelMode,
        sample_pairing: samplePairing,
        ignore_rule: ignoreRuleOn ? 'ma20<ma120' : 'none',
        ignore_swing_pct:
          ignoreSwingPctInput.trim() === '' ? null : Number(ignoreSwingPctInput),
      },
      filters: {
        ma_alignment: maAlignment === '' ? null : maAlignment,
        min_amount: minAmountInput.trim() === '' ? null : Number(minAmountInput),
      },
      cleaning: {
        mode: cleaningMode,
        policy: 'kronos_adapted_v1',
        price_jump_threshold: null,
        max_illiquid_bars: null,
        max_stagnant_bars: null,
        min_segment_bars: null,
      },
    }),
    [
      cleaningMode,
      featureColumns,
      fractalByTimeframe,
      fractalN,
      ignoreRuleOn,
      ignoreSwingPctInput,
      labelMode,
      maAlignment,
      minAmountInput,
      samplePairing,
      timeframe,
      timeframeConfig,
      tiePolicy,
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
    if (!selectedItem) return ''
    const symbol = selectedItem.name
      ? `${selectedItem.name} • ${selectedItem.symbol}`
      : selectedItem.symbol
    return `${symbol} • ${timeframeLabel(selectedItem.timeframe)}`
  }, [selectedItem])

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
      .then(setWatchlist)
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    setSelectedKey((current) =>
      watchlist.some((item) => watchItemKey(item) === current)
        ? current
        : watchlist[0]
          ? watchItemKey(watchlist[0])
          : '',
    )
  }, [watchlist])

  useEffect(() => {
    requestSeqRef.current += 1
    setPreview(null)
    setSelectedSample(null)
    setPrevStats(null)
    lastStatsRef.current = null
  }, [selectedKey])

  useEffect(() => {
    if (!selectedItem) return
    const seq = ++requestSeqRef.current
    setLoading(true)
    const timer = setTimeout(async () => {
      try {
        const next = await api.preprocessPreview(selectedItem, params)
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
  }, [params, selectedItem])

  const selectSampleByTime = useCallback(
    (time: string | number) => {
      if (!preview) return
      const sampleIndexes = preview.markers
        .filter((marker) => marker.time === time && marker.incoming_sample_index !== null)
        .map((marker) => marker.incoming_sample_index as number)
      if (sampleIndexes.length === 0) return
      setSelectedSample((current) => {
        const index = current ? sampleIndexes.indexOf(current.index) : -1
        const sampleIndex = sampleIndexes[(index + 1) % sampleIndexes.length]
        return preview.samples[sampleIndex] ?? null
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

  useEffect(() => {
    if (!selectedSample) return

    function handleKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || isEditableTarget(event.target)) return
      if (event.key === 'ArrowLeft') {
        event.preventDefault()
        moveSample(-1)
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault()
        moveSample(1)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [moveSample, selectedSample])

  async function savePreset() {
    const name = presetName.trim()
    if (!name || presetSaving) return
    setPresetSaving(true)
    setPresetStatus(null)
    try {
      // 차트 표시 지표와 학습 피처 선택을 프리셋에 함께 보존한다 (docs/04 §2)
      const preset: PresetJson = {
        ...params,
        name,
        chart_indicators: {
          preset: name,
          moving_averages: LAB_MA_LINES.map((line) => ({
            window: line.window,
            color: line.color,
            line_width: 1,
            chart: true,
            feature: featureWindows.includes(line.window),
          })),
          volume: { chart: false, feature: volumeFeature },
        },
      }
      // 같은 이름이 있으면 새 버전, 없으면 신규 생성 (목록은 name asc, version desc 정렬)
      const sameName = (await api.presets()).filter((row) => row.name === name)
      const saved =
        sameName.length > 0
          ? await api.createPresetVersion(sameName[0].id, preset)
          : await api.createPreset(preset)
      setPresetStatus(`'${saved.name}' v${saved.version} 저장 완료 — 데이터셋 탭에서 사용할 수 있습니다.`)
    } catch (e) {
      setPresetStatus(`저장 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setPresetSaving(false)
    }
  }

  function toggleFeatureWindow(window: number) {
    setFeatureWindows((current) =>
      current.includes(window)
        ? current.filter((item) => item !== window)
        : [...current, window],
    )
  }

  const stats = preview?.stats ?? null
  const chartMarkers = useMemo(
    () =>
      preview?.markers.map((marker) => ({
        ...marker,
        label: marker.incoming_sample_label ?? marker.label,
      })) ?? [],
    [preview],
  )
  const classCount = (label: number) => stats?.class_counts?.[String(label)] ?? 0
  const prevClassCount = (label: number) => prevStats?.class_counts?.[String(label)]
  const previewError = error
    ? `${error}${error.includes('404') ? ' — 종목 & 데이터 탭에서 해당 타임프레임을 먼저 수집하세요.' : ''}`
    : null

  return (
    <>
      <aside className="side-panel lab-symbols">
        <section className="control-section grow">
          <h2>종목</h2>
          <div className="watch-table">
            {watchlist.length === 0 ? (
              <p className="empty">종목 & 데이터 탭에서 수집 항목을 먼저 추가하세요.</p>
            ) : (
              watchlist.map((item) => {
                const key = watchItemKey(item)
                return (
                <div
                  className={key === selectedKey ? 'watch-row selected' : 'watch-row'}
                  key={key}
                >
                  <button
                    className="watch-main"
                    onClick={() => setSelectedKey(key)}
                    type="button"
                  >
                    <strong>
                      {item.name || item.symbol} - {timeframeLabel(item.timeframe)}
                    </strong>
                    <span>
                      {item.symbol} · {item.region === 'overseas' ? item.exchange : 'KRX'}
                    </span>
                    <small className="data-range">
                      {item.start || item.end
                        ? `${item.start ? formatDateTime(item.start) : '처음'} ~ ${item.end ? formatDateTime(item.end) : '최신'}`
                        : '전체 수집 범위'}
                    </small>
                  </button>
                </div>
                )
              })
            )}
          </div>
        </section>
      </aside>

      <ChartPanel
        emptyText="미리보기 결과가 없습니다."
        error={previewError}
        hasContent={Boolean(preview)}
        loading={loading}
        loadingText="계산 중..."
        subtitle={
          <>
            {timeframe} · 프랙탈 n={fractalN} ·{' '}
            {tiePolicy === 'plateau_last' ? '동률 마지막 봉' : '동률 전체 봉'} ·{' '}
            {samplePairing === 'adjacent_markers_v1' ? '인접 마커' : '최근 반대 마커'}
            {loading ? ' · 계산 중...' : ''}
          </>
        }
        title={selectedSymbolLabel ? `${selectedSymbolLabel} 전처리 미리보기` : '종목을 선택하세요'}
        footer={
          <>
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
                    {stats.dropped_unpaired > 0 ? ` · 짝 없음 제외 ${stats.dropped_unpaired.toLocaleString()}` : ''}
                    {stats.dropped_filters > 0 ? ` · 필터 제외 ${stats.dropped_filters.toLocaleString()}` : ''}
                    {stats.dropped_ignore > 0 ? ` · 무시 샘플 제외 ${stats.dropped_ignore.toLocaleString()}` : ''}
                    {stats.swing_ignored > 0 ? ` · 스윙 무시 ${stats.swing_ignored.toLocaleString()}` : ''}
                  </span>
                  <span>미확정: 마지막 {stats.confirmation_lag}봉 (미래 확인 대기)</span>
                  <span>
                    페어링 {stats.pairing_stats.rule} · edge{' '}
                    {stats.pairing_stats.adjacent_edges.toLocaleString()} · label2 제외{' '}
                    {stats.pairing_stats.dropped_label2.toLocaleString()}
                  </span>
                  <span>
                    동률 정규화 {stats.overlap_clusters.dropped_plateau_points.toLocaleString()}개 제거
                    {' · '}잔여 overlap cluster{' '}
                    {stats.overlap_clusters.sample_clusters.toLocaleString()}개
                    {stats.overlap_clusters.redundant_samples > 0
                      ? ` (중복 추정 ${stats.overlap_clusters.redundant_samples.toLocaleString()}개)`
                      : ''}
                  </span>
                  <span>
                    클리닝 {stats.cleaning.mode} · 후보 유지{' '}
                    {stats.cleaning.retained_bars.toLocaleString()}/
                    {stats.cleaning.original_bars.toLocaleString()}봉 · 구간{' '}
                    {stats.cleaning.segments.toLocaleString()} · 경계{' '}
                    {stats.cleaning.structural_breaks.toLocaleString()}
                  </span>
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
                    {String(selectedSample.end_time)}) · ←/→ 이동
                  </span>
                ) : (
                  <span className="muted-text">
                    마커가 있는 봉을 클릭하면 선택한 페어링 전략의 입력 윈도우가 하이라이트됩니다.
                  </span>
                )}
              </div>
            )}
          </>
        }
      >
        {preview ? (
          <CandleChart
            candles={preview.candles}
            fitContentKey={`${selectedKey}:${preview.stats.cleaning.mode}`}
            highlightRange={highlightRange}
            ma={preview.ma}
            markers={chartMarkers}
            onTimeClick={selectSampleByTime}
            priceDecimals={selectedItem?.region === 'overseas' ? 2 : 0}
            visibleIndicators={visibleIndicators}
            volumes={preview.volumes}
          />
        ) : null}
      </ChartPanel>

      <aside className="side-panel lab-params">
        <section className="control-section">
          <h2>프랙탈 파라미터 · {timeframeLabel(timeframe)}</h2>
          <label className="field">
            fractal n (center window)
            <input
              inputMode="numeric"
              onChange={(event) => {
                const value = event.target.value.replace(/\D/g, '')
                setFractalInputByTimeframe((current) => ({ ...current, [timeframe]: value }))
                setFractalByTimeframe((current) => ({
                  ...current,
                  [timeframe]: Math.max(3, Number(value) || 3),
                }))
              }}
              onBlur={() => {
                if (fractalNInput !== '' && Number(fractalNInput) < 3) {
                  setFractalInputByTimeframe((current) => ({
                    ...current,
                    [timeframe]: '3',
                  }))
                  setFractalByTimeframe((current) => ({ ...current, [timeframe]: 3 }))
                }
              }}
              pattern="[0-9]*"
              placeholder="미입력 시 최소값 3 적용"
              type="text"
              value={fractalNInput}
            />
          </label>
          <p className="hint">fractal n만 타임프레임별로 저장됩니다.</p>
          <p className="hint">
            확정에 미래 {Math.floor((fractalN - 1) / 2)}봉이 필요합니다. 마지막{' '}
            {Math.floor((fractalN - 1) / 2)}봉은 라벨되지 않습니다.
          </p>
          <p className="hint">
            같은 가격의 연속 고점·저점은 plateau로 묶고 마지막 봉만 대표 라벨로 사용합니다.
          </p>
          <p className="hint">
            입력 윈도우의 시작 마커는 아래 라벨 모드의 샘플 페어링 전략으로 결정됩니다.
          </p>
        </section>

        <section className="control-section">
          <h2>공통 라벨 설정</h2>
          <label className="field">
            동률 극값 처리
            <select
              onChange={(event) => setTiePolicy(event.target.value as FractalTiePolicy)}
              value={tiePolicy}
            >
              <option value="plateau_last">plateau_last — 마지막 봉만 라벨</option>
              <option value="all">all — 모든 봉 라벨 (기존 방식)</option>
            </select>
          </label>
          <label className="field">
            샘플 페어링
            <select
              onChange={(event) => setSamplePairing(event.target.value as SamplePairing)}
              value={samplePairing}
            >
              <option value="adjacent_markers_v1">adjacent — 바로 이전 마커</option>
              <option value="latest_opposite_v1">legacy — 최근 반대 마커</option>
            </select>
          </label>
          <p className="hint">
            adjacent는 같은 종류의 연속 마커를 무시(2) 샘플로 만들고, 도착 마커를 다음
            샘플의 시작점으로 유지합니다.
          </p>
          <label className="field">
            모드
            <select
              onChange={(event) => setLabelMode(event.target.value as 'cls3' | 'cls2_drop')}
              value={labelMode}
            >
              <option value="cls3">cls3 — 무시 규칙 해당 지점을 2로 라벨</option>
              <option value="cls2_drop">cls2_drop — 무시 규칙 해당 샘플 제외</option>
            </select>
          </label>
          <p className="hint">
            아래 무시 규칙(역배열, 최소 스윙)은 독립적으로 조합됩니다. 예: 역배열을
            끄고 최소 스윙만 설정하면 무시(2)는 잔진동 스윙에서만 생성됩니다.
          </p>
          <label className="inline-check">
            <input
              checked={ignoreRuleOn}
              onChange={(event) => setIgnoreRuleOn(event.target.checked)}
              type="checkbox"
            />
            역배열(MA20&lt;MA120) 무시 규칙 적용
          </label>
          <label className="field">
            최소 스윙 변화율 (%)
            <input
              inputMode="decimal"
              onChange={(event) => {
                if (/^\d*\.?\d*$/.test(event.target.value)) {
                  setIgnoreSwingPctInput(event.target.value)
                }
              }}
              pattern="[0-9]*[.]?[0-9]*"
              placeholder="미입력 시 미적용"
              type="text"
              value={ignoreSwingPctInput}
            />
          </label>
          <p className="hint">
            선택한 pair의 시작 마커와 끝의 가격 변화율이 이 값 미만이면
            잔진동으로 보고 무시(2)로 라벨합니다.
          </p>
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
              inputMode="numeric"
              onChange={(event) => setMinAmountInput(event.target.value.replace(/\D/g, ''))}
              pattern="[0-9]*"
              placeholder="미입력 시 미적용"
              type="text"
              value={minAmountInput}
            />
          </label>
        </section>

        <section className="control-section">
          <h2>데이터 클리닝</h2>
          <label className="field">
            Kronos 적응형 정책
            <select
              onChange={(event) => setCleaningMode(event.target.value as CleaningMode)}
              value={cleaningMode}
            >
              <option value="report_only">진단만 — 샘플 유지</option>
              <option value="filter">필터 적용 — 정상 구간별 재계산</option>
              <option value="off">사용 안 함</option>
            </select>
          </label>
          <p className="hint">
            가격 이상, 구조적 점프, 장기 비유동·정체를 탐지합니다. 필터 적용 시 이동평균,
            프랙탈과 샘플을 각 정상 구간에서 독립 계산합니다.
          </p>
          {timeframeConfig.type === 'tick' && cleaningMode !== 'off' ? (
            <p className="warning">
              틱봉은 논문 주기 기준이 없어 가격 필드 무결성만 자동 검사합니다.
            </p>
          ) : null}
          <a
            className="hint"
            href="https://arxiv.org/abs/2508.02739"
            rel="noreferrer"
            target="_blank"
          >
            Kronos 논문 Appendix B 기준 보기
          </a>
        </section>

        <section className="control-section">
          <h2>학습 피처 (공통)</h2>
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

        <section className="control-section">
          <h2>프리셋 저장</h2>
          <label className="field">
            프리셋 이름
            <input
              onChange={(event) => setPresetName(event.target.value)}
              placeholder="예: day20_ma20120_cls3"
              type="text"
              value={presetName}
            />
          </label>
          <button
            className="primary"
            disabled={presetSaving || presetName.trim() === ''}
            onClick={savePreset}
            type="button"
          >
            {presetSaving ? '저장 중...' : '현재 파라미터를 프리셋으로 저장'}
          </button>
          {presetStatus && <p className="hint">{presetStatus}</p>}
          <p className="hint">
            같은 이름으로 저장하면 새 버전이 생성됩니다. 일괄 적용은 데이터셋 탭에서 실행합니다.
          </p>
        </section>
      </aside>
    </>
  )
}
