import { useCallback, useEffect, useRef, useState } from 'react'
import {
  trainingApi,
  type ArtifactSummary,
  type EpochRow,
  type EvaluationResult,
  type RunDetail,
  type RunStatus,
  type RunSummary,
} from '../../api/training'

export type SseConnection = 'idle' | 'live' | 'reconnecting' | 'closed'

export function isTerminal(status: RunStatus): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled'
}

function upsertEpoch(rows: EpochRow[], next: EpochRow): EpochRow[] {
  const merged = rows.filter((row) => row.epoch !== next.epoch)
  merged.push(next)
  return merged.sort((a, b) => a.epoch - b.epoch)
}

function upsertById<T extends { id: number }>(rows: T[], next: T): T[] {
  const merged = rows.filter((row) => row.id !== next.id)
  merged.push(next)
  return merged.sort((a, b) => a.id - b.id)
}

/**
 * run 상세 + SSE 구독. Supabase durable 상태가 원본이고 SSE는 전달 계층이므로
 * (docs/07 §5.4) 연결 단절·종료 이벤트 시점에는 GET /api/runs/{id}로 재조회한다.
 * epoch/evaluation/artifact 이벤트는 key 기준 upsert라 재전송돼도 중복되지 않는다.
 */
export function useRunDetail(runId: number | null) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [connection, setConnection] = useState<SseConnection>('idle')
  const [nonce, setNonce] = useState(0)
  const sourceRef = useRef<EventSource | null>(null)

  /** 상세 재조회 + SSE 재구독까지 처음부터 다시 수행한다. */
  const reload = useCallback(() => setNonce((current) => current + 1), [])

  const refetch = useCallback(() => {
    if (runId === null) return Promise.resolve(null)
    return trainingApi
      .runDetail(runId)
      .then((next) => {
        setDetail(next)
        setError(null)
        return next
      })
      .catch((e: Error) => {
        setError(e.message)
        return null
      })
  }, [runId])

  useEffect(() => {
    if (runId === null) {
      setDetail(null)
      setError(null)
      setConnection('idle')
      return
    }
    let stale = false
    let source: EventSource | null = null
    let retryTimer: number | undefined
    setDetail(null)
    setError(null)
    setLoading(true)
    setConnection('idle')

    const closeSource = () => {
      source?.close()
      source = null
      sourceRef.current = null
      if (!stale) setConnection('closed')
    }

    const refetchDetail = (onDone?: (next: RunDetail) => void) => {
      trainingApi
        .runDetail(runId)
        .then((next) => {
          if (stale) return
          setDetail(next)
          onDone?.(next)
        })
        .catch(() => undefined) // 일시 오류는 다음 이벤트/재연결에서 복구된다
    }

    const subscribe = () => {
      source = new EventSource(trainingApi.eventsUrl(runId))
      sourceRef.current = source
      source.onopen = () => {
        if (!stale) setConnection('live')
      }
      source.addEventListener('run', (event) => {
        if (stale) return
        const run = JSON.parse((event as MessageEvent).data) as RunSummary
        setDetail((current) => (current ? { ...current, run } : current))
      })
      source.addEventListener('epoch', (event) => {
        if (stale) return
        const row = JSON.parse((event as MessageEvent).data) as EpochRow
        setDetail((current) =>
          current ? { ...current, epochs: upsertEpoch(current.epochs, row) } : current,
        )
      })
      source.addEventListener('evaluation', (event) => {
        if (stale) return
        const row = JSON.parse((event as MessageEvent).data) as EvaluationResult
        setDetail((current) =>
          current
            ? { ...current, evaluations: upsertById(current.evaluations, row) }
            : current,
        )
      })
      source.addEventListener('artifact', (event) => {
        if (stale) return
        const row = JSON.parse((event as MessageEvent).data) as ArtifactSummary
        setDetail((current) =>
          current ? { ...current, artifacts: upsertById(current.artifacts, row) } : current,
        )
      })
      const finish = () => {
        if (stale) return
        refetchDetail(() => closeSource())
      }
      source.addEventListener('run_succeeded', finish)
      source.addEventListener('run_failed', finish)
      source.addEventListener('run_cancelled', finish)
      source.onerror = () => {
        if (stale) return
        // EventSource 기본 재연결에 맡기되(Last-Event-ID로 durable 이벤트는 건너뜀),
        // 단절 동안의 상태 변화는 상세 재조회로 복구한다.
        setConnection('reconnecting')
        // HTTP 오류 응답 등으로 브라우저가 재연결을 포기하면(CLOSED) 직접 다시 구독한다
        const fatal = source?.readyState === EventSource.CLOSED
        refetchDetail((next) => {
          if (isTerminal(next.run.status)) {
            closeSource()
            return
          }
          if (fatal) {
            window.clearTimeout(retryTimer)
            retryTimer = window.setTimeout(() => {
              if (stale) return
              source?.close()
              subscribe()
            }, 2000)
          }
        })
      }
    }

    trainingApi
      .runDetail(runId)
      .then((next) => {
        if (stale) return
        setDetail(next)
        if (isTerminal(next.run.status)) setConnection('closed')
        else subscribe()
      })
      .catch((e: Error) => {
        if (!stale) setError(e.message)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })

    return () => {
      stale = true
      window.clearTimeout(retryTimer)
      source?.close()
      sourceRef.current = null
    }
  }, [runId, nonce])

  return { detail, loading, error, connection, refetch, reload }
}
