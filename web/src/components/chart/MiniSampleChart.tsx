import { useEffect, useRef } from 'react'
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  type UTCTimestamp,
} from 'lightweight-charts'

// 샘플 피처에 Time이 없으므로(백로그 A2) x축은 봉 순번이다 — 시간축은 숨긴다
const BASE_COLUMNS = ['Open', 'High', 'Low', 'Close']
const SCALE_MISMATCH_COLUMNS = ['Volume', 'Amount'] // 가격축과 스케일이 달라 제외
const LINE_COLORS = ['#009c62', '#e31b35', '#ff8a00', '#8a26b2', '#0ea5e9', '#64748b']

interface Props {
  columns: string[]
  features: number[][]
}

/** 샘플 브라우저용 축소판 차트 — 원본 피처 시퀀스를 정적으로 그린다 (docs/04 §5). */
export function MiniSampleChart({ columns, features }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const hasOhlc = BASE_COLUMNS.every((name) => columns.includes(name))
  const lineColumns = columns.filter(
    (name) =>
      !SCALE_MISMATCH_COLUMNS.includes(name) && (!hasOhlc || !BASE_COLUMNS.includes(name)),
  )

  useEffect(() => {
    const container = containerRef.current
    if (!container || features.length === 0) return

    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#6b7280' },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: 'rgba(107, 114, 128, 0.12)' },
      },
      timeScale: { visible: false },
      rightPriceScale: { borderVisible: false },
      localization: {
        priceFormatter: (value: number) =>
          value.toLocaleString('ko-KR', { maximumFractionDigits: 2 }),
      },
      handleScroll: false,
      handleScale: false,
    })
    const time = (position: number) => (position * 60) as UTCTimestamp
    const columnIndex = new Map(columns.map((name, index) => [name, index]))
    const hasOhlcData = BASE_COLUMNS.every((name) => columnIndex.has(name))

    if (hasOhlcData) {
      const candles = chart.addSeries(CandlestickSeries, {
        upColor: '#e5484d',
        downColor: '#3b82f6',
        borderVisible: false,
        wickUpColor: '#e5484d',
        wickDownColor: '#3b82f6',
        priceLineVisible: false,
        lastValueVisible: false,
      })
      candles.setData(
        features.map((row, position) => ({
          time: time(position),
          open: row[columnIndex.get('Open')!],
          high: row[columnIndex.get('High')!],
          low: row[columnIndex.get('Low')!],
          close: row[columnIndex.get('Close')!],
        })),
      )
    }

    const chartLineColumns = columns.filter(
      (name) =>
        !SCALE_MISMATCH_COLUMNS.includes(name) &&
        (!hasOhlcData || !BASE_COLUMNS.includes(name)),
    )
    chartLineColumns.forEach((name, order) => {
      const series = chart.addSeries(LineSeries, {
        color: LINE_COLORS[order % LINE_COLORS.length],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      const index = columnIndex.get(name)!
      series.setData(
        features.map((row, position) => ({ time: time(position), value: row[index] })),
      )
    })

    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [columns, features])

  return (
    <div className="mini-chart-panel">
      <div className="mini-chart-legend">
        <strong>가격 및 보조지표</strong>
        <span><i className="candle-up" />상승</span>
        <span><i className="candle-down" />하락</span>
        {lineColumns.map((name, order) => (
          <span key={name}>
            <i style={{ backgroundColor: LINE_COLORS[order % LINE_COLORS.length] }} />
            {name}
          </span>
        ))}
      </div>
      <div className="mini-chart" ref={containerRef} />
    </div>
  )
}
