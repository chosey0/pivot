import { useEffect, useState } from 'react'
import {
  api,
  type DatasetRow,
  type DiagnosticReportDetail,
  type DiagnosticReportRow,
  type DiagnosticTarget,
  type PresetRow,
  type WatchItem,
} from '../api/client'
import { MINUTE_UNITS, TICK_UNITS, toTimeframeCode, type TimeframeKind } from '../lib/timeframe'

const TARGET_TEXT: Record<DiagnosticTarget, string> = {
  raw_cache: '원천 캐시',
  preset: '프리셋 미리보기',
  dataset: '데이터셋',
}

const STATUS_TEXT: Record<string, string> = {
  passed: '통과',
  warning: '경고',
  failed: '실패',
}

function formatDate(value: string) {
  return value.slice(0, 19).replace('T', ' ')
}

export function Diagnostics() {
  const [target, setTarget] = useState<DiagnosticTarget>('raw_cache')
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [presets, setPresets] = useState<PresetRow[]>([])
  const [datasets, setDatasets] = useState<DatasetRow[]>([])

  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([])
  const [timeframeKind, setTimeframeKind] = useState<TimeframeKind>('day')
  const [timeframeUnit, setTimeframeUnit] = useState(1)
  const [presetId, setPresetId] = useState<number | null>(null)
  const [datasetId, setDatasetId] = useState<number | null>(null)

  const [reports, setReports] = useState<DiagnosticReportRow[]>([])
  const [current, setCurrent] = useState<DiagnosticReportDetail | null>(null)
  const [historyOpen, setHistoryOpen] = useState(true)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function refreshReports() {
    api
      .diagnosticReports()
      .then(setReports)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    refreshReports()
    api
      .watchlist()
      .then((items) => {
        setWatchlist(items)
        setSelectedSymbols(items.map((item) => item.symbol))
      })
      .catch((e: Error) => setError(e.message))
    api
      .presets()
      .then((rows) => {
        setPresets(rows)
        setPresetId(rows[0]?.id ?? null)
      })
      .catch((e: Error) => setError(e.message))
    api
      .datasets()
      .then((rows) => {
        setDatasets(rows)
        setDatasetId(rows[0]?.id ?? null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  function toggleSymbol(symbol: string) {
    setSelectedSymbols((current) =>
      current.includes(symbol)
        ? current.filter((item) => item !== symbol)
        : [...current, symbol],
    )
  }

  const canRun =
    !running &&
    (target === 'raw_cache'
      ? selectedSymbols.length > 0
      : target === 'preset'
        ? presetId !== null && selectedSymbols.length > 0
        : datasetId !== null)

  async function runDiagnostics() {
    if (!canRun) return
    setError(null)
    setRunning(true)
    try {
      let report: DiagnosticReportDetail
      if (target === 'raw_cache') {
        report = await api.diagnoseCache(
          selectedSymbols,
          toTimeframeCode(timeframeKind, timeframeUnit),
        )
      } else if (target === 'preset') {
        report = await api.diagnosePreview(presetId!, selectedSymbols)
      } else {
        report = await api.diagnoseDataset(datasetId!)
      }
      setCurrent(report)
      refreshReports()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  async function openReport(reportId: number) {
    setError(null)
    try {
      setCurrent(await api.diagnosticReport(reportId))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <aside className="side-panel lab-symbols">
        <section className="control-section">
          <h2>진단 대상</h2>
          <div className="field">
            {(Object.keys(TARGET_TEXT) as DiagnosticTarget[]).map((value) => (
              <label className="inline-check" key={value}>
                <input
                  checked={target === value}
                  name="diag-target"
                  onChange={() => setTarget(value)}
                  type="radio"
                />
                {TARGET_TEXT[value]}
              </label>
            ))}
          </div>

          {target !== 'dataset' && (
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
                    </label>
                  ))
                )}
              </div>
            </div>
          )}

          {target === 'raw_cache' && (
            <label className="field">
              타임프레임
              <span className="inline-selects">
                <select
                  onChange={(event) => {
                    const kind = event.target.value as TimeframeKind
                    setTimeframeKind(kind)
                    setTimeframeUnit(1)
                  }}
                  value={timeframeKind}
                >
                  <option value="day">일봉</option>
                  <option value="minute">분봉</option>
                  <option value="tick">틱봉</option>
                </select>
                {timeframeKind !== 'day' && (
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
                )}
              </span>
            </label>
          )}

          {target === 'preset' && (
            <label className="field">
              프리셋
              <select
                onChange={(event) => setPresetId(Number(event.target.value))}
                value={presetId ?? ''}
              >
                {presets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.name} v{preset.version}
                  </option>
                ))}
              </select>
            </label>
          )}

          {target === 'dataset' && (
            <label className="field">
              데이터셋
              <select
                onChange={(event) => setDatasetId(Number(event.target.value))}
                value={datasetId ?? ''}
              >
                {datasets.map((dataset) => (
                  <option key={dataset.id} value={dataset.id}>
                    {dataset.name} ({dataset.status})
                  </option>
                ))}
              </select>
            </label>
          )}

          <button className="primary" disabled={!canRun} onClick={runDiagnostics} type="button">
            {running ? '진단 중...' : '진단 실행'}
          </button>
          <p className="hint">
            진단은 읽기 전용입니다. 리포트는 입력 스냅샷과 함께 Supabase에 저장됩니다.
          </p>
          <p className="hint">
            원천·프리셋 진단은{' '}
            <a href="https://arxiv.org/abs/2508.02739" rel="noreferrer" target="_blank">
              Kronos Appendix B
            </a>
            를 국내 봉 데이터에 맞게 적용한 품질 경계를 포함합니다.
          </p>
        </section>

      </aside>

      <section className="datasets-main">
        {error ? <p className="error">오류: {error}</p> : null}

        {!current ? (
          <section className="placeholder">
            <h2>데이터 진단</h2>
            <p>
              왼쪽에서 진단 대상을 선택해 실행하거나, 오른쪽 리포트 이력에서 저장된 리포트를
              열어보세요.
            </p>
          </section>
        ) : (
          <section className="control-section grow">
            <div className="section-title-row">
              <h2>
                리포트 #{current.id} — {TARGET_TEXT[current.target_type]}{' '}
                <em className={`diag-status ${current.status}`}>
                  {STATUS_TEXT[current.status]}
                </em>
              </h2>
              <span className="hint">{formatDate(current.created_at)}</span>
            </div>
            <p className="hint">
              검사 {current.summary.checks}건 — 통과 {current.summary.passed} · 경고{' '}
              {current.summary.warning} · 실패 {current.summary.failed}
            </p>
            <div className="diag-table">
              <div className="diag-row diag-head">
                <span>종목</span>
                <span>검사</span>
                <span>상태</span>
                <span>메시지</span>
              </div>
              {current.report.checks.map((item, order) => (
                <div className="diag-row" key={order}>
                  <span>{item.symbol ?? '-'}</span>
                  <span className="muted-text">{item.id}</span>
                  <span className={`diag-status ${item.status}`}>
                    {STATUS_TEXT[item.status]}
                  </span>
                  <span>{item.message}</span>
                </div>
              ))}
            </div>
          </section>
        )}
      </section>

      <aside className={historyOpen ? 'diagnostics-history' : 'diagnostics-history collapsed'}>
        <div className="section-title-row">
          {historyOpen ? <h2>리포트 이력</h2> : null}
          <div className="row-actions">
            {historyOpen ? (
              <button onClick={refreshReports} type="button">
                새로고침
              </button>
            ) : null}
            <button
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen((open) => !open)}
              title={historyOpen ? '리포트 이력 접기' : '리포트 이력 열기'}
              type="button"
            >
              {historyOpen ? '접기' : '이력 열기'}
            </button>
          </div>
        </div>
        {historyOpen &&
          (reports.length === 0 ? (
            <p className="empty">저장된 진단 리포트가 없습니다.</p>
          ) : (
            <div className="watch-table">
              {reports.map((report) => (
                <div
                  className={report.id === current?.id ? 'watch-row selected' : 'watch-row'}
                  key={report.id}
                >
                  <button
                    className="watch-main"
                    onClick={() => openReport(report.id)}
                    type="button"
                  >
                    <strong>
                      #{report.id} {TARGET_TEXT[report.target_type]}{' '}
                      <em className={`diag-status ${report.status}`}>
                        {STATUS_TEXT[report.status]}
                      </em>
                    </strong>
                    <span>
                      통과 {report.summary.passed} · 경고 {report.summary.warning} · 실패{' '}
                      {report.summary.failed}
                      {' · '}
                      {formatDate(report.created_at)}
                    </span>
                  </button>
                </div>
              ))}
            </div>
          ))}
      </aside>
    </>
  )
}
