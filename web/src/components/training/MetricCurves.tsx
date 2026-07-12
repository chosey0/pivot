import { useEffect, useRef } from 'react'
import { createChart, LineSeries, type UTCTimestamp } from 'lightweight-charts'
import type { EpochRow } from '../../api/training'

const TRAIN_COLOR = '#2563eb'
const VALIDATION_COLOR = '#e5484d'

interface SeriesSpec {
  color: string
  values: { epoch: number; value: number }[]
}

/** x축이 epoch 순번인 소형 라인 차트 — 시간값 대신 epoch 번호를 그대로 축에 쓴다. */
function EpochLineChart({ series, digits }: { series: SeriesSpec[]; digits: number }) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#6b7280' },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: 'rgba(107, 114, 128, 0.12)' },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: false,
        tickMarkFormatter: (time: UTCTimestamp) => String(time),
      },
      rightPriceScale: { borderVisible: false },
      localization: {
        timeFormatter: (time: UTCTimestamp) => `epoch ${time}`,
        priceFormatter: (value: number) =>
          value.toLocaleString('ko-KR', { maximumFractionDigits: digits }),
      },
      handleScroll: false,
      handleScale: false,
    })
    for (const spec of series) {
      const line = chart.addSeries(LineSeries, {
        color: spec.color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      line.setData(
        spec.values.map((point) => ({
          time: point.epoch as UTCTimestamp,
          value: point.value,
        })),
      )
    }
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [series, digits])

  return <div className="metric-chart" ref={containerRef} />
}

const CURVES: { title: string; digits: number; pick: (row: EpochRow) => [number, number] }[] = [
  {
    title: 'Loss',
    digits: 4,
    pick: (row) => [row.metrics.train_loss, row.metrics.validation_loss],
  },
  {
    title: 'Accuracy',
    digits: 3,
    pick: (row) => [row.metrics.train_accuracy, row.metrics.validation_accuracy],
  },
  {
    title: 'Macro F1',
    digits: 3,
    pick: (row) => [row.metrics.train_macro_f1, row.metrics.validation_macro_f1],
  },
]

export function MetricCurves({ epochs }: { epochs: EpochRow[] }) {
  if (epochs.length === 0) {
    return <p className="empty">아직 기록된 epoch가 없습니다.</p>
  }
  return (
    <div className="metric-grid">
      {CURVES.map(({ title, digits, pick }) => (
        <figure className="metric-cell" key={title}>
          <figcaption className="metric-title">
            <strong>{title}</strong>
            <span className="metric-legend">
              <i style={{ backgroundColor: TRAIN_COLOR }} /> train
              <i style={{ backgroundColor: VALIDATION_COLOR }} /> validation
            </span>
          </figcaption>
          <EpochLineChart
            digits={digits}
            series={[
              {
                color: TRAIN_COLOR,
                values: epochs.map((row) => ({ epoch: row.epoch, value: pick(row)[0] })),
              },
              {
                color: VALIDATION_COLOR,
                values: epochs.map((row) => ({ epoch: row.epoch, value: pick(row)[1] })),
              },
            ]}
          />
        </figure>
      ))}
    </div>
  )
}
