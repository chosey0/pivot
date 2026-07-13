import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type DatasetRow,
  type JobRow,
  type PresetRow,
  type SampleDetail,
  type SampleListResponse,
  type WatchItem,
} from '../api/client'
import { MiniSampleChart } from '../components/chart/MiniSampleChart'

const DATASET_STATUS_TEXT: Record<string, string> = {
  building: '생성 중',
  ready: '완료',
  failed: '실패',
}

const LABEL_TEXT: Record<number, string> = { 0: '저점', 1: '고점', 2: '무시' }
const PAGE_SIZE = 20

interface SymbolProgress {
  status: 'running' | 'succeeded' | 'failed'
  sampleCount?: number
  error?: string
}

function classCountsText(counts: Record<string, number> | undefined) {
  if (!counts || Object.keys(counts).length === 0) return '-'
  return ['0', '1', '2']
    .filter((label) => counts[label] !== undefined)
    .map((label) => `${label}: ${counts[label].toLocaleString()}`)
    .join(' · ')
}

function formatDate(value: string) {
  return value.slice(0, 19).replace('T', ' ')
}

function formatFeatureValue(value: number) {
  return value.toLocaleString('ko-KR', { maximumFractionDigits: 3 })
}

/** 스윙 시작(직전 반대 프랙탈 봉)~끝(현재 프랙탈 봉)의 가격 변화율(%). */
function sampleChangeRate(sample: SampleDetail): number | null {
  const columns = sample.feature_columns
  // 저점 샘플은 직전 고점(High)→현재 저점(Low), 고점 샘플은 그 반대
  const startIndex = columns.indexOf(sample.kind === 'low' ? 'High' : 'Low')
  const endIndex = columns.indexOf(sample.kind === 'low' ? 'Low' : 'High')
  if (startIndex < 0 || endIndex < 0 || sample.features.length < 2) return null
  const start = sample.features[0][startIndex]
  const end = sample.features[sample.features.length - 1][endIndex]
  if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0) return null
  return (end / start - 1) * 100
}

