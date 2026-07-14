import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  type Candle,
  type DatasetSymbolRow,
  type TimeframeCode,
} from '../../api/client'
import {
  trainingApi,
  type EvaluationSplit,
  type PredictionEvaluation,
  type PredictionPoint,
  type RunSummary,
} from '../../api/training'
import { PredictionChart } from '../../components/training/PredictionChart'
import { datasetSourceKey, timeframeLabel } from '../../lib/watchlist'

const LABEL_TEXT: Record<number, string> = { 0: '저점', 1: '고점', 2: '무시' }
const MAX_PREDICTION_CHART_BARS = 20_000

interface DatasetTarget {
  symbol: string
  timeframe: TimeframeCode
  region: 'domestic' | 'overseas'
  exchange: string
  start: string | null
  end: string | null
}

function formatTime(value: string | number) {
  if (typeof value === 'number') {
    const date = new Date(value * 1000)
    const pad = (part: number) => String(part).padStart(2, '0')
    return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  }
  return String(value)
}

function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

function chartBefore(points: PredictionPoint[], timeframe: string) {
  const latest = points.reduce<string | number | null>((current, point) => {
    if (current === null) return point.time
    return point.time > current ? point.time : current
  }, null)
  if (latest === null) return undefined
  if (timeframe === 'day') {
    const date = new Date(`${latest}T00:00:00Z`)
    date.setUTCDate(date.getUTCDate() + 1)
    return date.toISOString().slice(0, 10)
  }
  return Number(latest) + 1
}

