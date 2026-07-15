import { useEffect, useRef, useState } from 'react'
import type { Data, Layout, PlotHoverEvent } from 'plotly.js-basic-dist-min'
import type { EpochRow } from '../../api/training'
import { chartTheme, useTheme } from '../../lib/theme'

const TRAIN_COLOR = '#2563eb'
const VALIDATION_COLOR = '#e5484d'

type PlotlyModule = typeof import('plotly.js-basic-dist-min')

// plotly는 학습 탭에서만 쓰므로 초기 번들에서 분리하고, 로드된 모듈은 재사용한다
let plotly: PlotlyModule | null = null

async function loadPlotly(): Promise<PlotlyModule> {
  if (!plotly) {
    // UMD(`export =`) 번들이라 interop 결과가 namespace일 수도, default일 수도 있다
    const loaded = (await import('plotly.js-basic-dist-min')) as unknown as {
      default?: PlotlyModule
    } & PlotlyModule
    plotly = loaded.default ?? loaded
  }
  return plotly
}

interface SeriesSpec {
  name: string
  color: string
  values: { epoch: number; value: number }[]
}

interface HoverState {
  epoch: number
  rows: { name: string; color: string; value: number }[]
  left: number
  top: number
  flip: boolean
}

/** x축이 epoch 순번인 소형 라인 차트. */
function EpochLineChart({ series, digits }: { series: SeriesSpec[]; digits: number }) {
  const theme = useTheme()
  const containerRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<HoverState | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    // plotly도 CSS 변수를 못 읽으므로 테마 토큰을 값으로 넣는다 (theme 의존성으로 다시 그린다)
    const palette = chartTheme()
    const axisFont = { size: 10, color: palette.axisText }
    const data: Data[] = series.map((spec) => ({
      type: 'scatter',
      mode: 'lines+markers',
      name: spec.name,
      x: spec.values.map((point) => point.epoch),
      y: spec.values.map((point) => point.value),
      line: { color: spec.color, width: 2 },
      marker: { color: spec.color, size: 4 },
      // 라벨은 HTML로 직접 그린다 (SVG rect에는 backdrop blur가 적용되지 않는다).
      // 'none'은 기본 라벨만 숨기고 plotly_hover 이벤트는 그대로 발생시킨다 ('skip'은 이벤트까지 막는다).
      hoverinfo: 'none',
    }))
    // epoch은 정수 순번이라 자동 눈금의 소수 간격 대신 실제 첫 epoch부터 정수 간격으로 찍는다
    const epochs = series[0]?.values.map((point) => point.epoch) ?? []
    const firstEpoch = epochs[0] ?? 1
    const lastEpoch = epochs[epochs.length - 1] ?? 1
    const layout: Partial<Layout> = {
      margin: { t: 8, r: 8, b: 28, l: 44 },
      paper_bgcolor: 'transparent',
      plot_bgcolor: 'transparent',
      showlegend: false,
      hovermode: 'x unified',
      xaxis: {
        tick0: firstEpoch,
        dtick: Math.max(1, Math.ceil((lastEpoch - firstEpoch + 1) / 8)),
        tickformat: 'd',
        zeroline: false,
        showgrid: false,
        linecolor: palette.grid,
        tickfont: axisFont,
        showspikes: true,
        spikemode: 'across',
        spikedash: 'dot',
        spikethickness: 1,
        spikecolor: palette.muted,
      },
      yaxis: {
        tickformat: `.${digits}f`,
        zeroline: false,
        gridcolor: palette.grid,
        linecolor: palette.grid,
        tickfont: axisFont,
      },
    }

    let plotted = false
    loadPlotly().then((Plotly) => {
      if (!containerRef.current) return // 로드 완료 전에 언마운트됨
      Plotly.react(container, data, layout, {
        displayModeBar: false,
        responsive: true,
        doubleClick: 'autosize',
      }).then((graph) => {
        plotted = true
        graph.on('plotly_hover', (event: PlotHoverEvent) => {
          const bounds = container.getBoundingClientRect()
          const mouse = event.event as MouseEvent | undefined
          if (!mouse || event.points.length === 0) return
          const left = mouse.clientX - bounds.left
          setHover({
            epoch: Number(event.points[0].x),
            // plotly는 커서에 가까운 순으로 넘겨주므로 trace 순서(train → validation)로 되돌린다
            rows: [...event.points]
              .sort((a, b) => a.curveNumber - b.curveNumber)
              .map((point) => ({
                name: point.data.name ?? '',
                color: String(point.data.line?.color ?? palette.muted),
                value: Number(point.y),
              })),
            left,
            top: mouse.clientY - bounds.top,
            flip: left > bounds.width * 0.55,
          })
        })
        graph.on('plotly_unhover', () => setHover(null))
      })
    })
    return () => {
      setHover(null)
      if (plotted) plotly?.purge(container)
    }
  }, [digits, series, theme])

  return (
    <div className="metric-chart-frame">
      <div className="metric-chart" ref={containerRef} />
      {hover && (
        <div
          className={hover.flip ? 'metric-tooltip flip' : 'metric-tooltip'}
          style={{ left: hover.left, top: hover.top }}
        >
          <strong>epoch {hover.epoch}</strong>
          {hover.rows.map((row) => (
            <span key={row.name}>
              <i style={{ backgroundColor: row.color }} />
              {row.name}
              <em>{row.value.toFixed(digits)}</em>
            </span>
          ))}
        </div>
      )}
    </div>
  )
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
                name: 'train',
                color: TRAIN_COLOR,
                values: epochs.map((row) => ({ epoch: row.epoch, value: pick(row)[0] })),
              },
              {
                name: 'validation',
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
