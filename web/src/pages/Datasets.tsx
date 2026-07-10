import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type DatasetRow,
  type JobRow,
  type PresetRow,
  type WatchItem,
} from '../api/client'

const DATASET_STATUS_TEXT: Record<string, string> = {
  building: '생성 중',
  ready: '완료',
  failed: '실패',
}

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

export function Datasets() {
  const [presets, setPresets] = useState<PresetRow[]>([])
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [datasets, setDatasets] = useState<DatasetRow[]>([])
  const [selectedPresetId, setSelectedPresetId] = useState<number | null>(null)
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([])
  const [datasetName, setDatasetName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [job, setJob] = useState<JobRow | null>(null)
  const [symbolProgress, setSymbolProgress] = useState<Record<string, SymbolProgress>>({})
  const eventSourceRef = useRef<EventSource | null>(null)

  const refreshPresets = useCallback(() => {
    api
      .presets()
      .then((rows) => {
        setPresets(rows)
        setSelectedPresetId((current) =>
          current && rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null,
        )
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const refreshDatasets = useCallback(() => {
    api
      .datasets()
      .then(setDatasets)
      .catch((e: Error) => setError(e.message))
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

  const jobRunning = job !== null && (job.status === 'queued' || job.status === 'running')
  const selectedPreset = presets.find((preset) => preset.id === selectedPresetId) ?? null

  return (
    <>
      <aside className="side-panel lab-symbols">
        <section className="control-section grow">
          <div className="section-title-row">
            <h2>프리셋</h2>
            <button onClick={refreshPresets} type="button">
              새로고침
            </button>
          </div>
          {presets.length === 0 ? (
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
                  <button
                    className="ghost"
                    onClick={() => archivePreset(preset)}
                    title="보관 (batch 대상에서 제외)"
                    type="button"
                  >
                    보관
                  </button>
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

      <section className="datasets-main">
        {error ? <p className="error">오류: {error}</p> : null}
        {message && !error ? <p className="message">{message}</p> : null}

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
            <h2>
              진행 상황 — job #{job.id} ({job.status})
            </h2>
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
          {datasets.length === 0 ? (
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
                </div>
              ))}
            </div>
          )}
        </section>
      </section>
    </>
  )
}