export function Datasets() {
  const [presets, setPresets] = useState<PresetRow[]>([])
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [datasets, setDatasets] = useState<DatasetRow[]>([])
  const [presetsLoading, setPresetsLoading] = useState(true)
  const [datasetsLoading, setDatasetsLoading] = useState(true)
  const [selectedPresetId, setSelectedPresetId] = useState<number | null>(null)
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([])
  const [datasetName, setDatasetName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [job, setJob] = useState<JobRow | null>(null)
  const [symbolProgress, setSymbolProgress] = useState<Record<string, SymbolProgress>>({})
  const eventSourceRef = useRef<EventSource | null>(null)
  const sampleListRef = useRef<HTMLDivElement | null>(null)

  // 샘플 브라우저 상태 — position은 라벨 필터 적용 후 목록에서의 순번
  const [browse, setBrowse] = useState<{ datasetId: number; name: string } | null>(null)
  const [labelFilter, setLabelFilter] = useState<number | null>(null)
  const [pageOffset, setPageOffset] = useState(0)
  const [page, setPage] = useState<SampleListResponse | null>(null)
  const [position, setPosition] = useState<number | null>(null)
  const [selectedSample, setSelectedSample] = useState<SampleDetail | null>(null)
  const [sampleError, setSampleError] = useState<string | null>(null)
  const [samplesLoading, setSamplesLoading] = useState(false)
  const [sampleDetailLoading, setSampleDetailLoading] = useState(false)

  const refreshPresets = useCallback(() => {
    setPresetsLoading(true)
    api
      .presets()
      .then((rows) => {
        setPresets(rows)
        setSelectedPresetId((current) =>
          current && rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null,
        )
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setPresetsLoading(false))
  }, [])

  const refreshDatasets = useCallback(() => {
    setDatasetsLoading(true)
    api
      .datasets()
      .then(setDatasets)
      .catch((e: Error) => setError(e.message))
      .finally(() => setDatasetsLoading(false))
  }, [])

  useEffect(() => {
    refreshPresets()
    refreshDatasets()
    api
      .watchlist()
      .then((items) => {
        setWatchlist(items)
        setSelectedSymbols(items.map((item) => item.symbol))
      })
      .catch((e: Error) => setError(e.message))
    return () => eventSourceRef.current?.close()
  }, [refreshDatasets, refreshPresets])

  function toggleSymbol(symbol: string) {
    setSelectedSymbols((current) =>
      current.includes(symbol)
        ? current.filter((item) => item !== symbol)
        : [...current, symbol],
    )
  }

  async function archivePreset(preset: PresetRow) {
    try {
      await api.archivePreset(preset.id)
      setMessage(`프리셋 '${preset.name} v${preset.version}'을 보관 처리했습니다.`)
      refreshPresets()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function removePreset(preset: PresetRow) {
    if (
      !window.confirm(
        `프리셋 '${preset.name} v${preset.version}'을 영구 삭제할까요? 참조 중인 데이터셋이 있으면 삭제되지 않습니다.`,
      )
    )
      return
    setError(null)
    try {
      await api.deletePreset(preset.id)
      setMessage(`프리셋 '${preset.name} v${preset.version}'을 삭제했습니다.`)
      refreshPresets()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function watchJob(jobId: number) {
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/jobs/${jobId}/events`)
    eventSourceRef.current = source

    source.addEventListener('job', (event) => {
      const next = JSON.parse((event as MessageEvent).data) as JobRow
      setJob(next)
      if (next.status !== 'queued' && next.status !== 'running') {
        source.close()
        refreshDatasets()
      }
    })
    source.addEventListener('symbol_started', (event) => {
      const { symbol } = JSON.parse((event as MessageEvent).data) as { symbol: string }
      setSymbolProgress((current) => ({ ...current, [symbol]: { status: 'running' } }))
    })
    source.addEventListener('symbol_succeeded', (event) => {
      const data = JSON.parse((event as MessageEvent).data) as {
        symbol: string
        sample_count: number
      }
      setSymbolProgress((current) => ({
        ...current,
        [data.symbol]: { status: 'succeeded', sampleCount: data.sample_count },
      }))
    })
    source.addEventListener('symbol_failed', (event) => {
      const data = JSON.parse((event as MessageEvent).data) as {
        symbol: string
        error: string
      }
      setSymbolProgress((current) => ({
        ...current,
        [data.symbol]: { status: 'failed', error: data.error },
      }))
    })
    source.addEventListener('dataset_ready', () => {
      setMessage('데이터셋 생성이 완료되었습니다.')
    })
    source.addEventListener('dataset_failed', (event) => {
      const data = JSON.parse((event as MessageEvent).data) as { message: string }
      setError(data.message)
    })
    source.addEventListener('job_cancelled', () => {
      setMessage('일괄 전처리가 취소되었습니다.')
    })
    source.onerror = () => {
      // 실행 중 연결 오류는 EventSource 기본 재연결에 맡긴다. 서버가 보낸 event id를
      // Last-Event-ID로 전달하므로 이미 처리한 durable 이벤트는 다시 받지 않는다.
      api
        .job(jobId)
        .then((next) => {
          setJob(next)
          if (next.status !== 'queued' && next.status !== 'running') {
            source.close()
            refreshDatasets()
          }
        })
        .catch(() => undefined)
    }
  }

  async function startBatch() {
    if (!selectedPresetId || selectedSymbols.length === 0 || !datasetName.trim()) return
    setError(null)
    setMessage(null)
    setSymbolProgress({})
    try {
      const { job_id } = await api.preprocessBatch(
        selectedPresetId,
        datasetName.trim(),
        selectedSymbols,
      )
      const started = await api.job(job_id)
      setJob(started)
      setDatasetName('')
      refreshDatasets()
      watchJob(job_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function cancelRunningJob() {
    if (!job) return
    try {
      await api.cancelJob(job.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function removeDataset(dataset: DatasetRow) {
    if (
      !window.confirm(
        `데이터셋 '${dataset.name}'의 Storage 객체와 메타데이터를 모두 삭제할까요?`,
      )
    )
      return
    setError(null)
    try {
      await api.deleteDataset(dataset.id)
      setMessage(`데이터셋 '${dataset.name}'을 삭제했습니다.`)
      if (browse?.datasetId === dataset.id) closeBrowser()
      refreshDatasets()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      refreshDatasets()
    }
  }

  // ── 샘플 브라우저 ──────────────────────────────────────────────

  function openBrowser(dataset: DatasetRow) {
    setBrowse({ datasetId: dataset.id, name: dataset.name })
    setLabelFilter(null)
    setPageOffset(0)
    setPage(null)
    setPosition(null)
    setSelectedSample(null)
    setSampleError(null)
    setSampleDetailLoading(false)
  }

  function closeBrowser() {
    setBrowse(null)
    setPage(null)
    setPosition(null)
    setSelectedSample(null)
    setSampleError(null)
    setSampleDetailLoading(false)
  }

  function changeLabelFilter(value: string) {
    setLabelFilter(value === 'all' ? null : Number(value))
    setPageOffset(0)
    setPosition(null)
    setSelectedSample(null)
  }

  useEffect(() => {
    if (!browse) return
    let stale = false
    setSamplesLoading(true)
    api
      .datasetSamples(browse.datasetId, {
        label: labelFilter,
        offset: pageOffset,
        limit: PAGE_SIZE,
      })
      .then((response) => {
        if (stale) return
        setPage(response)
        setPosition((current) => current ?? (response.items.length > 0 ? response.offset : null))
        setSampleError(null)
      })
      .catch((e: Error) => {
        if (!stale) setSampleError(e.message)
      })
      .finally(() => {
        if (!stale) setSamplesLoading(false)
      })
    return () => {
      stale = true
    }
  }, [browse, labelFilter, pageOffset])

  useEffect(() => {
    if (!browse || position === null || !page) return
    const item = page.items[position - page.offset]
    if (!item) return // 선택 위치의 페이지가 아직 로드되지 않음
    let stale = false
    setSampleDetailLoading(true)
    api
      .datasetSample(browse.datasetId, item.index)
      .then((detail) => {
        if (!stale) setSelectedSample(detail)
      })
      .catch((e: Error) => {
        if (!stale) setSampleError(e.message)
      })
      .finally(() => {
        if (!stale) setSampleDetailLoading(false)
      })
    return () => {
      stale = true
    }
  }, [browse, position, page])

  const moveSelection = useCallback(
    (nextPosition: number) => {
      const total = page?.total ?? 0
      if (total === 0 || nextPosition < 0 || nextPosition >= total) return
      // 같은 위치 재클릭 시 상세를 비우면 재조회 effect가 돌지 않아 빈 화면이 된다
      if (nextPosition === position) return
      setSelectedSample(null)
      setPosition(nextPosition)
      const nextOffset = Math.floor(nextPosition / PAGE_SIZE) * PAGE_SIZE
      setPageOffset((current) => (current === nextOffset ? current : nextOffset))
    },
    [page, position],
  )

  const showPage = useCallback((nextOffset: number) => {
    setSelectedSample(null)
    setPosition(nextOffset)
    setPageOffset(nextOffset)
  }, [])

  useEffect(() => {
    if (!browse) return
    const handler = (event: KeyboardEvent) => {
      const { key } = event
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(key)) return
      const target = event.target as HTMLElement
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)) return
      event.preventDefault()
      if (key === 'ArrowUp' || key === 'ArrowDown') {
        // 상하: 샘플 선택 이동
        const delta = key === 'ArrowUp' ? -1 : 1
        moveSelection(position === null ? 0 : position + delta)
        return
      }
      // 좌우: 페이지 이동 (pager 버튼과 동일 조건)
      if (samplesLoading) return
      if (key === 'ArrowLeft' && pageOffset > 0) {
        showPage(Math.max(pageOffset - PAGE_SIZE, 0))
      } else if (key === 'ArrowRight' && page && pageOffset + PAGE_SIZE < page.total) {
        showPage(pageOffset + PAGE_SIZE)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [browse, position, moveSelection, page, pageOffset, samplesLoading, showPage])

  // 선택 샘플이 리스트 뷰포트를 벗어나면 자동 스크롤 (block: nearest — 필요할 때만)
  useEffect(() => {
    if (position === null) return
    sampleListRef.current
      ?.querySelector('.sample-item.selected')
      ?.scrollIntoView({ block: 'nearest' })
  }, [position, page])

  function pickRandomSample() {
    const total = page?.total ?? 0
    if (total > 0) moveSelection(Math.floor(Math.random() * total))
  }

  const jobRunning = job !== null && (job.status === 'queued' || job.status === 'running')
  const sampleRate = selectedSample ? sampleChangeRate(selectedSample) : null
  const selectedPreset = presets.find((preset) => preset.id === selectedPresetId) ?? null
  const totalPages = page ? Math.max(Math.ceil(page.total / PAGE_SIZE), 1) : 1
  const currentPage = Math.floor(pageOffset / PAGE_SIZE) + 1

  return (
    <>
      {!browse && (
        <aside className="side-panel lab-symbols">
          <section className="control-section grow">
          <div className="section-title-row">
            <h2>프리셋</h2>
            <button onClick={refreshPresets} type="button">
              새로고침
            </button>
          </div>
          {presetsLoading && presets.length === 0 ? (
            <p className="hint">프리셋을 불러오는 중...</p>
          ) : presets.length === 0 ? (
            <p className="empty">
              전처리 실험실에서 파라미터를 튜닝한 뒤 프리셋으로 저장하세요.
            </p>
          ) : (
            <div className="watch-table">
              {presets.map((preset) => (
                <div
                  className={preset.id === selectedPresetId ? 'watch-row selected' : 'watch-row'}
                  key={preset.id}
                >
                  <button
                    className="watch-main"
                    onClick={() => setSelectedPresetId(preset.id)}
                    type="button"
                  >
                    <strong>
                      {preset.name} <em className="preset-version">v{preset.version}</em>
                    </strong>
                    <span>
                      {preset.preset.timeframe.type === 'day'
                        ? 'day'
                        : `${preset.preset.timeframe.type}${preset.preset.timeframe.unit}`}
                      {' · '}n={preset.preset.fractal.n}
                      {' · '}
                      {formatDate(preset.created_at)}
                    </span>
                  </button>
                  <span className="row-actions">
                    <button
                      className="ghost"
                      onClick={() => archivePreset(preset)}
                      title="보관 (batch 대상에서 제외)"
                      type="button"
                    >
                      보관
                    </button>
                    <button
                      className="ghost danger"
                      onClick={() => removePreset(preset)}
                      title="참조 데이터셋이 없는 프리셋만 영구 삭제할 수 있습니다"
                      type="button"
                    >
                      삭제
                    </button>
                  </span>
                </div>
              ))}
            </div>
          )}
          {selectedPreset && (
            <div className="feature-preview">
              <strong>features</strong>
              <span>{selectedPreset.preset.features.join(', ')}</span>
              <em>
                라벨 모드 {selectedPreset.preset.labeling.mode} · 필터{' '}
                {selectedPreset.preset.filters.ma_alignment ?? '없음'}
              </em>
            </div>
          )}
          </section>
        </aside>
      )}

      <section className={browse ? 'datasets-main reviewing' : 'datasets-main'}>
        {error ? <p className="error">오류: {error}</p> : null}
        {message && !error ? <p className="message">{message}</p> : null}

        {!browse && (
          <>
            <section className="control-section datasets-launch">
          <h2>일괄 전처리 실행</h2>
          <div className="batch-form">
            <label className="field">
              데이터셋 이름
              <input
                onChange={(event) => setDatasetName(event.target.value)}
                placeholder="예: day20-cls3-2026q3"
                type="text"
                value={datasetName}
              />
            </label>
            <div className="field">
              대상 종목 ({selectedSymbols.length}/{watchlist.length})
              <div className="batch-symbols">
                {watchlist.length === 0 ? (
                  <p className="empty">종목 & 데이터 탭에서 종목을 먼저 추가하세요.</p>
                ) : (
                  watchlist.map((item) => (
                    <label className="inline-check" key={item.symbol}>
                      <input
                        checked={selectedSymbols.includes(item.symbol)}
                        onChange={() => toggleSymbol(item.symbol)}
                        type="checkbox"
                      />
                      {item.name || item.symbol}
                      <span className="muted-text">{item.symbol}</span>
                    </label>
                  ))
                )}
              </div>
            </div>
            <button
              className="primary"
              disabled={
                jobRunning ||
                !selectedPresetId ||
                selectedSymbols.length === 0 ||
                datasetName.trim() === ''
              }
              onClick={startBatch}
              type="button"
            >
              {jobRunning ? '실행 중...' : '일괄 전처리 시작'}
            </button>
            <p className="hint">
              선택한 프리셋을 로컬 캔들 캐시에 적용해 Supabase에 데이터셋을 생성합니다.
              종목 단위 train/validation/test split이 자동 배정됩니다.
            </p>
          </div>
            </section>

            {job && (
          <section className="control-section batch-progress">
            <div className="section-title-row">
              <h2>
                진행 상황 — job #{job.id} ({job.status})
              </h2>
              {jobRunning && (
                <button className="ghost" onClick={cancelRunningJob} type="button">
                  취소
                </button>
              )}
            </div>
            <div className="progress-track">
              <div
                className="progress-fill"
                style={{
                  width: `${job.total_items === 0 ? 0 : Math.round((job.completed_items / job.total_items) * 100)}%`,
                }}
              />
            </div>
            <span className="hint">
              {job.completed_items}/{job.total_items} 종목 처리
              {job.error ? ` · ${job.error}` : ''}
            </span>
            <div className="symbol-progress">
              {Object.entries(symbolProgress).map(([symbol, progress]) => (
                <span className={`symbol-chip ${progress.status}`} key={symbol}>
                  {symbol}
                  {progress.status === 'running' && ' ⋯'}
                  {progress.status === 'succeeded' &&
                    ` ✓ ${progress.sampleCount?.toLocaleString() ?? ''}`}
                  {progress.status === 'failed' && ` ✗ ${progress.error ?? ''}`}
                </span>
              ))}
            </div>
          </section>
            )}

            <section className="control-section grow">
          <div className="section-title-row">
            <h2>데이터셋</h2>
            <button onClick={refreshDatasets} type="button">
              새로고침
            </button>
          </div>
          {datasetsLoading && datasets.length === 0 ? (
            <p className="hint">데이터셋을 불러오는 중...</p>
          ) : datasets.length === 0 ? (
            <p className="empty">아직 생성된 데이터셋이 없습니다.</p>
          ) : (
            <div className="dataset-table">
              <div className="dataset-row dataset-head">
                <span>이름</span>
                <span>상태</span>
                <span>프리셋</span>
                <span>샘플</span>
                <span>클래스 분포</span>
                <span>생성일</span>
                <span>동작</span>
              </div>
              {datasets.map((dataset) => (
                <div className="dataset-row" key={dataset.id}>
                  <span>
                    <strong>{dataset.name}</strong>
                    <em className="muted-text"> · {dataset.timeframe}</em>
                  </span>
                  <span className={`dataset-status ${dataset.status}`}>
                    {DATASET_STATUS_TEXT[dataset.status] ?? dataset.status}
                    {dataset.failure_message ? (
                      <em title={dataset.failure_message}> ⓘ</em>
                    ) : null}
                  </span>
                  <span>
                    {dataset.preset_snapshot?.preset_name ?? dataset.preset_id}
                    {dataset.preset_snapshot?.preset_version
                      ? ` v${dataset.preset_snapshot.preset_version}`
                      : ''}
                  </span>
                  <span>
                    {dataset.sample_count.toLocaleString()} ({dataset.symbol_count}종목)
                  </span>
                  <span>{classCountsText(dataset.class_counts)}</span>
                  <span>{formatDate(dataset.created_at)}</span>
                  <span className="dataset-actions">
                    {dataset.status === 'ready' && (
                      <button
                        className="ghost"
                        onClick={() => openBrowser(dataset)}
                        type="button"
                      >
                        검수
                      </button>
                    )}
                    <button
                      className="ghost danger"
                      disabled={dataset.status === 'building'}
                      onClick={() => removeDataset(dataset)}
                      title={
                        dataset.status === 'building'
                          ? '생성 중인 데이터셋은 job 취소 후 삭제할 수 있습니다'
                          : 'Storage 객체 삭제 후 메타데이터를 삭제합니다'
                      }
                      type="button"
                    >
                      삭제
                    </button>
                  </span>
                </div>
              ))}
            </div>
          )}
            </section>
          </>
        )}

        {browse && (
          <section className="control-section sample-browser">
            <div className="sample-browser-header">
              <div>
                <span className="sample-browser-eyebrow">데이터셋 검수</span>
                <h2>{browse.name}</h2>
              </div>
              <button className="ghost" onClick={closeBrowser} type="button">
                데이터셋 목록으로
              </button>
            </div>
            <div className="sample-toolbar">
              <label className="inline-field">
                라벨
                <select
                  onChange={(event) => changeLabelFilter(event.target.value)}
                  value={labelFilter === null ? 'all' : String(labelFilter)}
                >
                  <option value="all">전체</option>
                  <option value="0">0 · 저점</option>
                  <option value="1">1 · 고점</option>
                  <option value="2">2 · 무시</option>
                </select>
              </label>
              <div className="sample-navigation" aria-label="샘플 이동">
                <button
                  disabled={position === null || position <= 0 || samplesLoading}
                  onClick={() => position !== null && moveSelection(position - 1)}
                  type="button"
                >
                  ← 이전 샘플
                </button>
                <strong>
                  {position === null ? '-' : (position + 1).toLocaleString()} /{' '}
                  {(page?.total ?? 0).toLocaleString()}
                </strong>
                <button
                  disabled={
                    position === null || !page || position >= page.total - 1 || samplesLoading
                  }
                  onClick={() => position !== null && moveSelection(position + 1)}
                  type="button"
                >
                  다음 샘플 →
                </button>
              </div>
              <button
                className="ghost"
                disabled={!page || page.total === 0}
                onClick={pickRandomSample}
                type="button"
              >
                무작위
              </button>
              <div className="sample-pager">
                <button
                  disabled={pageOffset === 0 || samplesLoading}
                  onClick={() => showPage(Math.max(pageOffset - PAGE_SIZE, 0))}
                  title="이전 페이지"
                  type="button"
                >
                  ←
                </button>
                <span>
                  {currentPage}/{totalPages} 페이지
                </span>
                <button
                  disabled={!page || pageOffset + PAGE_SIZE >= page.total || samplesLoading}
                  onClick={() => showPage(pageOffset + PAGE_SIZE)}
                  title="다음 페이지"
                  type="button"
                >
                  →
                </button>
              </div>
              <span className="sample-shortcut">키보드 ↑↓ 샘플 · ←→ 페이지</span>
            </div>

            {sampleError ? <p className="error">오류: {sampleError}</p> : null}
            {samplesLoading && !page ? <p className="hint">샘플을 불러오는 중...</p> : null}
            {page && page.total === 0 ? (
              <p className="empty">선택한 라벨의 샘플이 없습니다.</p>
            ) : null}

            {page && page.total > 0 && (
              <div className="sample-layout">
                <div className="sample-list" ref={sampleListRef}>
                  {page.items.map((item, order) => {
                    const itemPosition = page.offset + order
                    return (
                      <button
                        className={
                          itemPosition === position ? 'sample-item selected' : 'sample-item'
                        }
                        key={item.index}
                        onClick={() => moveSelection(itemPosition)}
                        type="button"
                      >
                        <span className={`sample-label label-${item.label}`}>
                          {item.label} {LABEL_TEXT[item.label]}
                        </span>
                        <strong>#{item.index}</strong>
                        <span>{item.symbol}</span>
                        <span className="muted-text">
                          {item.length}봉 · {item.split ?? '-'}
                        </span>
                      </button>
                    )
                  })}
                </div>
                <div className="sample-detail">
                  {sampleDetailLoading ? (
                    <p className="empty">샘플 상세를 불러오는 중...</p>
                  ) : selectedSample ? (
                    <>
                      <div className="sample-meta">
                        <div className="sample-meta-primary">
                          <strong>#{selectedSample.index} · {selectedSample.symbol}</strong>
                          <span className={`sample-label label-${selectedSample.label}`}>
                            {selectedSample.label} {LABEL_TEXT[selectedSample.label]}
                          </span>
                        </div>
                        <div className="sample-meta-details">
                          <span>
                            {selectedSample.kind === 'low' ? '프랙탈 저점' : '프랙탈 고점'}
                          </span>
                          <span>{selectedSample.length}봉</span>
                          {sampleRate !== null && (
                            <span
                              className={sampleRate >= 0 ? 'swing-rate up' : 'swing-rate down'}
                              title="스윙 시작(직전 반대 프랙탈)~끝(현재 프랙탈) 가격 변화율"
                            >
                              {sampleRate >= 0 ? '+' : ''}
                              {sampleRate.toFixed(2)}%
                            </span>
                          )}
                          <span>split {selectedSample.split ?? '-'}</span>
                          <span className="muted-text">
                            {formatDate(selectedSample.start_time)} ~{' '}
                            {formatDate(selectedSample.end_time)}
                          </span>
                        </div>
                      </div>
                      <MiniSampleChart
                        columns={selectedSample.feature_columns}
                        features={selectedSample.features}
                      />
                      <div className="sample-features">
                        <table>
                          <thead>
                            <tr>
                              <th>#</th>
                              {selectedSample.feature_columns.map((column) => (
                                <th key={column}>{column}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {selectedSample.features.map((row, barIndex) => (
                              <tr key={barIndex}>
                                <td>{barIndex}</td>
                                {row.map((value, columnIndex) => (
                                  <td key={columnIndex}>{formatFeatureValue(value)}</td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  ) : (
                    <p className="empty">왼쪽 목록에서 샘플을 선택하세요.</p>
                  )}
                </div>
              </div>
            )}
          </section>
        )}
      </section>
    </>
  )
}
