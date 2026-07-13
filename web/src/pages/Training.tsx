import { useCallback, useEffect, useState } from 'react'
import { api, type DatasetRow } from '../api/client'
import { trainingApi, type RunStatus, type RunSummary } from '../api/training'
import { EvaluationReport } from '../components/training/EvaluationReport'
import { MetricCurves } from '../components/training/MetricCurves'
import { NewRunForm } from '../features/training/NewRunForm'
import { PredictionPanel } from '../features/training/PredictionPanel'
import { isTerminal, useRunDetail } from '../features/training/useRunDetail'
import './Training.css'

const STATUS_TEXT: Record<RunStatus, string> = {
  queued: '대기',
  running: '학습 중',
  succeeded: '완료',
  failed: '실패',
  cancelled: '취소',
}

function formatDate(value: string | null) {
  return value ? value.slice(0, 19).replace('T', ' ') : '-'
}

function formatMetricValue(value: number | null) {
  return value === null ? '-' : value.toLocaleString('ko-KR', { maximumFractionDigits: 4 })
}

function formatBytes(size: number) {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(Math.round(size / 1024), 1)} KB`
}

function modelShort(model: string) {
  return model.replace('cnn1d_', '').replace('_v1', '')
}

export function Training() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [datasets, setDatasets] = useState<DatasetRow[]>([])
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [deletingRunId, setDeletingRunId] = useState<number | null>(null)

  const { detail, loading: detailLoading, error: detailError, connection, refetch, reload } =
    useRunDetail(selectedRunId)

  const refreshRuns = useCallback(() => {
    setRunsLoading(true)
    trainingApi
      .runs()
      .then((rows) => {
        setRuns(rows)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setRunsLoading(false))
  }, [])

  useEffect(() => {
    refreshRuns()
    api
      .datasets()
      .then(setDatasets)
      .catch((e: Error) => setError(e.message))
  }, [refreshRuns])

  // SSE로 갱신된 run 스냅샷을 목록 행에도 반영해 목록/상세가 어긋나지 않게 한다
  useEffect(() => {
    const run = detail?.run
    if (!run) return
    setRuns((current) => current.map((row) => (row.id === run.id ? run : row)))
  }, [detail?.run])

  function handleCreated(runId: number) {
    setShowForm(false)
    setMessage(`run #${runId} 학습을 시작했습니다.`)
    refreshRuns()
    setSelectedRunId(runId)
  }

  async function stopRun(run: RunSummary) {
    if (!window.confirm(`run '${run.name}' 학습을 중단할까요?`)) return
    setStopping(true)
    setError(null)
    try {
      await trainingApi.stopRun(run.id)
      setMessage(`run #${run.id} 중단을 요청했습니다.`)
      await refetch()
      refreshRuns()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStopping(false)
    }
  }

  async function removeRun(run: RunSummary) {
    if (
      !window.confirm(
        `run '${run.name}'의 체크포인트와 학습 이력을 모두 삭제할까요? 이 작업은 되돌릴 수 없습니다.`,
      )
    )
      return
    setDeletingRunId(run.id)
    setError(null)
    try {
      await trainingApi.deleteRun(run.id)
      if (selectedRunId === run.id) setSelectedRunId(null)
      setMessage(`run #${run.id}을 삭제했습니다.`)
      refreshRuns()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      refreshRuns()
    } finally {
      setDeletingRunId(null)
    }
  }

  const run = detail?.run ?? null
  const runActive = run !== null && !isTerminal(run.status)
  const readyDatasets = datasets.filter((row) => row.status === 'ready')
  const hasBestCheckpoint =
    detail?.artifacts.some((artifact) => artifact.kind === 'best_checkpoint') ?? false

  return (
    <>
      <aside className="side-panel">
        <section className="control-section grow">
          <div className="section-title-row">
            <h2>학습 run</h2>
            <div className="run-list-actions">
              <button onClick={() => setShowForm(true)} type="button">
                새 run
              </button>
              <button onClick={refreshRuns} type="button">
                새로고침
              </button>
            </div>
          </div>
          {runsLoading && runs.length === 0 ? (
            <p className="hint">run 목록을 불러오는 중...</p>
          ) : runs.length === 0 ? (
            <p className="empty">아직 학습 run이 없습니다. 새 run으로 시작하세요.</p>
          ) : (
            <div className="run-list">
              {runs.map((row) => (
                <div
                  className={row.id === selectedRunId ? 'run-item selected' : 'run-item'}
                  key={row.id}
                >
                  <button
                    className="run-item-main"
                    onClick={() => {
                      if (row.id === selectedRunId) reload()
                      else setSelectedRunId(row.id)
                      setShowForm(false)
                    }}
                    type="button"
                  >
                    <span className="run-item-title-row">
                      <strong className="run-item-title">{row.name}</strong>
                      <span className={`run-status ${row.status}`}>
                        {STATUS_TEXT[row.status]}
                      </span>
                    </span>
                    <span className="run-item-meta">
                      {row.dataset_name} · {modelShort(row.config.model)} ·{' '}
                      {row.device ?? '디바이스 미정'}
                    </span>
                    <span className="run-item-meta">
                      best F1 {formatMetricValue(row.best_metric_value)}
                      {row.best_epoch !== null ? ` @ep${row.best_epoch}` : ''} ·{' '}
                      {formatDate(row.created_at)}
                    </span>
                    {row.error ? (
                      <span className="run-item-error" title={row.error}>
                        ✗ {row.error}
                      </span>
                    ) : null}
                  </button>
                  <div className="run-item-actions">
                    <button
                      className="run-item-delete danger"
                      disabled={!isTerminal(row.status) || deletingRunId === row.id}
                      onClick={() => removeRun(row)}
                      title={
                        isTerminal(row.status)
                          ? '체크포인트와 학습 이력을 삭제합니다'
                          : '진행 중인 run은 중단 후 삭제할 수 있습니다'
                      }
                      type="button"
                    >
                      {deletingRunId === row.id ? '삭제 중' : '삭제'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </aside>

      <section className="training-main">
        {error ? <p className="error">오류: {error}</p> : null}
        {message && !error ? <p className="message">{message}</p> : null}

        {showForm && (
          <NewRunForm
            datasets={readyDatasets}
            onClose={() => setShowForm(false)}
            onCreated={handleCreated}
          />
        )}

        {!showForm && selectedRunId === null && (
          <section className="control-section">
            <p className="empty">
              왼쪽 목록에서 run을 선택하거나 새 run으로 학습을 시작하세요.
            </p>
          </section>
        )}

        {!showForm && selectedRunId !== null && (
          <>
            {detailError ? (
              <p className="error">
                run 상세 오류: {detailError}{' '}
                <button className="ghost" onClick={reload} type="button">
                  다시 시도
                </button>
              </p>
            ) : null}
            {detailLoading && !detail ? <p className="hint">run 상세를 불러오는 중...</p> : null}

            {run && detail && (
              <>
                <section className="control-section">
                  <div className="section-title-row">
                    <h2>
                      run #{run.id} · {run.name}
                      <span className={`run-status ${run.status}`}>
                        {STATUS_TEXT[run.status]}
                      </span>
                      {runActive && connection === 'live' && (
                        <span className="sse-badge live">실시간</span>
                      )}
                      {runActive && connection === 'reconnecting' && (
                        <span className="sse-badge reconnecting">재연결 중</span>
                      )}
                    </h2>
                    <div className="run-list-actions">
                      {runActive && (
                        <button
                          className="ghost danger"
                          disabled={stopping}
                          onClick={() => stopRun(run)}
                          type="button"
                        >
                          {stopping ? '중단 요청 중...' : '학습 중단'}
                        </button>
                      )}
                      <button className="ghost" onClick={reload} type="button">
                        새로고침
                      </button>
                    </div>
                  </div>
                  {run.error ? <p className="error">실패 원인: {run.error}</p> : null}
                  <dl className="run-meta">
                    <div>
                      <dt>데이터셋</dt>
                      <dd>{run.dataset_name}</dd>
                    </div>
                    <div>
                      <dt>모델</dt>
                      <dd>{run.config.model}</dd>
                    </div>
                    <div>
                      <dt>디바이스</dt>
                      <dd>{run.device ?? '-'}</dd>
                    </div>
                    <div>
                      <dt>설정</dt>
                      <dd>
                        ep {run.config.epochs} · batch {run.config.batch_size} · lr{' '}
                        {run.config.learning_rate} · sampler {run.config.sampler} · seed{' '}
                        {run.config.seed}
                      </dd>
                    </div>
                    <div>
                      <dt>고정 계약</dt>
                      <dd>
                        {run.config.scaling} · {run.config.padding} · {run.config.best_metric}
                      </dd>
                    </div>
                    <div>
                      <dt>best</dt>
                      <dd>
                        {run.best_metric_value !== null
                          ? `${run.best_metric_name ?? 'val_macro_f1'} ${formatMetricValue(run.best_metric_value)} @ epoch ${run.best_epoch}`
                          : '-'}
                      </dd>
                    </div>
                    <div>
                      <dt>생성 / 시작 / 종료</dt>
                      <dd>
                        {formatDate(run.created_at)} / {formatDate(run.started_at)} /{' '}
                        {formatDate(run.completed_at)}
                      </dd>
                    </div>
                  </dl>
                </section>

                <section className="control-section">
                  <h2>학습 곡선</h2>
                  {run.status === 'queued' && detail.epochs.length === 0 ? (
                    <p className="hint">학습 시작을 기다리는 중...</p>
                  ) : (
                    <MetricCurves epochs={detail.epochs} />
                  )}
                </section>

                <section className="control-section">
                  <h2>평가</h2>
                  {detail.evaluations.length === 0 ? (
                    <p className="empty">
                      {runActive
                        ? '학습이 끝나면 validation/test 평가가 기록됩니다.'
                        : '기록된 평가가 없습니다.'}
                    </p>
                  ) : (
                    detail.evaluations.map((evaluation) => (
                      <div className="eval-block" key={evaluation.id}>
                        <h3>
                          {evaluation.split} · {formatDate(evaluation.created_at)}
                        </h3>
                        <EvaluationReport evaluation={evaluation} />
                      </div>
                    ))
                  )}
                </section>

                <section className="control-section">
                  <h2>체크포인트 · artifact</h2>
                  {detail.artifacts.length === 0 ? (
                    <p className="empty">기록된 artifact가 없습니다.</p>
                  ) : (
                    <table className="artifact-table">
                      <thead>
                        <tr>
                          <th>종류</th>
                          <th>epoch</th>
                          <th>크기</th>
                          <th>sha256</th>
                          <th>생성일</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.artifacts.map((artifact) => (
                          <tr
                            className={artifact.kind === 'best_checkpoint' ? 'artifact-best' : undefined}
                            key={artifact.id}
                          >
                            <td>
                              {artifact.kind}
                              {artifact.kind === 'best_checkpoint' ? (
                                <span className="artifact-best-badge">best</span>
                              ) : null}
                            </td>
                            <td>{artifact.epoch ?? '-'}</td>
                            <td>{formatBytes(artifact.size_bytes)}</td>
                            <td className="artifact-sha" title={artifact.sha256}>
                              {artifact.sha256.slice(0, 12)}…
                            </td>
                            <td>{formatDate(artifact.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </section>

                <section className="control-section">
                  <h2>예측 검수</h2>
                  {run.status === 'succeeded' || hasBestCheckpoint ? (
                    <PredictionPanel run={run} />
                  ) : (
                    <p className="empty">
                      {runActive
                        ? '학습이 완료되면 예측 검수를 사용할 수 있습니다.'
                        : 'best checkpoint가 없어 예측 검수를 사용할 수 없습니다.'}
                    </p>
                  )}
                </section>
              </>
            )}
          </>
        )}
      </section>
    </>
  )
}
