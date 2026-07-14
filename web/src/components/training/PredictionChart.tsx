import { useEffect, useRef } from 'react'
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts'
import type { Candle } from '../../api/client'
import type { PredictionPoint } from '../../api/training'
import { chartPriceFormat, formatChartPrice, type PriceDecimals } from '../../lib/chartPrice'

// 국내 관례: 상승 빨강 / 하락 파랑 (CandleChart와 동일)
const UP_COLOR = '#e5484d'
const DOWN_COLOR = '#3b82f6'
const CORRECT_COLOR = '#059669'
const INCORRECT_COLOR = '#dc2626'

interface Props {
  candles: Candle[]
  points: PredictionPoint[]
  selectedIndex: number | null
  onSelect: (point: PredictionPoint) => void
  priceDecimals?: PriceDecimals
}

function timeToKey(time: Time): string | number {
  if (typeof time === 'object') {
    const pad = (value: number) => String(value).padStart(2, '0')
    return `${time.year}-${pad(time.month)}-${pad(time.day)}`
  }
  return time
}

/**
 * 예측 검수 마커: 화살표 방향 = 실제 라벨(▲저점/▼고점/●무시),
 * 색 = 정오답(초록 정답/빨강 오답), 텍스트 = 예측 클래스.
 */
function toMarker(point: PredictionPoint, selected: boolean): SeriesMarker<Time> {
  const color = point.correct ? CORRECT_COLOR : INCORRECT_COLOR
  const size = selected ? 3 : 1
  const text = `P${point.predicted_label}`
  if (point.actual_label === 0) {
    return { time: point.time as Time, position: 'belowBar', shape: 'arrowUp', color, size, text }
  }
  if (point.actual_label === 1) {
    return { time: point.time as Time, position: 'aboveBar', shape: 'arrowDown', color, size, text }
  }
  return { time: point.time as Time, position: 'aboveBar', shape: 'circle', color, size, text }
}

export function PredictionChart({
  candles,
  points,
  selectedIndex,
  onSelect,
  priceDecimals = 0,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const markersApiRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const pointsRef = useRef(points)
  const onSelectRef = useRef(onSelect)

  useEffect(() => {
    pointsRef.current = points
    onSelectRef.current = onSelect
  }, [points, onSelect])

  useEffect(() => {
    const container = containerRef.current
    if (!container || candles.length === 0) return

    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#6b7280' },
      grid: {
        vertLines: { color: 'rgba(107, 114, 128, 0.12)' },
        horzLines: { color: 'rgba(107, 114, 128, 0.12)' },
      },
      localization: {
        priceFormatter: (price: number) => formatChartPrice(price, priceDecimals),
      },
      timeScale: { borderVisible: false },
      rightPriceScale: { borderVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderVisible: false,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
      priceFormat: chartPriceFormat(priceDecimals),
    })
    series.setData(
      candles.map((candle) => ({
        time: candle.time as Time,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      })),
    )
    markersApiRef.current = createSeriesMarkers(series, [])
    chart.subscribeClick((param) => {
      if (param.time === undefined) return
      const key = timeToKey(param.time)
      const point = pointsRef.current.find((item) => item.time === key)
      if (point) onSelectRef.current(point)
    })
    chart.timeScale().fitContent()

    return () => {
      chart.remove()
      markersApiRef.current = null
    }
  }, [candles, priceDecimals])

  useEffect(() => {
    markersApiRef.current?.setMarkers(
      points.map((point) => toMarker(point, point.sample_index === selectedIndex)),
    )
  }, [points, selectedIndex, candles, priceDecimals])

  return <div className="pred-chart" ref={containerRef} />
}
