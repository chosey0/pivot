import { useEffect, useMemo, useState } from 'react'
import {
  liveApi,
  type LiveDeployment,
  type LiveStateResponse,
} from '../../api/live'
import {
  trainingApi,
  type ArtifactSummary,
  type RunSummary,
} from '../../api/training'

const DEPLOYMENT_STATUS_TEXT: Record<LiveDeployment['status'], string> = {
  activating: '활성화 중',
  active: '활성',
  failed: '실패',
}

function formatDate(value: string | null) {
  return value ? value.slice(0, 19).replace('T', ' ') : '-'
}

interface Props {
  deployment: LiveDeployment | null
  onActivated: (state: LiveStateResponse) => void
}

/**
 * 활성 모델 지정 — succeeded run의 검증된 best_checkpoint만 후보로 노출한다.
 * PUT /api/live/model 실패 시 기존 deployment를 그대로 유지한다 (docs/08 §6.1).
 */
export function ModelPanel({ deployment, onActivated }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const [bestArtifact, setBestArtifact] = useState<ArtifactSummary | null>(null)
  const [artifactLoading, setArtifactLoading] = useState(false)
  const [artifactError, setArtifactError] = useState<string | null>(null)
  const [activating, setActivating] = useState(false)
  const [activateError, setActivateError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const succeededRuns = useMemo(
    () => runs.filter((run) => run.status === 'succeeded'),
    [runs],
  )

  useEffect(() => {
    let stale = false
    trainingApi
      .runs()
      .then((rows) => {
        if (stale) return
        setRuns(rows)
        setRunsError(null)
      })
      .catch((e: Error) => {
        if (!stale) setRunsError(e.message)
      })
      .finally(() => {
        if (!stale) setRunsLoading(false)
      })
    return () => {
      stale = true
    }
  }, [])

  // run 선택 시 검증된 best_checkpoint 존재 여부를 확인한다
  useEffect(() => {
    if (selectedRunId === null) {
      setBestArtifact(null)
      setArtifactError(null)
      return
    }
    let stale = false
    setArtifactLoading(true)
    setBestArtifact(null)
    setArtifactError(null)
    trainingApi
      .runDetail(selectedRunId)
      .then((detail) => {
        if (stale) return
        const best = detail.artifacts.find((row) => row.kind === 'best_checkpoint') ?? null
        setBestArtifact(best)
        if (!best) setArtifactError('이 run에는 검증된 best checkpoint가 없습니다.')
      })
      .catch((e: Error) => {
        if (!stale) setArtifactError(e.message)
      })
      .finally(() => {
        if (!stale) setArtifactLoading(false)
      })
    return () => {
      stale = true
    }
  }, [selectedRunId])

  async function activate() {
    if (selectedRunId === null || !bestArtifact) return
    setActivating(true)
    setActivateError(null)
    setMessage(null)
    try {
      const next = await liveApi.activateModel(selectedRunId, bestArtifact.id)
      onActivated(next)
      setMessage(`run #${selectedRunId} 모델을 활성화했습니다.`)
      setSelectedRunId(null)
    } catch (e) {
      // 활성화 실패 — 서버가 기존 모델을 유지하므로 화면의 deployment도 그대로 둔다
      setActivateError(e instanceof Error ? e.message : String(e))
    } finally {
      setActivating(false)
    }
  }

  const selectedRun = succeededRuns.find((run) => run.id === selectedRunId) ?? null

  return (
    <section className="control-section">
      <h2>활성 모델</h2>
      {deployment ? (
        <dl className="live-model-meta">
          <div>
            <dt>run</dt>
            <dd>
              #{deployment.run_id} {deployment.run_name}
              <span className={`live-deploy-status ${deployment.status}`}>
                {DEPLOYMENT_STATUS_TEXT[deployment.status]}
              </span>
            </dd>
          </div>
          <div>
            <dt>데이터셋</dt>
            <dd>{deployment.dataset_name}</dd>
          </div>
          <div>
            <dt>모델 · timeframe</dt>
            <dd>
              {deployment.model} · {deployment.timeframe}
            </dd>
          </div>
          <div>
            <dt>features</dt>
            <dd>{deployment.feature_columns.join(', ')}</dd>
          </div>
          <div>
            <dt>pairing</dt>
            <dd>{deployment.pairing_rule}</dd>
          </div>
          <div>
            <dt>활성화 시각</dt>
            <dd>{formatDate(deployment.activated_at)}</dd>
          </div>
        </dl>
      ) : (
        <p className="empty">활성 모델이 없습니다. succeeded run에서 모델을 지정하세요.</p>
      )}

      {runsError ? <p className="error">run 목록 오류: {runsError}</p> : null}
      {!runsLoading && !runsError && succeededRuns.length === 0 ? (
        <p className="hint">succeeded 상태의 run이 없어 활성화할 모델이 없습니다.</p>
      ) : null}

      {succeededRuns.length > 0 && (
        <>
          <label className="field">
            모델 교체 (succeeded run)
            <select
              onChange={(event) =>
                setSelectedRunId(event.target.value === '' ? null : Number(event.target.value))
              }
              value={selectedRunId ?? ''}
            >
              <option value="">run 선택...</option>
              {succeededRuns.map((run) => (
                <option key={run.id} value={run.id}>
                  #{run.id} {run.name} · {run.dataset_name}
                </option>
              ))}
            </select>
          </label>
          {selectedRun && (
            <p className="hint">
              {selectedRun.config.model} · best {selectedRun.best_metric_name ?? 'val_macro_f1'}{' '}
              {selectedRun.best_metric_value?.toFixed(4) ?? '-'}
              {selectedRun.best_epoch !== null ? ` @ep${selectedRun.best_epoch}` : ''}
            </p>
          )}
          {artifactLoading ? <p className="hint">best checkpoint 확인 중...</p> : null}
          {artifactError ? <p className="error">{artifactError}</p> : null}
          {bestArtifact && (
            <p className="hint">
              best_checkpoint #{bestArtifact.id}
              {bestArtifact.epoch !== null ? ` · ep${bestArtifact.epoch}` : ''} · sha256{' '}
              {bestArtifact.sha256.slice(0, 12)}…
            </p>
          )}
          {activateError ? (
            <p className="error">활성화 실패 (기존 모델 유지): {activateError}</p>
          ) : null}
          {message && !activateError ? <p className="message">{message}</p> : null}
          <button
            className="primary"
            disabled={selectedRunId === null || !bestArtifact || activating}
            onClick={activate}
            type="button"
          >
            {activating ? '활성화 중...' : '이 모델 활성화'}
          </button>
        </>
      )}
    </section>
  )
}