/** 예측 검수 — 종목/split을 골라 실제 프랙탈 라벨 대비 모델 예측을 차트로 확인한다. */
export function PredictionPanel({ run }: { run: RunSummary }) {
  const [symbols, setSymbols] = useState<DatasetSymbolRow[]>([])
  const [sources, setSources] = useState<
    Record<string, { region: 'domestic' | 'overseas'; exchange: string }>
  >({})
  const [targets, setTargets] = useState<DatasetTarget[]>([])
  const [symbolsError, setSymbolsError] = useState<string | null>(null)
  const [split, setSplit] = useState<EvaluationSplit>('validation')
  const [symbol, setSymbol] = useState<string | null>(null)
  const [selectedTargetKey, setSelectedTargetKey] = useState('')
  const [evaluation, setEvaluation] = useState<PredictionEvaluation | null>(null)
  const [candles, setCandles] = useState<Candle[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [misclassifiedOnly, setMisclassifiedOnly] = useState(false)
  const [selected, setSelected] = useState<PredictionPoint | null>(null)

  useEffect(() => {
    let stale = false
    api
      .dataset(run.dataset_id)
      .then((detail) => {
        if (stale) return
        setSymbols(detail.symbols)
        setSources(detail.preset_snapshot.sources ?? {})
        setTargets(detail.preset_snapshot.targets ?? [])
        setSymbolsError(null)
      })
      .catch((e: Error) => {
        if (!stale) setSymbolsError(e.message)
      })
    return () => {
      stale = true
    }
  }, [run.dataset_id])

  const splitSymbols = useMemo(
    () => symbols.filter((row) => row.split === null || row.split === split),
    [symbols, split],
  )

  useEffect(() => {
    setSymbol((current) =>
      current && splitSymbols.some((row) => row.symbol === current)
        ? current
        : splitSymbols[0]?.symbol ?? null,
    )
  }, [splitSymbols])

  const symbolTargets = useMemo(
    () => targets.filter((target) => target.symbol === symbol),
    [symbol, targets],
  )
  const selectedTarget =
    symbolTargets.find((target) => datasetSourceKey(target) === selectedTargetKey) ??
    symbolTargets[0] ??
    null

  useEffect(() => {
    setSelectedTargetKey(selectedTarget ? datasetSourceKey(selectedTarget) : '')
  }, [selectedTarget])

  async function evaluate() {
    if (!symbol) return
    setLoading(true)
    setError(null)
    setSelected(null)
    try {
      const result = await trainingApi.evaluate(
        run.id,
        symbol,
        split,
        selectedTarget
          ? { timeframe: selectedTarget.timeframe, source_key: datasetSourceKey(selectedTarget) }
          : undefined,
      )
      const source = selectedTarget ?? sources[symbol]
      const chart = await api.chart(symbol, result.timeframe as TimeframeCode, [], {
        limit: MAX_PREDICTION_CHART_BARS,
        before: chartBefore(result.points, result.timeframe),
        start: selectedTarget?.start ?? undefined,
        end: selectedTarget?.end ?? undefined,
        region: source?.region ?? 'domestic',
        exchange: source?.exchange ?? '',
      })
      setEvaluation(result)
      setCandles(chart.candles)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  const points = evaluation?.points ?? []
  const incorrect = points.filter((point) => !point.correct)
  const visiblePoints = misclassifiedOnly ? incorrect : points

  const movePoint = useCallback(
    (delta: number) => {
      if (visiblePoints.length === 0) return
      setSelected((current) => {
        const position = current
          ? visiblePoints.findIndex((point) => point.sample_index === current.sample_index)
          : -1
        const next =
          position === -1
            ? delta > 0
              ? 0
              : visiblePoints.length - 1
            : (position + delta + visiblePoints.length) % visiblePoints.length
        return visiblePoints[next]
      })
    },
    [visiblePoints],
  )

  useEffect(() => {
    if (!selected) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isEditableTarget(event.target)) return
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
      event.preventDefault()
      movePoint(event.key === 'ArrowLeft' ? -1 : 1)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [movePoint, selected])

  return (
    <div className="pred-panel">
      <div className="pred-toolbar">
        <label className="inline-field">
          split
          <select
            onChange={(event) => setSplit(event.target.value as EvaluationSplit)}
            value={split}
          >
            <option value="validation">validation</option>
            <option value="test">test</option>
          </select>
        </label>
        {symbolTargets.length > 1 ? (
          <label className="inline-field">
            데이터
            <select
              onChange={(event) => setSelectedTargetKey(event.target.value)}
              value={selectedTargetKey}
            >
              {symbolTargets.map((target) => {
                const key = datasetSourceKey(target)
                return (
                  <option key={key} value={key}>
                    {timeframeLabel(target.timeframe)} · {target.start ?? '처음'} ~{' '}
                    {target.end ?? '최신'}
                  </option>
                )
              })}
            </select>
          </label>
        ) : null}
        <label className="inline-field">
          종목
          <select
            disabled={splitSymbols.length === 0}
            onChange={(event) => setSymbol(event.target.value)}
            value={symbol ?? ''}
          >
            {splitSymbols.map((row) => (
              <option key={row.symbol} value={row.symbol}>
                {row.symbol}
              </option>
            ))}
          </select>
        </label>
        <button
          className="primary"
          disabled={loading || !symbol}
          onClick={evaluate}
          type="button"
        >
          {loading ? '평가 중...' : '평가 실행'}
        </button>
        <label className="inline-check">
          <input
            checked={misclassifiedOnly}
            onChange={(event) => {
              setMisclassifiedOnly(event.target.checked)
              setSelected(null)
            }}
            type="checkbox"
          />
          오분류만
        </label>
        {evaluation && (
          <span className="pred-count">
            {points.length.toLocaleString()}건 중 오분류 {incorrect.length.toLocaleString()}건
          </span>
        )}
      </div>

      {symbolsError ? <p className="error">종목 목록 오류: {symbolsError}</p> : null}
      {error ? <p className="error">오류: {error}</p> : null}
      {!symbolsError && splitSymbols.length === 0 && symbols.length > 0 ? (
        <p className="empty">{split} split에 배정된 종목이 없습니다.</p>
      ) : null}

      {evaluation && !loading ? (
        points.length === 0 ? (
          <p className="empty">해당 종목/split에 예측 대상 샘플이 없습니다.</p>
        ) : (
          <div className="pred-layout">
            <div className="pred-chart-panel">
              <div className="pred-legend">
                <span>
                  <i className="pred-key arrow-up" /> 실제 저점
                </span>
                <span>
                  <i className="pred-key arrow-down" /> 실제 고점
                </span>
                <span>
                  <i className="pred-key dot" /> 실제 무시
                </span>
                <span>
                  <i className="pred-key correct" /> 정답
                </span>
                <span>
                  <i className="pred-key incorrect" /> 오답
                </span>
                <span className="muted-text">
                  마커 클릭 → 상세 · ←/→ 포인트 이동 · P#=예측 클래스
                </span>
              </div>
              <PredictionChart
                candles={candles}
                onSelect={setSelected}
                points={visiblePoints}
                priceDecimals={(selectedTarget ?? sources[symbol ?? ''])?.region === 'overseas' ? 2 : 0}
                selectedIndex={selected?.sample_index ?? null}
              />
            </div>
            <div className="pred-detail">
              {selected ? (
                <>
                  <div className="pred-detail-head">
                    <strong>샘플 #{selected.sample_index}</strong>
                    <span className={selected.correct ? 'pred-badge correct' : 'pred-badge incorrect'}>
                      {selected.correct ? '정답' : '오답'}
                    </span>
                  </div>
                  <dl className="pred-detail-meta">
                    <div>
                      <dt>시각</dt>
                      <dd>{formatTime(selected.time)}</dd>
                    </div>
                    <div>
                      <dt>실제</dt>
                      <dd>
                        {selected.actual_label} {LABEL_TEXT[selected.actual_label]}
                      </dd>
                    </div>
                    <div>
                      <dt>예측</dt>
                      <dd>
                        {selected.predicted_label} {LABEL_TEXT[selected.predicted_label]}
                      </dd>
                    </div>
                  </dl>
                  <div className="pred-probs">
                    {selected.probabilities.map((probability, label) => (
                      <div className="pred-prob-row" key={label}>
                        <span>
                          {label} {LABEL_TEXT[label]}
                        </span>
                        <div className="pred-prob-track">
                          <div
                            className={
                              label === selected.predicted_label
                                ? 'pred-prob-fill predicted'
                                : 'pred-prob-fill'
                            }
                            style={{ width: `${Math.round(probability * 100)}%` }}
                          />
                        </div>
                        <em>{(probability * 100).toFixed(1)}%</em>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="empty">차트에서 마커를 클릭하면 예측 상세를 보여줍니다.</p>
              )}
            </div>
          </div>
        )
      ) : null}
      {!evaluation && !loading && !error && splitSymbols.length > 0 ? (
        <p className="empty">종목과 split을 선택한 뒤 평가를 실행하세요.</p>
      ) : null}
      {loading ? <p className="hint">예측을 계산하는 중...</p> : null}
    </div>
  )
}
